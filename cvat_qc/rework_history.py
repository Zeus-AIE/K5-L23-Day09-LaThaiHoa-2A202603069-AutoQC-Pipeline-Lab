"""Theo dõi & ghi nhận lịch sử chỉnh sửa Rework ở mức từng hộp nhãn (box-level diff audit trail)."""
from __future__ import annotations

import csv
import json
import shutil
import time
from collections import Counter
from pathlib import Path

from pipeline.common import Dataset, iou_xywh, parse_yolo_line


def _read_boxes(ds: Dataset, stem: str, classes: list[str]) -> list[dict]:
    """Đọc danh sách box hợp lệ của 1 ảnh kèm số dòng và tên lớp."""
    out: list[dict] = []
    for line_no, raw_ln in ds.read_label_lines(stem) or []:
        try:
            cls_id, cx, cy, bw, bh = parse_yolo_line(raw_ln)
        except ValueError:
            continue
        if not (0 <= cls_id < len(classes)) or bw <= 0 or bh <= 0:
            continue
        out.append(
            {
                "line": line_no,
                "cls_id": cls_id,
                "cls_name": classes[cls_id],
                "box": (cx, cy, bw, bh),
                "text": f"{classes[cls_id]} ({cx:.3f}, {cy:.3f}, {bw:.3f}, {bh:.3f})",
            }
        )
    return out


def save_baseline_snapshot(ds_dir: str | Path, snapshot_dir: str | Path, meta: dict | None = None) -> Path:
    """Lưu bản chụp trạng thái trước khi chỉnh sửa (baseline) cho Job / Rework Job."""
    src = Path(ds_dir)
    dst = Path(snapshot_dir)
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for sub in ("images", "labels"):
        if (src / sub).is_dir():
            shutil.copytree(src / sub, dst / sub)
    if (src / "data.yaml").exists():
        shutil.copy2(src / "data.yaml", dst / "data.yaml")
    if meta is not None:
        (dst / "baseline_meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return dst


def compute_rework_diff(
    before_ds: Dataset,
    after_ds: Dataset,
    classes: list[str],
    iou_match_thr: float = 0.30,
    unchanged_iou_thr: float = 0.995,
) -> dict:
    """Đối chiếu từng ảnh và từng bounding box giữa trạng thái trước và sau khi sửa Rework."""
    before_stems = {p.stem for p in before_ds.image_files()} | {p.stem for p in before_ds.label_files()}
    after_stems = {p.stem for p in after_ds.image_files()} | {p.stem for p in after_ds.label_files()}
    all_stems = sorted(before_stems | after_stems)

    events: list[dict] = []
    edited_images: set[str] = set()

    for stem in all_stems:
        b_boxes = _read_boxes(before_ds, stem, classes)
        a_boxes = _read_boxes(after_ds, stem, classes)

        candidates: list[tuple[int, float, int, int]] = []
        for bi, b in enumerate(b_boxes):
            for ai, a in enumerate(a_boxes):
                iou = iou_xywh(b["box"], a["box"])
                if iou >= iou_match_thr:
                    exact_same = 1 if (b["cls_id"] == a["cls_id"] and iou >= unchanged_iou_thr) else 0
                    candidates.append((exact_same, iou, bi, ai))

        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        matched_b: set[int] = set()
        matched_a: set[int] = set()

        for _, iou, bi, ai in candidates:
            if bi in matched_b or ai in matched_a:
                continue
            matched_b.add(bi)
            matched_a.add(ai)
            b, a = b_boxes[bi], a_boxes[ai]
            if b["cls_id"] != a["cls_id"]:
                edited_images.add(stem)
                events.append(
                    {
                        "image": stem,
                        "change_type": "CLASS_CHANGED",
                        "before": b["text"],
                        "after": a["text"],
                        "iou": round(iou, 3),
                        "detail": f"Đổi lớp {b['cls_name']} → {a['cls_name']} (IoU={iou:.2f})",
                    }
                )
            elif iou < unchanged_iou_thr:
                edited_images.add(stem)
                events.append(
                    {
                        "image": stem,
                        "change_type": "BOX_ADJUSTED",
                        "before": b["text"],
                        "after": a["text"],
                        "iou": round(iou, 3),
                        "detail": f"Chỉnh khung box lớp {a['cls_name']} (IoU={iou:.2f})",
                    }
                )

        for bi, b in enumerate(b_boxes):
            if bi not in matched_b:
                edited_images.add(stem)
                events.append(
                    {
                        "image": stem,
                        "change_type": "BOX_DELETED",
                        "before": b["text"],
                        "after": "(đã xoá)",
                        "iou": 0.0,
                        "detail": f"Xoá box thừa/lỗi lớp {b['cls_name']} (dòng {b['line']})",
                    }
                )

        for ai, a in enumerate(a_boxes):
            if ai not in matched_a:
                edited_images.add(stem)
                events.append(
                    {
                        "image": stem,
                        "change_type": "BOX_ADDED",
                        "before": "(chưa có)",
                        "after": a["text"],
                        "iou": 0.0,
                        "detail": f"Thêm box mới lớp {a['cls_name']} (dòng {a['line']})",
                    }
                )

    counts = Counter(e["change_type"] for e in events)
    return {
        "n_images_compared": len(all_stems),
        "n_images_edited": len(edited_images),
        "n_changes": len(events),
        "counts": {
            "CLASS_CHANGED": counts.get("CLASS_CHANGED", 0),
            "BOX_ADJUSTED": counts.get("BOX_ADJUSTED", 0),
            "BOX_ADDED": counts.get("BOX_ADDED", 0),
            "BOX_DELETED": counts.get("BOX_DELETED", 0),
        },
        "edited_images": sorted(edited_images),
        "events": events,
    }


def load_rework_history(history_dir: str | Path) -> list[dict]:
    """Đọc toàn bộ danh sách phiên chỉnh sửa Rework đã ghi."""
    json_path = Path(history_dir) / "rework_history.json"
    if not json_path.exists():
        return []
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def record_rework_history(
    history_dir: str | Path,
    source_job_id: str,
    target_job_id: str,
    before_ds: Dataset,
    after_ds: Dataset,
    classes: list[str],
    editor: str = "annotator",
    issue_filter: str = "ALL",
) -> dict:
    """Ghi nhận thêm 1 phiên lịch sử chỉnh sửa Rework vào `rework_history.json` và `rework_history.csv`."""
    hdir = Path(history_dir)
    hdir.mkdir(parents=True, exist_ok=True)
    diff = compute_rework_diff(before_ds, after_ds, classes)

    existing = load_rework_history(hdir)
    revision_no = len(existing) + 1
    entry = {
        "revision": revision_no,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_job_id": str(source_job_id),
        "target_job_id": str(target_job_id),
        "editor": editor,
        "issue_filter": issue_filter,
        "n_images_compared": diff["n_images_compared"],
        "n_images_edited": diff["n_images_edited"],
        "n_changes": diff["n_changes"],
        "counts": diff["counts"],
        "events": diff["events"],
    }
    existing.append(entry)
    (hdir / "rework_history.json").write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")

    csv_path = hdir / "rework_history.csv"
    cols = ["revision", "timestamp", "source_job_id", "target_job_id", "editor", "issue_filter", "image", "change_type", "iou", "before", "after", "detail"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        for rev in existing:
            if not rev["events"]:
                w.writerow(
                    {
                        "revision": rev["revision"],
                        "timestamp": rev["timestamp"],
                        "source_job_id": rev["source_job_id"],
                        "target_job_id": rev["target_job_id"],
                        "editor": rev["editor"],
                        "issue_filter": rev["issue_filter"],
                        "image": "",
                        "change_type": "NO_CHANGE",
                        "iou": "",
                        "before": "",
                        "after": "",
                        "detail": "Chưa phát hiện chỉnh sửa mới so với baseline",
                    }
                )
            else:
                for ev in rev["events"]:
                    w.writerow(
                        {
                            "revision": rev["revision"],
                            "timestamp": rev["timestamp"],
                            "source_job_id": rev["source_job_id"],
                            "target_job_id": rev["target_job_id"],
                            "editor": rev["editor"],
                            "issue_filter": rev["issue_filter"],
                            "image": ev["image"],
                            "change_type": ev["change_type"],
                            "iou": ev["iou"],
                            "before": ev["before"],
                            "after": ev["after"],
                            "detail": ev["detail"],
                        }
                    )
    return entry
