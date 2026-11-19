"""Tạo CVAT Rework Sub-Job theo hàng đợi hoặc tách riêng theo từng loại Issue (gắn sẵn `can_review=true`)."""
from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from pathlib import Path

from pipeline.common import ERROR, ROOT, Dataset

from .analyzer import JobQCResult
from .connector import CVATLocalClient, export_cvat_xml, export_yolo11_zip
from .rework_history import save_baseline_snapshot


def _safe_slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "all"


def _collect_image_issue_codes(qc_result: JobQCResult) -> dict[str, set[str]]:
    """Tổng hợp toàn bộ mã lỗi/cảnh báo còn lại theo từng ảnh."""
    codes_by_img: dict[str, set[str]] = defaultdict(set)
    for i in qc_result.v2_issues:
        if i.image not in ("data.yaml", "<dataset>"):
            codes_by_img[i.image].add(i.code)
    for row in qc_result.review_queue:
        for c in str(row.get("codes", "")).split(";"):
            c = c.strip()
            if c:
                codes_by_img[row["image"]].add(c)
    return codes_by_img


def _select_rework_stems(
    qc_result: JobQCResult,
    max_images: int | None = None,
    issue_codes: list[str] | None = None,
) -> list[str]:
    """Chọn danh sách ảnh đưa vào Rework Job (hỗ trợ lọc theo danh sách `issue_codes`)."""
    wanted = {c.strip() for c in (issue_codes or []) if c and c.strip()}
    img_codes = _collect_image_issue_codes(qc_result)

    must_fix = [
        i.image
        for i in qc_result.v2_issues
        if i.severity == ERROR
        and i.image not in ("data.yaml", "<dataset>")
        and (not wanted or bool(img_codes.get(i.image, set()) & wanted))
    ]
    ordered: list[str] = []
    seen: set[str] = set()
    for s in must_fix:
        if s not in seen:
            ordered.append(s)
            seen.add(s)
    for row in qc_result.review_queue:
        s = row["image"]
        if wanted and not (img_codes.get(s, set()) & wanted):
            continue
        if s not in seen:
            ordered.append(s)
            seen.add(s)

    if max_images is not None and max_images >= 0:
        limit = max(len(must_fix), max_images)
        return ordered[:limit]
    return ordered


def _filter_flagged_lines(
    qc_result: JobQCResult,
    selected_stems: set[str],
    issue_codes: list[str] | None = None,
) -> dict[tuple[str, int], list[str]]:
    """Lọc cờ `can_review` đúng theo tập ảnh và tập loại issue được chọn."""
    wanted = {c.strip() for c in (issue_codes or []) if c and c.strip()}
    out: dict[tuple[str, int], list[str]] = {}
    for (stem, line_no), codes in qc_result.flagged_lines.items():
        if stem not in selected_stems:
            continue
        matched = [c for c in codes if not wanted or c in wanted]
        if matched:
            out[(stem, line_no)] = matched
    return out


def _filter_suggestions(
    qc_result: JobQCResult,
    selected_stems: set[str],
    issue_codes: list[str] | None = None,
) -> dict[str, list[dict]]:
    wanted = {c.strip() for c in (issue_codes or []) if c and c.strip()}
    out: dict[str, list[dict]] = {}
    for stem, sugs in qc_result.model_suggestions.items():
        if stem not in selected_stems:
            continue
        matched = [s for s in sugs if not wanted or s.get("code") in wanted]
        if matched:
            out[stem] = matched
    return out


def create_rework_job(
    qc_result: JobQCResult,
    out_dir: str | Path,
    max_images: int | None = None,
    cvat_client: CVATLocalClient | None = None,
    project_id: int | None = None,
    live_push: bool = False,
    issue_codes: list[str] | None = None,
) -> dict:
    """Đóng gói Rework Job chuẩn YOLO 1.1 + CVAT 1.1 XML (gắn `can_review=true`), tuỳ chọn lọc theo loại Issue & đẩy lên CVAT."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    wanted = [c.strip() for c in (issue_codes or []) if c and c.strip()]
    suffix = ("_" + _safe_slug("+".join(wanted))) if wanted else ""
    rework_name = f"rework_job_{qc_result.job_id}{suffix}"
    rework_dir = out_path / rework_name
    if rework_dir.exists():
        shutil.rmtree(rework_dir)
    (rework_dir / "images").mkdir(parents=True)
    (rework_dir / "labels").mkdir(parents=True)

    clean_ds = Dataset(qc_result.clean_ds_dir, qc_result.cfg["classes"])
    selected = _select_rework_stems(qc_result, max_images=max_images, issue_codes=wanted)
    selected_set = set(selected)
    filtered_flags = _filter_flagged_lines(qc_result, selected_set, issue_codes=wanted)
    filtered_sugs = _filter_suggestions(qc_result, selected_set, issue_codes=wanted)
    queue_map = {r["image"]: r for r in qc_result.review_queue}

    copied_images: list[Path] = []
    manifest_items: list[dict] = []
    for stem in selected:
        img_p = clean_ds.image_path(stem)
        if img_p is None or not img_p.exists():
            continue
        dst_img = rework_dir / "images" / img_p.name
        shutil.copy2(img_p, dst_img)
        copied_images.append(dst_img)

        lbl_p = clean_ds.label_path(stem)
        if lbl_p.exists():
            shutil.copy2(lbl_p, rework_dir / "labels" / lbl_p.name)

        q_info = queue_map.get(stem, {})
        flagged_for_img = {
            str(ln): codes
            for (s, ln), codes in sorted(filtered_flags.items())
            if s == stem
        }
        manifest_items.append(
            {
                "image": stem,
                "filename": img_p.name,
                "priority": q_info.get("priority", 0.0),
                "codes": q_info.get("codes", ""),
                "flagged_lines": flagged_for_img,
                "ai_suggestions": filtered_sugs.get(stem, []),
            }
        )

    if clean_ds.yaml_path.exists():
        shutil.copy2(clean_ds.yaml_path, rework_dir / "data.yaml")

    rework_ds = Dataset(rework_dir, qc_result.cfg["classes"])
    tag_label = f"[{', '.join(wanted)}] " if wanted else ""
    task_title = f"AutoQC Rework {tag_label}— Job {qc_result.job_id}"

    xml_path = out_path / f"{rework_name}_annotations.xml"
    export_cvat_xml(
        rework_ds,
        filtered_flags,
        qc_result.cfg["classes"],
        xml_path,
        task_name=task_title,
    )

    manifest = {
        "source_job_id": qc_result.job_id,
        "annotator": qc_result.annotator,
        "rework_task_name": task_title,
        "issue_codes": wanted or ["ALL"],
        "n_images": len(copied_images),
        "n_flagged_boxes": sum(len(item["flagged_lines"]) for item in manifest_items),
        "cvat_filter_hint": 'attr["can_review"] == true',
        "items": manifest_items,
    }
    manifest_path = out_path / f"{rework_name}_manifest.json"
    manifest_text = json.dumps(manifest, indent=2, ensure_ascii=False)
    manifest_path.write_text(manifest_text, encoding="utf-8")

    labels_json_path = ROOT / "cvat" / "labels.json"
    extra_files: dict[str, str | bytes] = {
        "rework_manifest.json": manifest_text,
        "cvat_annotations_can_review.xml": xml_path.read_bytes(),
    }
    if labels_json_path.exists():
        extra_files["cvat_labels.json"] = labels_json_path.read_bytes()

    zip_path = out_path / f"{rework_name}.zip"
    export_yolo11_zip(rework_ds, zip_path, qc_result.cfg["classes"], include_images=True, extra_files=extra_files)

    # Lưu bản baseline phục vụ đối chiếu lịch sử chỉnh sửa Rework về sau
    baseline_dir = out_path / "baselines" / rework_name
    save_baseline_snapshot(rework_dir, baseline_dir, meta={"source_job_id": qc_result.job_id, "issue_codes": wanted or ["ALL"]})

    cvat_task = None
    if cvat_client is not None and copied_images:
        if live_push:
            target_proj = project_id if project_id is not None else (qc_result.live_meta or {}).get("project_id")
            cvat_task = cvat_client.create_rework_task_live(
                name=task_title,
                ds=rework_ds,
                flagged_lines=filtered_flags,
                classes=qc_result.cfg["classes"],
                suggestions=filtered_sugs,
                project_id=target_proj,
                labels_schema_path=labels_json_path if labels_json_path.exists() else None,
            )
            if cvat_task and cvat_task.get("job_id") is not None:
                save_baseline_snapshot(
                    rework_dir,
                    out_path / "baselines" / f"cvat_{cvat_task['job_id']}",
                    meta={
                        "source_job_id": qc_result.job_id,
                        "rework_cvat_job_id": cvat_task["job_id"],
                        "issue_codes": wanted or ["ALL"],
                    },
                )
        else:
            cvat_task = cvat_client.create_rework_task(
                name=task_title,
                image_paths=copied_images,
                labels_schema_path=labels_json_path if labels_json_path.exists() else None,
                annotations_xml_path=xml_path,
                project_id=project_id,
            )

    return {
        "rework_name": rework_name,
        "task_title": task_title,
        "issue_codes": wanted or ["ALL"],
        "rework_dir": str(rework_dir),
        "rework_zip": str(zip_path),
        "cvat_xml": str(xml_path),
        "manifest_path": str(manifest_path),
        "baseline_dir": str(baseline_dir),
        "n_images": len(copied_images),
        "n_flagged_boxes": manifest["n_flagged_boxes"],
        "cvat_task": cvat_task,
    }


def create_rework_jobs_by_issue(
    qc_result: JobQCResult,
    out_dir: str | Path,
    issue_codes: list[str] | None = None,
    max_images: int | None = None,
    cvat_client: CVATLocalClient | None = None,
    project_id: int | None = None,
    live_push: bool = False,
) -> list[dict]:
    """Tách và tạo nhiều Rework Job riêng biệt — mỗi loại Issue một Task độc lập."""
    if issue_codes:
        codes = [c.strip() for c in issue_codes if c and c.strip()]
    else:
        img_codes = _collect_image_issue_codes(qc_result)
        all_codes = sorted({c for cs in img_codes.values() for c in cs if not c.startswith("FIXED_")})
        codes = all_codes

    created: list[dict] = []
    for code in codes:
        stems = _select_rework_stems(qc_result, max_images=max_images, issue_codes=[code])
        if not stems:
            continue
        info = create_rework_job(
            qc_result,
            out_dir=out_dir,
            max_images=max_images,
            cvat_client=cvat_client,
            project_id=project_id,
            live_push=live_push,
            issue_codes=[code],
        )
        created.append(info)
    return created
