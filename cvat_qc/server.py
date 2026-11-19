"""Web Server cục bộ phục vụ Trạm Kiểm Soát Nhanh CVAT AutoQC & API cho Plugin CVAT UI."""
from __future__ import annotations

import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pipeline.common import ROOT, Dataset, load_config

from .analyzer import analyze_cvat_job
from .connector import CVATLocalClient, resolve_cvat_token
from .job_creator import create_rework_job, create_rework_jobs_by_issue
from .packager import build_qc_bundle, render_single_viz
from .rework_history import load_rework_history, record_rework_history, save_baseline_snapshot


class StationState:
    """Trạng thái phiên chạy của Trạm Kiểm Soát Nhanh CVAT AutoQC."""

    def __init__(
        self,
        source: str | Path,
        job_id: str,
        annotator: str,
        out_dir: Path,
        top_viz: int = 12,
        cvat_url: str = "http://localhost:8080",
        cvat_token: str | None = None,
    ):
        self.source = source
        self.job_id = job_id
        self.annotator = annotator
        self.out_dir = Path(out_dir)
        self.top_viz = top_viz
        self.cvat_url = cvat_url.rstrip("/")
        self.cvat_token = cvat_token
        self.qc_result = None
        self.rework_info: dict = {}
        self.bundle_info: dict = {}
        self._live_meta: dict | None = None

    @property
    def history_dir(self) -> Path:
        return self.out_dir / "history"

    @property
    def baselines_dir(self) -> Path:
        return self.out_dir / "rework" / "baselines"

    def get_client(self, cvat_url: str | None = None, token: str | None = None) -> CVATLocalClient:
        url = (cvat_url or self.cvat_url).rstrip("/")
        tok = resolve_cvat_token(token or self.cvat_token)
        return CVATLocalClient(url, token=tok)

    def run_all(self) -> None:
        self.qc_result = analyze_cvat_job(
            self.source,
            job_id=self.job_id,
            annotator=self.annotator,
            work_dir=self.out_dir / "work_analysis",
            live_meta=self._live_meta,
        )
        self.rework_info = create_rework_job(qc_result=self.qc_result, out_dir=self.out_dir / "rework")
        self.bundle_info = build_qc_bundle(self.qc_result, self.out_dir, top_viz=self.top_viz, rework_info=self.rework_info)

    def scan_live_cvat_job(self, cvat_job_id: int | str, cvat_url: str | None = None, token: str | None = None) -> None:
        client = self.get_client(cvat_url, token)
        pull_dir = self.out_dir / "pulled_live" / f"job_{cvat_job_id}"
        classes = load_config()["classes"]
        live_meta = client.pull_job_live(cvat_job_id, pull_dir, classes)
        # Lưu snapshot baseline lần đầu của Job để về sau theo dõi lịch sử sửa đổi
        base_dir = self.baselines_dir / f"cvat_{cvat_job_id}"
        if not base_dir.exists():
            save_baseline_snapshot(
                pull_dir,
                base_dir,
                meta={"source_job_id": f"cvat_{cvat_job_id}", "cvat_job_id": int(cvat_job_id), "issue_codes": ["ORIGINAL"]},
            )
        self.source = pull_dir
        self.job_id = f"cvat_{cvat_job_id}"
        self.annotator = live_meta.get("assignee") or "cvat_annotator"
        self._live_meta = live_meta
        self.run_all()

    def list_baselines(self) -> list[dict]:
        if not self.baselines_dir.exists():
            return []
        items = []
        for p in sorted(self.baselines_dir.iterdir()):
            if not p.is_dir():
                continue
            meta_p = p / "baseline_meta.json"
            meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
            items.append({"key": p.name, **meta})
        return items


def make_handler(state: StationState):
    """Tạo HTTP Request Handler gắn với trạng thái trạm AutoQC (có CORS + CORP cho CVAT UI)."""

    class StationHandler(BaseHTTPRequestHandler):
        def _common_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Cross-Origin-Resource-Policy", "cross-origin")

        def _send_bytes(self, status: int, body: bytes, content_type: str, download_name: str | None = None) -> None:
            self.send_response(status)
            self._common_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if download_name:
                self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: dict) -> None:
            self._send_bytes(status, json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            self._common_headers()
            self.end_headers()

        def do_GET(self):  # noqa: N802
            u = urllib.parse.urlparse(self.path)
            path = u.path
            qs = urllib.parse.parse_qs(u.query)

            if path in ("/", "/index.html"):
                dash = Path(state.bundle_info["dashboard_html"])
                self._send_bytes(200, dash.read_bytes(), "text/html; charset=utf-8")
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self._common_headers()
                self.end_headers()
                return
            if path == "/api/summary":
                self._send_json(200, state.qc_result.to_summary_dict())
                return
            if path == "/api/jobs":
                try:
                    client = state.get_client()
                    self._send_json(200, {"ok": True, "jobs": client.list_jobs()})
                except Exception as exc:  # noqa: BLE001
                    self._send_json(200, {"ok": False, "jobs": [], "error": str(exc)})
                return
            if path == "/api/rework-history":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "history": load_rework_history(state.history_dir),
                        "baselines": state.list_baselines(),
                    },
                )
                return
            if path == "/api/frame-detail":
                stem = (qs.get("image") or [""])[0]
                if not stem or state.qc_result is None:
                    self._send_json(400, {"ok": False, "message": "Thiếu tham số image"})
                    return
                res = state.qc_result
                issues = [
                    {"severity": i.severity, "code": i.code, "line": i.line, "message": i.message}
                    for i in (res.intake_issues + res.v2_issues)
                    if i.image == stem
                ]
                classes = res.cfg["classes"]
                sugs = [
                    {**s, "class_name": classes[s["class_id"]] if s["class_id"] < len(classes) else str(s["class_id"])}
                    for s in res.model_suggestions.get(stem, [])
                ]
                finfo = (res.live_meta or {}).get("frames_by_stem", {}).get(stem)
                frame_no = finfo["frame"] if finfo else None
                task_id = (res.live_meta or {}).get("task_id")
                cvat_job_id = (res.live_meta or {}).get("job_id")
                cvat_url = (res.live_meta or {}).get("cvat_url", state.cvat_url)
                cvat_frame_url = (
                    f"{cvat_url}/tasks/{task_id}/jobs/{cvat_job_id}?frame={frame_no}"
                    if task_id is not None and cvat_job_id is not None and frame_no is not None
                    else None
                )
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "image": stem,
                        "filename": finfo["name"] if finfo else f"{stem}.jpg",
                        "frame": frame_no,
                        "cvat_frame_url": cvat_frame_url,
                        "issues": issues,
                        "suggestions": sugs,
                    },
                )
                return
            if path == "/download/rework":
                zp = Path(state.rework_info["rework_zip"])
                self._send_bytes(200, zp.read_bytes(), "application/zip", download_name=zp.name)
                return
            if path == "/download/handover":
                zp = Path(state.bundle_info["handover_zip"])
                self._send_bytes(200, zp.read_bytes(), "application/zip", download_name=zp.name)
                return
            if path == "/download/rework-history":
                csv_p = state.history_dir / "rework_history.csv"
                if not csv_p.exists():
                    self._send_bytes(200, b"revision,timestamp,source_job_id,target_job_id,editor,issue_filter,image,change_type,iou,before,after,detail\n", "text/csv; charset=utf-8", download_name="rework_history.csv")
                    return
                self._send_bytes(200, csv_p.read_bytes(), "text/csv; charset=utf-8", download_name="rework_history.csv")
                return
            if path.startswith("/viz/"):
                fname = Path(path.removeprefix("/viz/")).name
                if not fname.lower().endswith(".png"):
                    self._send_bytes(404, b"Not found", "text/plain; charset=utf-8")
                    return
                viz_root = Path(state.bundle_info["viz_dir"]).resolve()
                target = (viz_root / fname).resolve()
                if not target.is_relative_to(viz_root):
                    self._send_bytes(404, b"Not found", "text/plain; charset=utf-8")
                    return
                if not target.is_file() and state.qc_result is not None:
                    render_single_viz(state.qc_result, target.stem, target)
                if not target.is_file():
                    self._send_bytes(404, b"Not found", "text/plain; charset=utf-8")
                    return
                self._send_bytes(200, target.read_bytes(), "image/png")
                return
            self._send_bytes(404, b"Not found", "text/plain; charset=utf-8")

        def do_POST(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path).path
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req_data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                req_data = {}

            if parsed == "/api/rerun":
                state.run_all()
                self._send_json(200, {"ok": True, "verdict": state.qc_result.verdict, "summary": state.qc_result.to_summary_dict()})
                return

            if parsed == "/api/scan":
                try:
                    mode = req_data.get("mode", "local")
                    if mode == "cvat":
                        state.scan_live_cvat_job(req_data["job_id"], req_data.get("cvat_url"), req_data.get("token"))
                    else:
                        src_rel = req_data.get("src", "data/raw")
                        src_path = (ROOT / src_rel).resolve()
                        if not src_path.is_relative_to(ROOT.resolve()):
                            self._send_json(400, {"ok": False, "message": "Đường dẫn nguồn ngoài phạm vi repo"})
                            return
                        state.source = src_path
                        state.job_id = str(req_data.get("job_id", "job_raw_224"))
                        state.annotator = str(req_data.get("annotator", "La Thai Hoa"))
                        state._live_meta = None
                        state.run_all()
                    self._send_json(200, {"ok": True, "summary": state.qc_result.to_summary_dict()})
                except Exception as exc:  # noqa: BLE001
                    self._send_json(500, {"ok": False, "message": str(exc)})
                return

            if parsed == "/api/cvat/flag":
                try:
                    if not state.qc_result or not state.qc_result.live_meta:
                        self._send_json(400, {"ok": False, "message": "Job hiện tại là dataset cục bộ, hãy chọn 1 CVAT Job live trước khi gắn cờ."})
                        return
                    cvat_jid = state.qc_result.live_meta["job_id"]
                    client = state.get_client(req_data.get("cvat_url"), req_data.get("token"))
                    res = client.apply_flags_to_cvat_job(cvat_jid, state.qc_result, create_issues=True)
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "message": f"Đã gắn can_review=true cho {res['updated_shapes']} box và tạo {res['issues_created']} CVAT Issues trên Job #{cvat_jid}!",
                            **res,
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self._send_json(502, {"ok": False, "message": f"Lỗi khi cập nhật CVAT Job: {exc}"})
                return

            if parsed in ("/api/cvat/create-rework", "/api/push-cvat"):
                try:
                    client = state.get_client(req_data.get("cvat_url"), req_data.get("token"))
                    max_imgs = req_data.get("max_images")
                    max_val = int(max_imgs) if max_imgs is not None else None
                    issue_codes = req_data.get("issue_codes") or []
                    if isinstance(issue_codes, str):
                        issue_codes = [c.strip() for c in issue_codes.split(",") if c.strip()]
                    split_by_code = bool(req_data.get("split_by_code", False))

                    if split_by_code:
                        created_list = create_rework_jobs_by_issue(
                            state.qc_result,
                            state.out_dir / "rework",
                            issue_codes=issue_codes or None,
                            max_images=max_val,
                            cvat_client=client,
                            live_push=True,
                        )
                        if created_list:
                            state.rework_info = created_list[0]
                            state.bundle_info = build_qc_bundle(state.qc_result, state.out_dir, top_viz=state.top_viz, rework_info=state.rework_info)
                        self._send_json(
                            200,
                            {
                                "ok": True,
                                "split_by_code": True,
                                "tasks": [
                                    {
                                        "issue_codes": x["issue_codes"],
                                        "n_images": x["n_images"],
                                        "n_flagged_boxes": x["n_flagged_boxes"],
                                        "cvat_task": x.get("cvat_task"),
                                    }
                                    for x in created_list
                                ],
                                "message": f"Đã tách và tạo {len(created_list)} Rework Task theo từng loại Issue trên CVAT Local!",
                            },
                        )
                        return

                    res = create_rework_job(
                        state.qc_result,
                        state.out_dir / "rework",
                        max_images=max_val,
                        cvat_client=client,
                        live_push=True,
                        issue_codes=issue_codes or None,
                    )
                    state.rework_info = res
                    state.bundle_info = build_qc_bundle(state.qc_result, state.out_dir, top_viz=state.top_viz, rework_info=res)
                    task_url = (res.get("cvat_task") or {}).get("url")
                    code_txt = f" [{', '.join(res['issue_codes'])}]" if res["issue_codes"] != ["ALL"] else ""
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "message": f"Đã tạo Rework Task/Job{code_txt} trên CVAT Local: {task_url}",
                            "cvat_task": res.get("cvat_task"),
                            "issue_codes": res["issue_codes"],
                            "n_images": res["n_images"],
                            "n_flagged_boxes": res["n_flagged_boxes"],
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self._send_json(502, {"ok": False, "message": f"Không kết nối được CVAT Local: {exc}"})
                return

            if parsed == "/api/cvat/track-rework":
                try:
                    client = state.get_client(req_data.get("cvat_url"), req_data.get("token"))
                    target_jid = req_data.get("cvat_job_id")
                    if target_jid is None:
                        if state.qc_result and state.qc_result.live_meta:
                            target_jid = state.qc_result.live_meta["job_id"]
                        else:
                            self._send_json(400, {"ok": False, "message": "Hãy chọn một CVAT Job để ghi lịch sử chỉnh sửa Rework."})
                            return
                    target_jid = int(target_jid)
                    baseline_key = str(req_data.get("baseline_key") or f"cvat_{target_jid}")
                    base_dir = (state.baselines_dir / Path(baseline_key).name).resolve()
                    if not base_dir.is_relative_to(state.baselines_dir.resolve()) or not base_dir.is_dir():
                        self._send_json(404, {"ok": False, "message": f"Chưa có snapshot baseline '{baseline_key}' để đối chiếu."})
                        return

                    after_dir = state.out_dir / "pulled_after" / f"job_{target_jid}"
                    classes = load_config()["classes"]
                    after_meta = client.pull_job_live(target_jid, after_dir, classes)
                    active_cls = after_meta.get("active_classes") or classes

                    before_ds = Dataset(base_dir, active_cls)
                    after_ds = Dataset(after_dir, active_cls)
                    base_meta_p = base_dir / "baseline_meta.json"
                    base_meta = json.loads(base_meta_p.read_text(encoding="utf-8")) if base_meta_p.exists() else {}
                    issue_filter = "+".join(base_meta.get("issue_codes", ["ALL"]))

                    entry = record_rework_history(
                        state.history_dir,
                        source_job_id=base_meta.get("source_job_id", state.job_id),
                        target_job_id=f"cvat_{target_jid}",
                        before_ds=before_ds,
                        after_ds=after_ds,
                        classes=active_cls,
                        editor=after_meta.get("assignee") or "cvat_editor",
                        issue_filter=issue_filter,
                    )
                    # Cập nhật lại baseline mới nhất sau khi đã ghi nhận phiên sửa này (theo dõi luỹ tiến nhiều vòng)
                    if entry["n_changes"] > 0:
                        save_baseline_snapshot(after_dir, base_dir, meta=base_meta)
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "message": f"Phiên #{entry['revision']}: ghi nhận {entry['n_changes']} thay đổi trên {entry['n_images_edited']} ảnh.",
                            "entry": entry,
                            "history": load_rework_history(state.history_dir),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self._send_json(500, {"ok": False, "message": f"Lỗi khi ghi lịch sử Rework: {exc}"})
                return

            self._send_bytes(404, b"Not found", "text/plain; charset=utf-8")

        def log_message(self, format, *args):  # noqa: A002
            return

    return StationHandler


def start_server(state: StationState, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Khởi tạo HTTP server cho trạm kiểm soát nhanh CVAT AutoQC."""
    return ThreadingHTTPServer((host, port), make_handler(state))
