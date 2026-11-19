#!/usr/bin/env python3
"""CLI & Web Server Trạm Kiểm Soát Nhanh AutoQC Tích Hợp CVAT Local.

Ví dụ:
    python scripts/cvat_autoqc.py --job-id job_raw_224 --annotator "La Thai Hoa" --src data/raw --out work/cvat_station --serve
    python scripts/cvat_autoqc.py --job-id 29 --cvat-url http://localhost:8080 --out work/cvat_station --serve
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cvat_qc.connector import CVATLocalClient, resolve_cvat_token  # noqa: E402
from cvat_qc.job_creator import create_rework_job, create_rework_jobs_by_issue  # noqa: E402
from cvat_qc.packager import build_qc_bundle  # noqa: E402
from cvat_qc.server import StationState, start_server  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job-id", default="job_raw_224", help="Mã CVAT Job cần kiểm tra")
    ap.add_argument("--annotator", default="La Thai Hoa", help="Tên người gán nhãn")
    ap.add_argument("--src", help="Thư mục dataset YOLO hoặc file .zip YOLO 1.1 export từ CVAT")
    ap.add_argument("--cvat-url", default="http://localhost:8080", help="URL CVAT Local server")
    ap.add_argument("--cvat-token", help="API Token của CVAT Local (tự lấy nếu để trống)")
    ap.add_argument("--flag-cvat", action="store_true", help="Gắn thẳng can_review=true & tạo Issues lên CVAT Job")
    ap.add_argument("--push-rework", action="store_true", help="Tạo Task/Job Rework mới trên CVAT Local qua REST API")
    ap.add_argument("--rework-issues", help="Lọc danh sách mã Issue để tạo Rework Job (vd: POSSIBLE_WRONG_CLASS,POSSIBLE_MISSING)")
    ap.add_argument("--split-rework-by-issue", action="store_true", help="Tách riêng mỗi loại Issue thành một Rework Task độc lập")
    ap.add_argument("--max-rework", type=int, default=None, help="Giới hạn số ảnh tối đa đưa vào Rework Sub-Job")
    ap.add_argument("--top-viz", type=int, default=12, help="Số ảnh vẽ trực quan đưa vào báo cáo QC")
    ap.add_argument("--out", default="work/cvat_station", help="Thư mục đầu ra")
    ap.add_argument("--serve", action="store_true", help="Mở Web Dashboard cục bộ")
    ap.add_argument("--port", type=int, default=8765, help="Cổng Web Dashboard")
    a = ap.parse_args(argv)

    out_dir = ROOT / a.out
    tok = resolve_cvat_token(a.cvat_token)
    state = StationState(
        source=ROOT / "data/raw",
        job_id=str(a.job_id),
        annotator=a.annotator,
        out_dir=out_dir,
        top_viz=a.top_viz,
        cvat_url=a.cvat_url,
        cvat_token=tok,
    )

    if a.src:
        source = Path(a.src) if Path(a.src).is_absolute() else (ROOT / a.src)
        state.source = source
        state.run_all()
    else:
        state.scan_live_cvat_job(a.job_id, cvat_url=a.cvat_url, token=tok)

    client = CVATLocalClient(a.cvat_url, token=tok) if tok else None
    if a.flag_cvat and client and state.qc_result.live_meta:
        flag_res = client.apply_flags_to_cvat_job(state.qc_result.live_meta["job_id"], state.qc_result, create_issues=True)
        print(f"  [CVAT Live] Gắn can_review={flag_res['updated_shapes']} box, tạo {flag_res['issues_created']} Issues")

    issue_list = [c.strip() for c in (a.rework_issues or "").split(",") if c.strip()]
    if a.split_rework_by_issue:
        created = create_rework_jobs_by_issue(
            state.qc_result,
            out_dir / "rework",
            issue_codes=issue_list or None,
            max_images=a.max_rework,
            cvat_client=client if a.push_rework else None,
            live_push=bool(a.push_rework),
        )
        print(f"  [Rework Split] Đã tạo {len(created)} Rework Job theo từng loại Issue:")
        for c in created:
            print(f"    - {c['issue_codes']}: {c['n_images']} ảnh -> {c['rework_zip']}")
    elif a.max_rework is not None or issue_list or (a.push_rework and client is not None):
        state.rework_info = create_rework_job(
            state.qc_result,
            out_dir / "rework",
            max_images=a.max_rework,
            cvat_client=client if a.push_rework else None,
            live_push=bool(a.push_rework),
            issue_codes=issue_list or None,
        )
        state.bundle_info = build_qc_bundle(state.qc_result, out_dir, top_viz=a.top_viz, rework_info=state.rework_info)

    res = state.qc_result
    print(f"[AutoQC Station] Job: {res.job_id} | Annotator: {res.annotator}")
    print(f"  Quality Score : {res.quality_score}/100 | Gate: {res.verdict['decision'].upper()}")
    print(f"  Ảnh nhận/quét : {res.n_accepted}/{res.n_scanned} (loại {res.n_rejected})")
    print(f"  Hàng đợi QC   : {len(res.review_queue)} ảnh | Box gắn can_review: {state.rework_info['n_flagged_boxes']}")
    print(f"  Báo cáo HTML  : {state.bundle_info['html_report']}")
    print(f"  Rework ZIP    : {state.rework_info['rework_zip']}")
    print(f"  QC Handover   : {state.bundle_info['handover_zip']}")
    if state.rework_info.get("cvat_task"):
        print(f"  CVAT Rework   : {state.rework_info['cvat_task'].get('url')}")

    if a.serve:
        srv = start_server(state, port=a.port)
        print(f"Đang mở Trạm Kiểm Soát Nhanh tại http://127.0.0.1:{a.port} (Ctrl+C để dừng)...")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
