#!/usr/bin/env python3
"""Cài đặt Plugin nút `AutoQC Station` vào container `cvat_ui` và tạo Task mẫu Day 09 trên CVAT Local."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cvat_qc.connector import CVATLocalClient, resolve_cvat_token  # noqa: E402
from pipeline.common import Dataset, load_config  # noqa: E402

PLUGIN_TAG = '<script defer="defer" src="/assets/autoqc-plugin.js?v=4"></script>'


def install_ui_plugin(container: str = "cvat_ui") -> None:
    """Chép `cvat_plugin.js` vào container `cvat_ui` và gắn thẻ `<script>` vào `index.html`."""
    plugin_src = ROOT / "cvat_qc" / "static" / "cvat_plugin.js"
    subprocess.run(
        ["docker", "cp", str(plugin_src), f"{container}:/usr/share/nginx/html/assets/autoqc-plugin.js"],
        check=True,
    )
    html_raw = subprocess.check_output(
        ["docker", "exec", container, "cat", "/usr/share/nginx/html/index.html"],
        text=True,
    )
    import re
    cleaned = re.sub(r'<script[^>]*src="/assets/autoqc-plugin\.js[^"]*"[^>]*></script>', "", html_raw)
    if PLUGIN_TAG not in cleaned:
        if "</body>" in cleaned:
            html_new = cleaned.replace("</body>", f"{PLUGIN_TAG}</body>")
        else:
            html_new = cleaned + PLUGIN_TAG
        import tempfile
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".html", delete=False) as tf:
            tf.write(html_new)
            tmp_path = tf.name
        try:
            subprocess.run(
                ["docker", "cp", tmp_path, f"{container}:/usr/share/nginx/html/index.html"],
                check=True,
            )
            subprocess.run(
                ["docker", "exec", "-u", "0", container, "chmod", "644", "/usr/share/nginx/html/index.html", "/usr/share/nginx/html/assets/autoqc-plugin.js"],
                check=False,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    print(f"[CVAT UI Plugin] Đã cài đặt `/assets/autoqc-plugin.js` vào container `{container}`.")


def seed_day09_demo_job(cvat_url: str = "http://localhost:8080", n_images: int = 30) -> dict:
    """Tạo Project & Task mẫu từ `data/raw` lên CVAT Local để kiểm chứng trực tiếp quy trình AutoQC."""
    tok = resolve_cvat_token()
    if not tok:
        raise RuntimeError("Không lấy được API Token của CVAT Local.")
    client = CVATLocalClient(cvat_url, token=tok)
    proj = client.ensure_project("day09-2A202603069-AutoQC", ROOT / "cvat" / "labels.json")

    task_name = f"day09-raw-sample-{n_images}"
    existing = client.get_json(f"/api/tasks?page_size=50").get("results", [])
    for t in existing:
        if t.get("name") == task_name and t.get("project_id") == proj["id"]:
            jobs = client.get_json(f"/api/jobs?task_id={t['id']}").get("results", [])
            jid = jobs[0]["id"] if jobs else None
            print(f"[CVAT Demo] Đã tồn tại Task #{t['id']} ({task_name}), Job #{jid}")
            return {"project_id": proj["id"], "task_id": t["id"], "job_id": jid, "url": f"{cvat_url}/tasks/{t['id']}/jobs/{jid}"}

    cfg = load_config()
    raw_ds = Dataset(ROOT / "data/raw", cfg["classes"])
    # Chọn 30 ảnh đầu (gồm cả ảnh sạch, ảnh lỗi format/box/thiếu nhãn/nhầm lớp từ data/raw)
    demo_dir = ROOT / "work" / "_seed_demo"
    if demo_dir.exists():
        import shutil
        shutil.rmtree(demo_dir)
    (demo_dir / "images").mkdir(parents=True)
    (demo_dir / "labels").mkdir(parents=True)
    # Lọc các ảnh mở được bằng Pillow (ảnh hỏng không upload được lên CVAT chunker)
    for p in raw_ds.image_files():
        if len(list((demo_dir / "images").iterdir())) >= n_images:
            break
        if raw_ds.image_size(p.stem) is None:
            continue
        import shutil
        shutil.copy2(p, demo_dir / "images" / p.name)
        lp = raw_ds.label_path(p.stem)
        if lp.exists():
            shutil.copy2(lp, demo_dir / "labels" / lp.name)

    demo_ds = Dataset(demo_dir, cfg["classes"])
    res = client.create_rework_task_live(
        name=task_name,
        ds=demo_ds,
        flagged_lines={},
        classes=cfg["classes"],
        project_id=proj["id"],
        labels_schema_path=ROOT / "cvat" / "labels.json",
    )
    import shutil
    shutil.rmtree(demo_dir, ignore_errors=True)
    print(f"[CVAT Demo] Đã tạo Project `{proj['name']}` và Task `{task_name}` -> {res.get('url')}")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cvat-url", default="http://localhost:8080")
    ap.add_argument("--container", default="cvat_ui")
    ap.add_argument("--seed-demo", action="store_true", help="Tạo thêm Project/Job mẫu Day 09 trên CVAT Local")
    ap.add_argument("--demo-count", type=int, default=30)
    a = ap.parse_args()

    install_ui_plugin(a.container)
    if a.seed_demo:
        seed_day09_demo_job(a.cvat_url, a.demo_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
