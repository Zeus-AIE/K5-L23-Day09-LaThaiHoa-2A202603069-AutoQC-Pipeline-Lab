"""Kiểm thử tính năng Bonus Core: CVAT Local AutoQC Pre-flight Station."""
from __future__ import annotations

import io
import json
import threading
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cvat_qc.analyzer import analyze_cvat_job
from cvat_qc.connector import (
    CVATLocalClient,
    export_cvat_xml,
    export_yolo11_zip,
    import_yolo11_zip,
)
from cvat_qc.job_creator import create_rework_job, create_rework_jobs_by_issue
from cvat_qc.packager import build_qc_bundle
from cvat_qc.rework_history import compute_rework_diff, load_rework_history, record_rework_history
from cvat_qc.server import StationState, start_server


def test_yolo11_zip_roundtrip_and_zip_slip_guard(make_ds, tmp_path, cfg):
    ds = make_ds(
        images={"img1": 1, "img2": 2},
        labels={"img1": ["0 0.5 0.5 0.2 0.2"], "img2": ["1 0.4 0.4 0.1 0.2"]},
    )
    zip_out = tmp_path / "yolo11.zip"
    export_yolo11_zip(ds, zip_out, cfg["classes"])
    assert zip_out.exists()

    imported = import_yolo11_zip(zip_out, tmp_path / "imported", cfg["classes"])
    assert [p.stem for p in imported.image_files()] == ["img1", "img2"]
    assert imported.read_label_lines("img1") == [(1, "0 0.5 0.5 0.2 0.2")]

    # Chặn Zip Slip
    evil_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../../outside.txt", "evil")
    with pytest.raises(ValueError, match="Zip Slip"):
        import_yolo11_zip(evil_zip, tmp_path / "evil_out", cfg["classes"])


def test_export_cvat_xml_sets_can_review_only_on_flagged_lines(make_ds, tmp_path, cfg):
    ds = make_ds(
        images={"a": 1},
        labels={"a": ["0 0.5 0.5 0.2 0.2", "1 0.2 0.2 0.01 0.01"]},
        size=(640, 480),
    )
    xml_path = tmp_path / "annotations.xml"
    export_cvat_xml(ds, {("a", 2): ["BOX_TINY"]}, cfg["classes"], xml_path)

    root = ET.parse(xml_path).getroot()
    boxes = root.findall(".//image[@name='a.jpg']/box")
    assert len(boxes) == 2
    assert boxes[0].attrib["label"] == "car"
    assert boxes[0].find("./attribute[@name='can_review']").text == "false"
    assert boxes[1].attrib["label"] == "person"
    assert boxes[1].find("./attribute[@name='can_review']").text == "true"


def test_analyze_rework_and_bundle_end_to_end(make_ds, tmp_path, cfg):
    ds = make_ds(
        images={"good": 1, "tiny": 2, "missing_lbl": 3, "bad_line": 4},
        labels={
            "good": ["0 0.5 0.5 0.3 0.3"],
            "tiny": ["1 0.5 0.5 0.01 0.3"],
            "bad_line": ["93 0.5 0.5 0.2 0.2", "1 0.5 0.5 0.2 0.3"],
        },
        size=(320, 240),
    )
    res = analyze_cvat_job(
        ds.root,
        job_id="job_99",
        annotator="Tester <script>alert(1)</script>",
        cfg=cfg,
        work_dir=tmp_path / "station_work",
        use_second_opinion=False,
    )
    assert res.n_scanned == 4
    assert res.verdict["decision"] == "Rework"
    assert ("tiny", 1) in res.flagged_lines
    assert "BOX_TINY" in res.flagged_lines[("tiny", 1)]

    rework = create_rework_job(res, tmp_path / "station" / "rework")
    assert Path(rework["rework_zip"]).exists()
    assert rework["n_images"] >= 2
    with zipfile.ZipFile(rework["rework_zip"], "r") as zf:
        names = set(zf.namelist())
        assert {"obj.names", "obj.data", "train.txt", "cvat_annotations_can_review.xml", "rework_manifest.json"} <= names

    bundle = build_qc_bundle(res, tmp_path / "station", top_viz=3, rework_info=rework)
    html_text = Path(bundle["html_report"]).read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html_text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_text
    assert Path(bundle["handover_zip"]).exists()
    with zipfile.ZipFile(bundle["handover_zip"], "r") as zf:
        bnames = set(zf.namelist())
        assert "qc_report.html" in bnames
        assert "qc_report.md" in bnames
        assert Path(rework["rework_zip"]).name in bnames


def test_cvat_local_client_and_dashboard_server(make_ds, tmp_path, cfg):
    ds = make_ds(images={"img1": 1}, labels={"img1": ["0 0.5 0.5 0.2 0.2"]}, size=(320, 240))
    zip_bytes_io = io.BytesIO()
    tmp_zip = tmp_path / "mock_cvat_export.zip"
    export_yolo11_zip(ds, tmp_zip, cfg["classes"])
    zip_bytes = tmp_zip.read_bytes()
    _ = zip_bytes_io

    calls: list[tuple[str, str]] = []

    class MockCVATHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            calls.append(("GET", self.path))
            if self.path.startswith("/api/jobs/42/dataset"):
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.end_headers()
                self.wfile.write(zip_bytes)
                return
            if self.path == "/api/jobs/42":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"id": 42, "task_id": 7}).encode("utf-8"))
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            calls.append(("POST", self.path))
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length:
                self.rfile.read(length)
            if self.path == "/api/tasks":
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"id": 777, "name": "Rework"}).encode("utf-8"))
                return
            if self.path.startswith("/api/tasks/777/"):
                self.send_response(202)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):  # noqa: A002
            return

    mock_srv = ThreadingHTTPServer(("127.0.0.1", 0), MockCVATHandler)
    t = threading.Thread(target=mock_srv.serve_forever, daemon=True)
    t.start()
    try:
        base_url = f"http://127.0.0.1:{mock_srv.server_port}"
        client = CVATLocalClient(base_url, token="test-token")
        assert client.get_job(42)["task_id"] == 7
        pulled_ds = client.export_job_dataset(42, tmp_path / "pulled", cfg["classes"])
        assert [p.stem for p in pulled_ds.image_files()] == ["img1"]

        xml_p = tmp_path / "push.xml"
        export_cvat_xml(pulled_ds, {("img1", 1): ["POSSIBLE_WRONG_CLASS"]}, cfg["classes"], xml_p)
        created = client.create_rework_task("Rework 42", pulled_ds.image_files(), annotations_xml_path=xml_p)
        assert created["id"] == 777
        assert any(p.startswith("/api/tasks/777/data") for _, p in calls)
        assert any(p.startswith("/api/tasks/777/annotations") for _, p in calls)
    finally:
        mock_srv.shutdown()
        mock_srv.server_close()

    # Kiểm tra Web Dashboard Server + path traversal defense
    ds_ui = make_ds(images={"u1": 1}, labels={"u1": ["1 0.5 0.5 0.01 0.3"]}, size=(320, 240))
    state = StationState(ds_ui.root, "ui_1", "Hoa", tmp_path / "ui_out")
    state.run_all()
    ui_srv = start_server(state, port=0)
    t2 = threading.Thread(target=ui_srv.serve_forever, daemon=True)
    t2.start()
    try:
        ui_url = f"http://127.0.0.1:{ui_srv.server_port}"
        with urllib.request.urlopen(f"{ui_url}/") as r:
            assert r.status == 200
            assert r.headers.get("Access-Control-Allow-Origin") == "*"
            assert r.headers.get("Cross-Origin-Resource-Policy") == "cross-origin"
            assert "AutoQC Pre-flight Station" in r.read().decode("utf-8")
        with urllib.request.urlopen(f"{ui_url}/api/summary") as r:
            summary = json.loads(r.read().decode("utf-8"))
            assert summary["job_id"] == "ui_1"
        with urllib.request.urlopen(f"{ui_url}/download/handover") as r:
            assert r.status == 200 and len(r.read()) > 100
        with urllib.request.urlopen(f"{ui_url}/viz/u1.png") as r:
            assert r.status == 200 and r.headers.get("Content-Type") == "image/png"
        with urllib.request.urlopen(f"{ui_url}/api/rework-history") as r:
            assert r.status == 200 and json.loads(r.read().decode("utf-8"))["ok"] is True
        with urllib.request.urlopen(f"{ui_url}/download/rework-history") as r:
            assert r.status == 200 and "change_type" in r.read().decode("utf-8")
    finally:
        ui_srv.shutdown()
        ui_srv.server_close()


def test_create_rework_by_issue_and_rework_history(make_ds, tmp_path, cfg):
    ds = make_ds(
        images={"img_tiny": 1, "img_huge": 2, "img_clean": 3},
        labels={
            "img_tiny": ["1 0.5 0.5 0.01 0.3"],
            "img_huge": ["0 0.5 0.5 0.99 0.99"],
            "img_clean": ["0 0.5 0.5 0.2 0.2"],
        },
        size=(320, 240),
    )
    res = analyze_cvat_job(ds.root, "job_split", "Hoa", cfg=cfg, work_dir=tmp_path / "w", use_second_opinion=False)
    tiny_only = create_rework_job(res, tmp_path / "rw_tiny", issue_codes=["BOX_TINY"])
    assert tiny_only["issue_codes"] == ["BOX_TINY"]
    assert tiny_only["n_images"] == 1
    assert tiny_only["n_flagged_boxes"] == 1

    split_jobs = create_rework_jobs_by_issue(res, tmp_path / "rw_split", issue_codes=["BOX_TINY", "BOX_HUGE"])
    assert len(split_jobs) == 2
    assert {tuple(j["issue_codes"]) for j in split_jobs} == {("BOX_TINY",), ("BOX_HUGE",)}

    # Kiểm tra đối chiếu lịch sử chỉnh sửa Rework (CLASS_CHANGED, BOX_ADJUSTED, BOX_ADDED, BOX_DELETED)
    before_ds = make_ds(
        images={"a": 1, "b": 2},
        labels={
            "a": ["2 0.5 0.5 0.3 0.3", "1 0.2 0.2 0.1 0.1"],  # 2=truck, 1=person
            "b": ["0 0.5 0.5 0.4 0.4"],
        },
        size=(320, 240),
    )
    # Copy before_ds trước vì make_ds ghi vào cùng tmp_path / "ds"
    import shutil
    before_copy = tmp_path / "before_ds"
    shutil.copytree(before_ds.root, before_copy)
    from pipeline.common import Dataset
    b_ds = Dataset(before_copy, cfg["classes"])

    after_ds = make_ds(
        images={"a": 1, "b": 2},
        labels={
            "a": ["3 0.5 0.5 0.3 0.3", "0 0.8 0.8 0.12 0.12"],  # đổi truck->bus; xoá person; thêm car
            "b": ["0 0.5 0.5 0.28 0.28"],                        # chỉnh kích thước box
        },
        size=(320, 240),
    )
    diff = compute_rework_diff(b_ds, after_ds, cfg["classes"])
    assert diff["counts"] == {"CLASS_CHANGED": 1, "BOX_ADJUSTED": 1, "BOX_ADDED": 1, "BOX_DELETED": 1}

    hdir = tmp_path / "history"
    entry = record_rework_history(hdir, "cvat_30", "cvat_31", b_ds, after_ds, cfg["classes"], editor="Hoa", issue_filter="BOX_TINY")
    assert entry["revision"] == 1 and entry["n_changes"] == 4
    hist = load_rework_history(hdir)
    assert len(hist) == 1
    csv_text = (hdir / "rework_history.csv").read_text(encoding="utf-8")
    assert "CLASS_CHANGED" in csv_text and "BOX_ADDED" in csv_text and "BOX_DELETED" in csv_text and "BOX_ADJUSTED" in csv_text
