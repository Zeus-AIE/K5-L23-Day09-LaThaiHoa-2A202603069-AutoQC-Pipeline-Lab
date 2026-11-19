"""Trạm kiểm soát tự động cho một CVAT Job (Pre-flight QC Analyzer)."""
from __future__ import annotations

import copy
import csv
import json
import shutil
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from pipeline import checks
from pipeline.checks import second_opinion
from pipeline.common import ERROR, WARN, Dataset, Issue, iou_xywh, load_config, parse_yolo_line
from pipeline.model_cache import YOLO2PRJ, load_cache, run_model
from pipeline.steps import autofix, gate, intake, review_queue
from pipeline.steps.report import write_report

from .connector import prepare_source_dataset


@dataclass
class JobQCResult:
    """Kết quả phân tích toàn diện một CVAT Job tại trạm Pre-flight QC."""

    job_id: str
    annotator: str
    timestamp: str
    n_scanned: int
    n_accepted: int
    n_rejected: int
    quality_score: float
    verdict: dict
    intake_issues: list[Issue]
    v1_issues: list[Issue]
    fixes: list[dict]
    v2_issues: list[Issue]
    review_queue: list[dict]
    class_counts: dict[str, int]
    flagged_lines: dict[tuple[str, int], list[str]]
    model_suggestions: dict[str, list[dict]]
    clean_ds_dir: Path
    source_ds_dir: Path
    reports_dir: Path
    elapsed_sec: float
    cfg: dict = field(repr=False)
    live_meta: dict | None = None
    notes: list[str] = field(default_factory=list)

    def code_summary(self, issues: list[Issue] | None = None) -> list[dict]:
        """Thống kê số lượng lỗi theo mã và mức độ."""
        target = issues if issues is not None else (self.intake_issues + self.v1_issues)
        counts = Counter((i.severity, i.code) for i in target)
        rows = []
        for (sev, code), n in sorted(counts.items(), key=lambda x: (x[0][0] != ERROR, -x[1], x[0][1])):
            rows.append({"severity": sev, "code": code, "count": n})
        return rows

    def to_summary_dict(self) -> dict:
        frames_map = (self.live_meta or {}).get("frames_by_stem", {})
        task_id = (self.live_meta or {}).get("task_id")
        cvat_url = (self.live_meta or {}).get("cvat_url", "http://localhost:8080")
        cvat_job_id = (self.live_meta or {}).get("job_id")
        enriched_queue = []
        for row in self.review_queue:
            finfo = frames_map.get(row["image"])
            frame_no = finfo["frame"] if finfo else None
            cvat_frame_url = (
                f"{cvat_url}/tasks/{task_id}/jobs/{cvat_job_id}?frame={frame_no}"
                if (task_id is not None and cvat_job_id is not None and frame_no is not None)
                else None
            )
            enriched_queue.append({**row, "frame": frame_no, "cvat_frame_url": cvat_frame_url})

        return {
            "job_id": self.job_id,
            "annotator": self.annotator,
            "timestamp": self.timestamp,
            "n_scanned": self.n_scanned,
            "n_accepted": self.n_accepted,
            "n_rejected": self.n_rejected,
            "quality_score": self.quality_score,
            "verdict": self.verdict,
            "n_v1_errors": sum(i.severity == ERROR for i in self.intake_issues + self.v1_issues),
            "n_v1_warns": sum(i.severity == WARN for i in self.intake_issues + self.v1_issues),
            "n_fixes": len(self.fixes),
            "n_v2_errors": sum(i.severity == ERROR for i in self.v2_issues),
            "n_v2_warns": sum(i.severity == WARN for i in self.v2_issues),
            "queue_size": len(self.review_queue),
            "n_flagged_boxes": len(self.flagged_lines),
            "classes": self.cfg["classes"],
            "class_counts": self.class_counts,
            "code_summary_v1": self.code_summary(self.intake_issues + self.v1_issues),
            "code_summary_v2": self.code_summary(self.v2_issues),
            "review_queue": enriched_queue,
            "fixes": self.fixes,
            "live_meta": {
                "is_live": self.live_meta is not None,
                "cvat_url": cvat_url,
                "task_id": task_id,
                "task_name": (self.live_meta or {}).get("task_name"),
                "project_name": (self.live_meta or {}).get("project_name"),
                "job_id": cvat_job_id,
                "job_url": f"{cvat_url}/tasks/{task_id}/jobs/{cvat_job_id}" if task_id and cvat_job_id else None,
            },
            "notes": self.notes,
            "elapsed_sec": self.elapsed_sec,
        }


def _write_dict_csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        w.writerows(rows)


def _resolve_predictions(ds: Dataset, cfg: dict, active_classes: list[str], notes: list[str]) -> dict:
    """Lấy dự đoán AI (ưu tiên cache `checker_preds.json`, sau đó thử model cục bộ, ánh xạ đúng tên lớp)."""
    mc = cfg["models"]["checker"]
    raw_cache = load_cache(mc["cache"])
    need = [p for p in ds.image_files() if p.stem not in raw_cache and ds.image_size(p.stem)]
    if need:
        for candidate in (mc["weights"], "yolov8n.pt"):
            if not Path(candidate).exists() and candidate == mc["weights"]:
                continue
            try:
                raw_cache.update(run_model(candidate, need, conf=mc.get("conf", 0.25)))
                notes.append(f"Đã suy luận bổ sung {len(need)} ảnh bằng {candidate}")
                break
            except Exception as exc:
                notes.append(f"Không chạy được model {candidate}: {exc}")
        else:
            # Thử tải/chạy weight cấu hình mặc định chỉ khi còn thiếu hoàn toàn
            if not any(p.stem in raw_cache for p in ds.image_files()):
                notes.append(f"Bỏ qua second_opinion cho {len(need)} ảnh chưa có trong cache (chưa có weight cục bộ).")

    # Ánh xạ id lớp của cache (0:car, 1:person, 2:truck, 3:bus) sang thứ tự lớp thực tế của Job
    base_names = ["car", "person", "truck", "bus"]
    lower_active = {c.lower(): idx for idx, c in enumerate(active_classes)}
    remap = {idx: lower_active[name] for idx, name in enumerate(base_names) if name in lower_active}
    _ = YOLO2PRJ

    mapped: dict[str, list] = {}
    for p in ds.image_files():
        stem = p.stem
        if stem not in raw_cache:
            continue
        rows = []
        for r in raw_cache[stem]:
            src_cls = int(r[0])
            if src_cls in remap:
                rows.append([remap[src_cls], r[1], r[2], r[3], r[4], r[5]])
        mapped[stem] = rows
    return mapped


def _run_qc_suite(ds: Dataset, cfg: dict, qc_names: list[str], preds: dict | None) -> list[Issue]:
    out: list[Issue] = []
    for name in qc_names:
        if name == "second_opinion":
            if preds is not None:
                out.extend(second_opinion.run(ds, cfg, preds=preds))
        else:
            out.extend(checks.load(name).run(ds, cfg))
    return out


def _map_flagged_lines(ds: Dataset, issues: list[Issue], cfg: dict, preds: dict) -> tuple[dict[tuple[str, int], list[str]], dict[str, list[dict]]]:
    """Ánh xạ các issue ở mức ảnh/dòng về đúng (stem, line_no) để gắn cờ `can_review` trên CVAT."""
    flagged: dict[tuple[str, int], list[str]] = defaultdict(list)
    suggestions: dict[str, list[dict]] = defaultdict(list)
    sc = cfg["second_opinion"]
    conf_thr = sc["conf"]
    min_area_px = sc["min_area_px"]
    iou_missing = sc["iou_missing"]
    iou_match = sc["iou_match"]
    iou_loose = sc["iou_loose"]

    for i in issues:
        if i.line is not None and i.image not in ("data.yaml", "<dataset>"):
            flagged[(i.image, int(i.line))].append(i.code)

    for img_path in ds.image_files():
        stem = img_path.stem
        if stem not in preds:
            continue
        size = ds.image_size(stem)
        raw_lines = ds.read_label_lines(stem)
        if size is None or raw_lines is None:
            continue
        img_w, img_h = size
        valid_labels = []
        for line_no, text in raw_lines:
            try:
                c, cx, cy, w, h = parse_yolo_line(text)
            except ValueError:
                continue
            if 0 <= c < len(ds.classes) and w > 0 and h > 0:
                valid_labels.append((line_no, c, (cx, cy, w, h)))

        for r in preds[stem]:
            p_cls, p_conf = int(r[0]), float(r[1])
            px, py, pw, ph = float(r[2]), float(r[3]), float(r[4]), float(r[5])
            if p_conf < conf_thr or (pw * img_w) * (ph * img_h) < min_area_px:
                continue
            p_box = (px, py, pw, ph)
            cands = [(iou_xywh(p_box, lb), lc, ln) for ln, lc, lb in valid_labels if iou_xywh(p_box, lb) >= iou_missing]
            if not cands:
                suggestions[stem].append(
                    {"code": "POSSIBLE_MISSING", "class_id": p_cls, "conf": round(p_conf, 3), "box": [px, py, pw, ph]}
                )
                continue
            same = [(iou, ln) for iou, lc, ln in cands if lc == p_cls]
            if same:
                best_iou, best_ln = max(same, key=lambda x: x[0])
                if best_iou < iou_loose:
                    flagged[(stem, best_ln)].append("POSSIBLE_LOOSE_BOX")
                    suggestions[stem].append(
                        {"code": "POSSIBLE_LOOSE_BOX", "class_id": p_cls, "conf": round(p_conf, 3), "box": [px, py, pw, ph]}
                    )
            else:
                best_iou, _, best_ln = max(cands, key=lambda x: x[0])
                if best_iou >= iou_match:
                    flagged[(stem, best_ln)].append("POSSIBLE_WRONG_CLASS")
                    suggestions[stem].append(
                        {"code": "POSSIBLE_WRONG_CLASS", "class_id": p_cls, "conf": round(p_conf, 3), "box": [px, py, pw, ph]}
                    )

    dedup_flagged = {k: sorted(set(v)) for k, v in flagged.items()}
    return dedup_flagged, dict(suggestions)


def analyze_cvat_job(
    source: str | Path,
    job_id: str,
    annotator: str = "annotator",
    cfg: dict | None = None,
    work_dir: str | Path = "work/cvat_station",
    apply_autofix: bool = True,
    use_second_opinion: bool = True,
    live_meta: dict | None = None,
) -> JobQCResult:
    """Chạy trạm kiểm soát tự động (Pre-flight QC) trên một CVAT Job / thư mục dataset."""
    t0 = time.time()
    active_cfg = copy.deepcopy(cfg if cfg is not None else load_config())
    if "review" not in active_cfg:
        active_cfg["review"] = load_config()["review"]

    work = Path(work_dir)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    notes: list[str] = []

    if live_meta is None and (Path(source) / "live_mapping.json").exists():
        live_meta = json.loads((Path(source) / "live_mapping.json").read_text(encoding="utf-8"))

    if live_meta and live_meta.get("active_classes"):
        active_cfg["classes"] = list(live_meta["active_classes"])
        # Với CVAT Job live khác kích thước mẫu Lab, tắt check leak với kho lab nếu job không phải Day09
        if live_meta.get("project_name") and "day09" not in live_meta.get("project_name", "").lower():
            active_cfg["intake"]["reference_dirs"] = []

    src_ds = prepare_source_dataset(source, work / "imported_src", active_cfg["classes"])
    ds_dir = work / "clean_ds"
    rep_dir = work / "reports"
    rep_dir.mkdir(parents=True)

    # Bước 1: Intake (ảnh hỏng, kích thước, trùng, rò rỉ test)
    accepted, intake_issues = intake.run(src_ds.root, ds_dir, active_cfg)
    n_scanned = len(src_ds.image_files())
    write_report(intake_issues, rep_dir / "01_intake", f"Job {job_id} — Intake", n_scanned)

    ds = Dataset(ds_dir, active_cfg["classes"])
    preds = _resolve_predictions(ds, active_cfg, active_cfg["classes"], notes) if use_second_opinion else None

    # Bước 2: QC Vòng 1 trên bản sao làm việc
    qc_names = list(checks.QC_CHECKS) if use_second_opinion else [c for c in checks.QC_CHECKS if c != "second_opinion"]
    v1_issues = _run_qc_suite(ds, active_cfg, qc_names, preds)
    write_report(intake_issues + v1_issues, rep_dir / "02_error_list_v1", f"Job {job_id} — Lỗi thô (v1)", len(accepted))

    # Bước 3: Autofix trên bản sao (nếu bật)
    fixes = autofix.fix(ds, v1_issues, active_cfg) if apply_autofix else []
    _write_dict_csv(rep_dir / "03_fixes.csv", fixes, ["image", "line", "action", "before", "after"])

    # Bước 4: QC Vòng 2 + Hàng đợi ưu tiên + Gate Verdict ở trạng thái trước khi QC người duyệt
    ds = Dataset(ds_dir, active_cfg["classes"])
    v2_issues = _run_qc_suite(ds, active_cfg, qc_names, preds)
    stems = {p.stem for p in ds.image_files()}
    queue = review_queue.build_queue(v2_issues, fixes, stems, active_cfg)
    _write_dict_csv(rep_dir / "05_review_queue.csv", queue, ["image", "priority", "codes"])

    verdict = gate.decide(v2_issues, stems, active_cfg, reviewed=frozenset())
    write_report(v2_issues, rep_dir / "04_error_list_v2", f"Job {job_id} — Sau sửa tự động (v2)", len(stems), verdict)

    from pipeline.checks.stats import class_counts as calc_class_counts

    cls_counts = {str(k): int(v) for k, v in sorted(calc_class_counts(ds).items())}
    flagged_lines, suggestions = _map_flagged_lines(ds, v2_issues, active_cfg, preds or {})

    # Điểm chất lượng dựa trên tỉ lệ ảnh lỗi/cảnh báo ở v1 (không tính trùng 1 ảnh vào cả 2 nhóm)
    err_imgs = {i.image for i in (intake_issues + v1_issues) if i.severity == ERROR and i.image not in ("data.yaml", "<dataset>")}
    warn_only_imgs = {i.image for i in v1_issues if i.severity == WARN and i.image not in ("data.yaml", "<dataset>")} - err_imgs
    denom = max(1, n_scanned)
    penalty = ((1.5 * len(err_imgs)) + (0.5 * len(warn_only_imgs))) / denom * 100.0
    quality_score = round(max(0.0, 100.0 - penalty), 1)

    res = JobQCResult(
        job_id=str(job_id),
        annotator=annotator,
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
        n_scanned=n_scanned,
        n_accepted=len(accepted),
        n_rejected=n_scanned - len(accepted),
        quality_score=quality_score,
        verdict=verdict,
        intake_issues=intake_issues,
        v1_issues=v1_issues,
        fixes=fixes,
        v2_issues=v2_issues,
        review_queue=queue,
        class_counts=cls_counts,
        flagged_lines=flagged_lines,
        model_suggestions=suggestions,
        clean_ds_dir=ds_dir,
        source_ds_dir=src_ds.root,
        reports_dir=rep_dir,
        elapsed_sec=round(time.time() - t0, 2),
        cfg=active_cfg,
        live_meta=live_meta,
        notes=notes,
    )
    (rep_dir / "summary.json").write_text(json.dumps(res.to_summary_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return res
