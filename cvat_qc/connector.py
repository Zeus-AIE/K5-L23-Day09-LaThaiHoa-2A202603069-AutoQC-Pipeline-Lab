"""Kết nối CVAT Local REST API v2 và chuyển đổi định dạng YOLO 1.1 / CVAT 1.1 XML."""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import yaml

from pipeline.common import IMG_EXTS, ROOT, Dataset, parse_yolo_line


def resolve_cvat_token(explicit_token: str | None = None) -> str | None:
    """Lấy API Token CVAT từ tham số, biến môi trường, hoặc `docker exec cvat_server`."""
    if explicit_token:
        return explicit_token.strip()
    env_tok = os.environ.get("CVAT_TOKEN") or os.environ.get("CVAT_API_TOKEN")
    if env_tok:
        return env_tok.strip()
    try:
        cmd = [
            "docker",
            "exec",
            "cvat_server",
            "python3",
            "manage.py",
            "shell",
            "-c",
            (
                "from django.contrib.auth import get_user_model; "
                "from rest_framework.authtoken.models import Token; "
                "u = get_user_model().objects.filter(is_superuser=True).first() or get_user_model().objects.first(); "
                "print(Token.objects.get_or_create(user=u)[0].key if u else '')"
            ),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
            if lines and len(lines[-1]) >= 20 and " " not in lines[-1]:
                return lines[-1]
    except Exception:
        pass
    return None


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """Giải nén zip an toàn, chặn lỗ hổng Zip Slip."""
    dest_resolved = dest_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            target = (dest_dir / member.filename).resolve()
            if not target.is_relative_to(dest_resolved):
                raise ValueError(f"Đường dẫn zip không hợp lệ (Zip Slip): {member.filename}")
        zf.extractall(dest_dir)


def import_yolo11_zip(zip_path: str | Path, dest_dir: str | Path, classes: list[str]) -> Dataset:
    """Chuyển gói YOLO 1.1 (export từ CVAT) thành thư mục Dataset chuẩn YOLO."""
    zip_path = Path(zip_path)
    dest = Path(dest_dir)
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "images").mkdir(parents=True)
    (dest / "labels").mkdir(parents=True)

    extract_tmp = dest / "_extract"
    extract_tmp.mkdir(parents=True)
    _safe_extract_zip(zip_path, extract_tmp)

    names_file = next(extract_tmp.rglob("obj.names"), None)
    parsed_names = list(classes)
    if names_file is not None:
        raw_names = [ln.strip() for ln in names_file.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        if raw_names:
            parsed_names = raw_names

    for p in sorted(extract_tmp.rglob("*")):
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf in IMG_EXTS:
            shutil.copy2(p, dest / "images" / p.name)
        elif suf == ".txt" and p.name not in ("train.txt", "valid.txt", "test.txt"):
            shutil.copy2(p, dest / "labels" / p.name)

    shutil.rmtree(extract_tmp, ignore_errors=True)
    data_yaml = {"path": ".", "names": dict(enumerate(parsed_names))}
    (dest / "data.yaml").write_text(yaml.safe_dump(data_yaml, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return Dataset(dest, classes)


def prepare_source_dataset(source: str | Path, work_src: str | Path, classes: list[str]) -> Dataset:
    """Chuẩn bị Dataset từ thư mục nguồn hoặc file .zip YOLO 1.1 (không sửa nguồn gốc)."""
    src_path = Path(source)
    work_path = Path(work_src)
    if src_path.is_file() and src_path.suffix.lower() == ".zip":
        return import_yolo11_zip(src_path, work_path, classes)
    if not src_path.is_dir():
        raise FileNotFoundError(f"Không tìm thấy nguồn dữ liệu CVAT: {src_path}")

    if (src_path / "images").is_dir():
        return Dataset(src_path, classes)

    if work_path.exists():
        shutil.rmtree(work_path)
    (work_path / "images").mkdir(parents=True)
    (work_path / "labels").mkdir(parents=True)
    for p in sorted(src_path.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() in IMG_EXTS:
            shutil.copy2(p, work_path / "images" / p.name)
        elif p.suffix.lower() == ".txt" and p.name not in ("train.txt", "valid.txt", "test.txt"):
            shutil.copy2(p, work_path / "labels" / p.name)
    names_file = next(src_path.rglob("obj.names"), None)
    names = classes
    if names_file is not None:
        raw_names = [ln.strip() for ln in names_file.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        if raw_names:
            names = raw_names
    (work_path / "data.yaml").write_text(
        yaml.safe_dump({"path": ".", "names": dict(enumerate(names))}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return Dataset(work_path, classes)


def export_yolo11_zip(
    ds: Dataset,
    out_zip: str | Path,
    classes: list[str],
    include_images: bool = True,
    extra_files: dict[str, str | bytes] | None = None,
) -> Path:
    """Đóng gói Dataset thành file ZIP chuẩn YOLO 1.1 để import trực tiếp vào CVAT."""
    out_zip = Path(out_zip)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    train_entries = []

    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("obj.names", "\n".join(classes) + "\n")
        zf.writestr(
            "obj.data",
            f"classes = {len(classes)}\ntrain = data/train.txt\nnames = data/obj.names\nbackup = backup/\n",
        )
        for img_path in ds.image_files():
            train_entries.append(f"data/obj_train_data/{img_path.name}")
            if include_images:
                zf.write(img_path, f"obj_train_data/{img_path.name}")
            lbl_path = ds.label_path(img_path.stem)
            if lbl_path.exists():
                zf.write(lbl_path, f"obj_train_data/{img_path.stem}.txt")
            else:
                zf.writestr(f"obj_train_data/{img_path.stem}.txt", "")
        zf.writestr("train.txt", "\n".join(train_entries) + ("\n" if train_entries else ""))

        if extra_files:
            for arc_name, payload in extra_files.items():
                zf.writestr(arc_name, payload)

    return out_zip


def export_cvat_xml(
    ds: Dataset,
    flagged_lines: dict[tuple[str, int], list[str]],
    classes: list[str],
    out_xml: str | Path,
    task_name: str = "AutoQC Rework Job",
) -> Path:
    """Sinh file annotations.xml chuẩn CVAT 1.1, tự động gắn attribute `can_review=true` cho các box bị cờ."""
    out_xml = Path(out_xml)
    out_xml.parent.mkdir(parents=True, exist_ok=True)

    root = ET.Element("annotations")
    ET.SubElement(root, "version").text = "1.1"
    meta = ET.SubElement(root, "meta")
    task_el = ET.SubElement(meta, "task")
    ET.SubElement(task_el, "name").text = task_name
    labels_el = ET.SubElement(task_el, "labels")

    for cls_name in classes:
        lbl_el = ET.SubElement(labels_el, "label")
        ET.SubElement(lbl_el, "name").text = cls_name
        ET.SubElement(lbl_el, "type").text = "rectangle"
        attrs_el = ET.SubElement(lbl_el, "attributes")
        attr_el = ET.SubElement(attrs_el, "attribute")
        ET.SubElement(attr_el, "name").text = "can_review"
        ET.SubElement(attr_el, "mutable").text = "False"
        ET.SubElement(attr_el, "input_type").text = "checkbox"
        ET.SubElement(attr_el, "default_value").text = "false"

    for idx, img_path in enumerate(ds.image_files()):
        stem = img_path.stem
        size = ds.image_size(stem)
        if size is None:
            continue
        img_w, img_h = size
        img_el = ET.SubElement(
            root,
            "image",
            {
                "id": str(idx),
                "name": img_path.name,
                "width": str(img_w),
                "height": str(img_h),
            },
        )
        for line_no, raw_ln in ds.read_label_lines(stem) or []:
            try:
                cls_id, cx, cy, bw, bh = parse_yolo_line(raw_ln)
            except ValueError:
                continue
            if not (0 <= cls_id < len(classes)) or bw <= 0 or bh <= 0:
                continue
            xtl = max(0.0, (cx - bw / 2) * img_w)
            ytl = max(0.0, (cy - bh / 2) * img_h)
            xbr = min(float(img_w), (cx + bw / 2) * img_w)
            ybr = min(float(img_h), (cy + bh / 2) * img_h)
            reasons = flagged_lines.get((stem, line_no), [])
            box_el = ET.SubElement(
                img_el,
                "box",
                {
                    "label": classes[cls_id],
                    "occluded": "0",
                    "source": "autoqc" if reasons else "manual",
                    "xtl": f"{xtl:.2f}",
                    "ytl": f"{ytl:.2f}",
                    "xbr": f"{xbr:.2f}",
                    "ybr": f"{ybr:.2f}",
                    "z_order": "0",
                },
            )
            attr_node = ET.SubElement(box_el, "attribute", {"name": "can_review"})
            attr_node.text = "true" if reasons else "false"

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(out_xml, encoding="utf-8", xml_declaration=True)
    return out_xml


class CVATLocalClient:
    """Client giao tiếp trực tiếp CVAT Local REST API v2."""

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.username = username
        self.password = password
        self.timeout = timeout

    def _headers(self, content_type: str | None = "application/json") -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/vnd.cvat+json, application/json, */*"}
        if content_type:
            headers["Content-Type"] = content_type
        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        elif self.username and self.password:
            raw = f"{self.username}:{self.password}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return headers

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str | None = "application/json",
    ) -> tuple[int, bytes, dict[str, str]]:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, data=body, method=method, headers=self._headers(content_type))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"CVAT API lỗi {err.code} tại {method} {path}: {detail}") from err

    def get_json(self, path: str) -> dict:
        _, payload, _ = self._request("GET", path, content_type=None)
        return json.loads(payload.decode("utf-8"))

    def list_projects(self, page_size: int = 50) -> list[dict]:
        return self.get_json(f"/api/projects?page_size={page_size}").get("results", [])

    def ensure_project(self, name: str, labels_schema_path: str | Path | None = None) -> dict:
        for p in self.list_projects():
            if p.get("name") == name:
                return p
        schema_path = Path(labels_schema_path) if labels_schema_path else ROOT / "cvat" / "labels.json"
        labels = json.loads(schema_path.read_text(encoding="utf-8"))
        _, resp, _ = self._request("POST", "/api/projects", body=json.dumps({"name": name, "labels": labels}).encode("utf-8"))
        return json.loads(resp.decode("utf-8"))

    def list_jobs(self, page_size: int = 100) -> list[dict]:
        data = self.get_json(f"/api/jobs?page_size={page_size}")
        jobs = []
        for j in data.get("results", []):
            assignee = j.get("assignee") or {}
            jobs.append(
                {
                    "id": j["id"],
                    "task_id": j.get("task_id"),
                    "task_name": j.get("task_name") or f"Task {j.get('task_id')}",
                    "project_id": j.get("project_id"),
                    "project_name": j.get("project_name") or "",
                    "stage": j.get("stage", ""),
                    "state": j.get("state", ""),
                    "frame_count": j.get("frame_count", 0),
                    "assignee": assignee.get("username", "") if isinstance(assignee, dict) else str(assignee),
                    "url": f"{self.base_url}/tasks/{j.get('task_id')}/jobs/{j['id']}",
                }
            )
        return jobs

    def get_job(self, job_id: int | str) -> dict:
        return self.get_json(f"/api/jobs/{job_id}")

    def export_job_dataset(self, job_id: int | str, out_dir: str | Path, classes: list[str], fmt: str = "YOLO 1.1") -> Dataset:
        """Tải gói dataset + annotations từ CVAT Job qua endpoint export."""
        q = urllib.parse.urlencode({"format": fmt, "action": "download"})
        _, payload, _ = self._request("GET", f"/api/jobs/{job_id}/dataset?{q}", content_type=None)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        zip_path = out_dir / f"cvat_job_{job_id}.zip"
        zip_path.write_bytes(payload)
        return import_yolo11_zip(zip_path, out_dir / "dataset", classes)

    def pull_job_live(self, job_id: int | str, dest_dir: str | Path, project_classes: list[str]) -> dict:
        """Tải trực tiếp frame ảnh, labels và shapes từ CVAT Job live về thư mục Dataset chuẩn YOLO."""
        dest = Path(dest_dir)
        if dest.exists():
            shutil.rmtree(dest)
        (dest / "images").mkdir(parents=True)
        (dest / "labels").mkdir(parents=True)

        job_info = self.get_job(job_id)
        meta = self.get_json(f"/api/jobs/{job_id}/data/meta")
        ann = self.get_json(f"/api/jobs/{job_id}/annotations")
        labels_resp = self.get_json(f"/api/labels?job_id={job_id}&page_size=100").get("results", [])

        label_by_id: dict[int, dict] = {}
        job_classes: list[str] = []
        name_to_idx: dict[str, int] = {}
        for l_obj in labels_resp:
            lname = l_obj["name"]
            can_rev_spec = next((a["id"] for a in l_obj.get("attributes", []) if a.get("name") == "can_review"), None)
            label_by_id[l_obj["id"]] = {"id": l_obj["id"], "name": lname, "can_review_spec_id": can_rev_spec}
            if lname not in name_to_idx:
                name_to_idx[lname] = len(job_classes)
                job_classes.append(lname)

        # Nếu các lớp trong job là tập con của project_classes, ưu tiên dùng đúng thứ tự project_classes
        if job_classes and set(c.lower() for c in job_classes) <= set(c.lower() for c in project_classes):
            active_classes = list(project_classes)
            lower_map = {c.lower(): idx for idx, c in enumerate(active_classes)}
            label_idx_by_id = {lid: lower_map[info["name"].lower()] for lid, info in label_by_id.items()}
        else:
            active_classes = job_classes if job_classes else list(project_classes)
            label_idx_by_id = {lid: name_to_idx[info["name"]] for lid, info in label_by_id.items()}

        start_frame = int(job_info.get("start_frame", 0))
        frames_meta = meta.get("frames", [])
        frame_to_info: dict[int, dict] = {}
        frames_by_stem: dict[str, dict] = {}
        used_stems: dict[str, int] = {}

        for local_idx, fmeta in enumerate(frames_meta):
            frame_no = start_frame + local_idx
            orig_name = Path(fmeta.get("name", f"frame_{frame_no:05d}.jpg")).name
            stem = Path(orig_name).stem
            if stem in used_stems:
                used_stems[stem] += 1
                stem = f"{stem}_{frame_no}"
                orig_name = f"{stem}{Path(orig_name).suffix or '.jpg'}"
            else:
                used_stems[stem] = 1

            _, img_bytes, _ = self._request(
                "GET",
                f"/api/jobs/{job_id}/data?type=frame&quality=compressed&number={frame_no}",
                content_type=None,
            )
            (dest / "images" / orig_name).write_bytes(img_bytes)
            finfo = {
                "frame": frame_no,
                "name": orig_name,
                "stem": stem,
                "width": int(fmeta.get("width", 1)),
                "height": int(fmeta.get("height", 1)),
            }
            frame_to_info[frame_no] = finfo
            frames_by_stem[stem] = finfo

        shapes_by_frame: dict[int, list[dict]] = {}
        for sh in ann.get("shapes", []):
            if sh.get("type") != "rectangle":
                continue
            shapes_by_frame.setdefault(int(sh["frame"]), []).append(sh)

        tags_by_frame: dict[int, set[str]] = {}
        for tg in ann.get("tags", []):
            lname = label_by_id.get(tg.get("label_id"), {}).get("name", "")
            tags_by_frame.setdefault(int(tg["frame"]), set()).add(lname)

        shapes_by_line: dict[str, dict] = {}
        total_shapes = len(ann.get("shapes", []))
        for frame_no, finfo in sorted(frame_to_info.items()):
            stem = finfo["stem"]
            img_w = max(1, finfo["width"])
            img_h = max(1, finfo["height"])
            f_shapes = shapes_by_frame.get(frame_no, [])
            # Hỗ trợ đánh dấu missing label nếu job hoàn toàn mới hoặc gắn tag missing_label
            if not f_shapes and (total_shapes == 0 or "missing_label" in tags_by_frame.get(frame_no, set())):
                continue
            lines_out: list[str] = []
            for line_no, sh in enumerate(f_shapes, 1):
                pts = sh.get("points", [0, 0, 0, 0])
                xtl, ytl, xbr, ybr = float(pts[0]), float(pts[1]), float(pts[2]), float(pts[3])
                bw = max(0.0, (xbr - xtl) / img_w)
                bh = max(0.0, (ybr - ytl) / img_h)
                cx = ((xtl + xbr) / 2.0) / img_w
                cy = ((ytl + ybr) / 2.0) / img_h
                cls_idx = label_idx_by_id.get(sh["label_id"], 0)
                lines_out.append(f"{cls_idx} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
                l_info = label_by_id.get(sh["label_id"], {})
                shapes_by_line[f"{stem}:{line_no}"] = {
                    "id": sh.get("id"),
                    "label_id": sh["label_id"],
                    "label_name": l_info.get("name", ""),
                    "can_review_spec_id": l_info.get("can_review_spec_id"),
                    "frame": frame_no,
                    "points": [xtl, ytl, xbr, ybr],
                    "attributes": sh.get("attributes", []),
                }
            (dest / "labels" / f"{stem}.txt").write_text(
                "\n".join(lines_out) + ("\n" if lines_out else ""),
                encoding="utf-8",
            )

        (dest / "data.yaml").write_text(
            yaml.safe_dump({"path": ".", "names": dict(enumerate(active_classes))}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        assignee = job_info.get("assignee") or {}
        live_meta = {
            "cvat_url": self.base_url,
            "job_id": int(job_id),
            "task_id": job_info.get("task_id"),
            "task_name": job_info.get("task_name", f"Job {job_id}"),
            "project_id": job_info.get("project_id"),
            "project_name": job_info.get("project_name", ""),
            "assignee": assignee.get("username", "annotator") if isinstance(assignee, dict) else str(assignee),
            "active_classes": active_classes,
            "labels": list(label_by_id.values()),
            "frames_by_stem": frames_by_stem,
            "shapes_by_line": shapes_by_line,
        }
        (dest / "live_mapping.json").write_text(json.dumps(live_meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return live_meta

    def create_issue(self, job_id: int | str, frame: int, position: list[float], message: str) -> dict:
        payload = {"job": int(job_id), "frame": int(frame), "position": [round(float(x), 2) for x in position], "message": message}
        _, resp, _ = self._request("POST", "/api/issues", body=json.dumps(payload).encode("utf-8"))
        return json.loads(resp.decode("utf-8"))

    def apply_flags_to_cvat_job(self, job_id: int | str, qc_result, create_issues: bool = True) -> dict:
        """Cập nhật trực tiếp `can_review=true` lên shapes và tạo CVAT Issues cho Job đang mở."""
        live_meta = qc_result.live_meta or {}
        shapes_map = live_meta.get("shapes_by_line", {})
        frames_map = live_meta.get("frames_by_stem", {})

        ann = self.get_json(f"/api/jobs/{job_id}/annotations")
        existing_shapes = {sh["id"]: sh for sh in ann.get("shapes", []) if "id" in sh}

        existing_issue_keys: set[tuple[int, str]] = set()
        if create_issues:
            iss_resp = self.get_json(f"/api/issues?job_id={job_id}&page_size=200")
            for iss in iss_resp.get("results", []):
                comments = iss.get("comments", [])
                msg = comments[0].get("message", "") if comments else ""
                existing_issue_keys.add((int(iss["frame"]), msg))

        shapes_to_update: list[dict] = []
        issues_created = 0
        for (stem, line_no), codes in sorted(qc_result.flagged_lines.items()):
            s_info = shapes_map.get(f"{stem}:{line_no}")
            if not s_info or s_info.get("id") not in existing_shapes:
                continue
            sh_copy = dict(existing_shapes[s_info["id"]])
            spec_id = s_info.get("can_review_spec_id")
            if spec_id is not None:
                attrs = [a for a in sh_copy.get("attributes", []) if a.get("spec_id") != spec_id]
                attrs.append({"spec_id": spec_id, "value": "true"})
                sh_copy["attributes"] = attrs
                shapes_to_update.append(sh_copy)
            if create_issues:
                msg = f"[AutoQC] {', '.join(codes)} (line {line_no})"
                frame_no = int(s_info["frame"])
                if (frame_no, msg) not in existing_issue_keys:
                    self.create_issue(job_id, frame_no, s_info["points"], msg)
                    existing_issue_keys.add((frame_no, msg))
                    issues_created += 1

        if shapes_to_update:
            patch_body = {"shapes": shapes_to_update, "tracks": [], "tags": [], "version": ann.get("version", 0)}
            self._request("PATCH", f"/api/jobs/{job_id}/annotations?action=update", body=json.dumps(patch_body).encode("utf-8"))

        # Tạo thêm Issue cho các gợi ý MISSING hoặc MISSING_LABEL_FILE
        if create_issues:
            for stem, sugs in sorted(qc_result.model_suggestions.items()):
                finfo = frames_map.get(stem)
                if not finfo:
                    continue
                img_w, img_h = max(1, finfo["width"]), max(1, finfo["height"])
                frame_no = int(finfo["frame"])
                for sug in sugs:
                    if sug.get("code") != "POSSIBLE_MISSING":
                        continue
                    bx, by, bw, bh = sug["box"]
                    pos = [
                        max(0.0, (bx - bw / 2) * img_w),
                        max(0.0, (by - bh / 2) * img_h),
                        min(float(img_w), (bx + bw / 2) * img_w),
                        min(float(img_h), (by + bh / 2) * img_h),
                    ]
                    cls_name = qc_result.cfg["classes"][sug["class_id"]] if sug["class_id"] < len(qc_result.cfg["classes"]) else str(sug["class_id"])
                    msg = f"[AutoQC] POSSIBLE_MISSING: {cls_name} (conf={sug['conf']:.2f})"
                    if (frame_no, msg) not in existing_issue_keys:
                        self.create_issue(job_id, frame_no, pos, msg)
                        existing_issue_keys.add((frame_no, msg))
                        issues_created += 1

            for row in qc_result.review_queue:
                if "NEEDS_LABEL" in row.get("codes", ""):
                    finfo = frames_map.get(row["image"])
                    if finfo:
                        frame_no = int(finfo["frame"])
                        msg = "[AutoQC] MISSING_LABEL_FILE: Ảnh chưa có nhãn, cần gán mới"
                        if (frame_no, msg) not in existing_issue_keys:
                            self.create_issue(job_id, frame_no, [10.0, 10.0, 120.0, 60.0], msg)
                            existing_issue_keys.add((frame_no, msg))
                            issues_created += 1

        return {"updated_shapes": len(shapes_to_update), "issues_created": issues_created}

    def create_rework_task(
        self,
        name: str,
        image_paths: list[Path],
        labels_schema_path: str | Path | None = None,
        annotations_xml_path: str | Path | None = None,
        project_id: int | None = None,
    ) -> dict:
        """Tạo Task/Job sửa lỗi mới trên CVAT Local, upload ảnh và gắn nhãn `can_review`."""
        schema_path = Path(labels_schema_path) if labels_schema_path else ROOT / "cvat" / "labels.json"
        labels = json.loads(schema_path.read_text(encoding="utf-8"))
        task_payload: dict = {"name": name}
        if project_id is not None:
            task_payload["project_id"] = project_id
        else:
            task_payload["labels"] = labels

        _, resp_bytes, _ = self._request("POST", "/api/tasks", body=json.dumps(task_payload).encode("utf-8"))
        task_info = json.loads(resp_bytes.decode("utf-8"))
        task_id = task_info["id"]

        if image_paths:
            self._upload_multipart_images(task_id, image_paths)
        if annotations_xml_path and Path(annotations_xml_path).exists():
            self._upload_annotations_xml(task_id, Path(annotations_xml_path))

        task_info["url"] = f"{self.base_url}/tasks/{task_id}"
        return task_info

    def create_rework_task_live(
        self,
        name: str,
        ds: Dataset,
        flagged_lines: dict[tuple[str, int], list[str]],
        classes: list[str],
        suggestions: dict[str, list[dict]] | None = None,
        project_id: int | None = None,
        labels_schema_path: str | Path | None = None,
    ) -> dict:
        """Tạo Task + Job Rework mới trên CVAT Local qua REST API, gán shape có `can_review=true` và tạo Issues."""
        schema_path = Path(labels_schema_path) if labels_schema_path else ROOT / "cvat" / "labels.json"
        if schema_path.exists() and set(classes) == {"car", "person", "truck", "bus"}:
            labels = json.loads(schema_path.read_text(encoding="utf-8"))
        else:
            palette = ["#ff8a3d", "#2fbf71", "#7b61ff", "#e5484d", "#0ea5e9", "#eab308", "#ec4899", "#64748b"]
            labels = [
                {
                    "name": cname,
                    "color": palette[i % len(palette)],
                    "type": "rectangle",
                    "attributes": [
                        {"name": "can_review", "mutable": False, "input_type": "checkbox", "default_value": "false", "values": []}
                    ],
                }
                for i, cname in enumerate(classes)
            ]

        task_payload: dict = {"name": name}
        if project_id is not None:
            task_payload["project_id"] = project_id
        else:
            task_payload["labels"] = labels

        _, resp_bytes, _ = self._request("POST", "/api/tasks", body=json.dumps(task_payload).encode("utf-8"))
        task_info = json.loads(resp_bytes.decode("utf-8"))
        task_id = task_info["id"]

        image_paths = ds.image_files()
        if not image_paths:
            task_info["url"] = f"{self.base_url}/tasks/{task_id}"
            return task_info

        self._upload_multipart_images(task_id, image_paths, sorting_method="lexicographical")

        # Chờ CVAT worker xử lý xong data và sinh Job
        new_job_id = None
        for _ in range(40):
            jobs_resp = self.get_json(f"/api/jobs?task_id={task_id}")
            results = jobs_resp.get("results", [])
            if results:
                new_job_id = results[0]["id"]
                break
            time.sleep(0.5)

        if new_job_id is None:
            task_info["url"] = f"{self.base_url}/tasks/{task_id}"
            return task_info

        meta = self.get_json(f"/api/jobs/{new_job_id}/data/meta")
        lbls_resp = self.get_json(f"/api/labels?job_id={new_job_id}&page_size=100").get("results", [])
        lbl_by_name = {
            l_obj["name"]: (
                l_obj["id"],
                next((a["id"] for a in l_obj.get("attributes", []) if a.get("name") == "can_review"), None),
            )
            for l_obj in lbls_resp
        }
        frame_by_name = {Path(f["name"]).name: idx for idx, f in enumerate(meta.get("frames", []))}

        shapes_payload: list[dict] = []
        pending_issues: list[tuple[int, list[float], str]] = []
        for img_path in image_paths:
            stem = img_path.stem
            frame_no = frame_by_name.get(img_path.name)
            size = ds.image_size(stem)
            if frame_no is None or size is None:
                continue
            img_w, img_h = size
            lbl_path = ds.label_path(stem)
            if not lbl_path.exists():
                pending_issues.append((frame_no, [10.0, 10.0, 120.0, 60.0], "[AutoQC] MISSING_LABEL_FILE: Cần gán nhãn"))
                continue

            for line_no, raw_ln in ds.read_label_lines(stem) or []:
                try:
                    cls_id, cx, cy, bw, bh = parse_yolo_line(raw_ln)
                except ValueError:
                    continue
                if not (0 <= cls_id < len(classes)) or bw <= 0 or bh <= 0:
                    continue
                cname = classes[cls_id]
                if cname not in lbl_by_name:
                    continue
                lid, spec_id = lbl_by_name[cname]
                pts = [
                    round(max(0.0, (cx - bw / 2) * img_w), 2),
                    round(max(0.0, (cy - bh / 2) * img_h), 2),
                    round(min(float(img_w), (cx + bw / 2) * img_w), 2),
                    round(min(float(img_h), (cy + bh / 2) * img_h), 2),
                ]
                reasons = flagged_lines.get((stem, line_no), [])
                attrs = [{"spec_id": spec_id, "value": "true" if reasons else "false"}] if spec_id is not None else []
                shapes_payload.append(
                    {
                        "type": "rectangle",
                        "frame": frame_no,
                        "label_id": lid,
                        "points": pts,
                        "occluded": False,
                        "z_order": 0,
                        "rotation": 0.0,
                        "group": 0,
                        "source": "auto" if reasons else "manual",
                        "attributes": attrs,
                    }
                )
                if reasons:
                    pending_issues.append((frame_no, pts, f"[AutoQC] {', '.join(reasons)} (line {line_no})"))

            for sug in (suggestions or {}).get(stem, []):
                if sug.get("code") != "POSSIBLE_MISSING":
                    continue
                bx, by, bw, bh = sug["box"]
                pts = [
                    round(max(0.0, (bx - bw / 2) * img_w), 2),
                    round(max(0.0, (by - bh / 2) * img_h), 2),
                    round(min(float(img_w), (bx + bw / 2) * img_w), 2),
                    round(min(float(img_h), (by + bh / 2) * img_h), 2),
                ]
                sc_name = classes[sug["class_id"]] if sug["class_id"] < len(classes) else str(sug["class_id"])
                pending_issues.append((frame_no, pts, f"[AutoQC] POSSIBLE_MISSING: {sc_name} (conf={sug['conf']:.2f})"))

        if shapes_payload:
            ann_body = {"shapes": shapes_payload, "tracks": [], "tags": [], "version": 0}
            self._request("PUT", f"/api/jobs/{new_job_id}/annotations", body=json.dumps(ann_body).encode("utf-8"))

        for frame_no, pts, msg in pending_issues[:100]:
            try:
                self.create_issue(new_job_id, frame_no, pts, msg)
            except Exception:
                pass

        return {
            "task_id": task_id,
            "job_id": new_job_id,
            "n_shapes": len(shapes_payload),
            "n_issues": len(pending_issues),
            "url": f"{self.base_url}/tasks/{task_id}/jobs/{new_job_id}",
        }

    def _upload_multipart_images(self, task_id: int, image_paths: list[Path], sorting_method: str = "natural") -> None:
        boundary = f"----AutoQCBoundary{uuid.uuid4().hex}"
        chunks: list[bytes] = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image_quality\"\r\n\r\n85\r\n".encode("utf-8"),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"sorting_method\"\r\n\r\n{sorting_method}\r\n".encode("utf-8"),
        ]
        for idx, img_p in enumerate(image_paths):
            mime = "image/png" if img_p.suffix.lower() == ".png" else "image/jpeg"
            header = (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"client_files[{idx}]\"; filename=\"{img_p.name}\"\r\n"
                f"Content-Type: {mime}\r\n\r\n"
            ).encode("utf-8")
            chunks.append(header + img_p.read_bytes() + b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(chunks)
        self._request("POST", f"/api/tasks/{task_id}/data", body=body, content_type=f"multipart/form-data; boundary={boundary}")

    def _upload_annotations_xml(self, task_id: int, xml_path: Path) -> None:
        boundary = f"----AutoQCBoundary{uuid.uuid4().hex}"
        header = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"annotation_file\"; filename=\"{xml_path.name}\"\r\n"
            f"Content-Type: application/xml\r\n\r\n"
        ).encode("utf-8")
        body = header + xml_path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")
        q = urllib.parse.urlencode({"format": "CVAT 1.1", "action": "upload"})
        self._request(
            "POST",
            f"/api/tasks/{task_id}/annotations?{q}",
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
