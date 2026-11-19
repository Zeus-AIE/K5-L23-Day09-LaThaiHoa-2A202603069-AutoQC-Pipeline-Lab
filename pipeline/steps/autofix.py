"""BƯỚC 2/4 — Sửa tự động những lỗi bắt buộc mà máy sửa được AN TOÀN.   ★★

Test:  pytest tests/test_autofix.py -q

`fix(ds, issues, cfg) -> list[dict]`: duyệt `issues` (chỉ lỗi severity ERROR), sửa thẳng vào thư mục `ds`,
và trả về danh sách thay đổi; mỗi phần tử có khoá: image, line, action, before, after
(đây chính là file `03_fixes.csv` — bằng chứng pipeline đã làm gì, người có thể kiểm lại).

| code lỗi                                       | cách sửa                                    | action           |
|------------------------------------------------|---------------------------------------------|------------------|
| BAD_FORMAT, BAD_CLASS_ID, BOX_ZERO, DUP_BOX    | xoá dòng đó khỏi file nhãn                  | "drop_line"      |
| BOX_OUT_OF_IMAGE                               | cắt box về trong [0,1] (giữ nguyên lớp)     | "clip_box"       |
| MISSING_LABEL_FILE                             | KHÔNG sửa file (máy không thể bịa nhãn): ghi 1 dòng fix, before="(không có file)", after="chuyển người gán" | "needs_label" |
| BAD_CLASS_NAME                                 | ghi lại `names` trong data.yaml đúng `cfg["classes"]` | "rename_classes" |
| DUP_IMAGE_*, LEAK_TEST, CORRUPT_IMAGE, BAD_SIZE| xoá ảnh + nhãn khỏi `ds`                    | "remove_image"   |

Bẫy cần nghĩ tới (có test cho từng cái)
- Một file có NHIỀU dòng lỗi: số dòng trong `issues` là số dòng GỐC. Nếu xoá từng dòng một thì số dòng lệch.
  Gom theo file, rồi ghi lại MỘT lần.
- Sau khi cắt, box có thể không còn diện tích (nằm hẳn ngoài ảnh): lúc đó xoá dòng thay vì ghi box rỗng.
- Một dòng có 2 lỗi (vd. BOX_OUT_OF_IMAGE + DUP_BOX): xoá dòng thắng, chỉ ghi 1 fix `drop_line`, không ghi thêm `clip_box`. Dòng trống ở giữa file vẫn đếm vào số dòng gốc.
- Ảnh vừa bị xoá (DUP_IMAGE_*...) mà còn issue dòng của chính nó: bỏ qua êm, không được báo lỗi/chết.
- Không động tới lỗi WARN (cảnh báo là việc của người). Không sửa file ở `data/raw/` — chỉ sửa `ds`.
- Chạy lần 2 trên dữ liệu đã sửa phải không làm gì thêm (idempotent) — NGOẠI LỆ duy nhất: `needs_label` được ghi lại mỗi lần còn lỗi `MISSING_LABEL_FILE` (vì ảnh vẫn chưa có nhãn).

- `needs_label` KHÔNG tạo file nhãn rỗng (rỗng = "ảnh nền", khác với "chưa gán"). `review_queue` sẽ đẩy ảnh này lên đầu hàng đợi cho người.

Gợi ý: `yaml.safe_load / safe_dump`.
"""
from collections import defaultdict  # noqa: F401

import yaml  # noqa: F401

from ..common import ERROR, parse_yolo_line  # noqa: F401


REMOVE_CODES = {
    "DUP_IMAGE_EXACT",
    "DUP_IMAGE_NEAR",
    "LEAK_TEST",
    "CORRUPT_IMAGE",
    "BAD_SIZE",
}
DROP_LINE_CODES = {"BAD_FORMAT", "BAD_CLASS_ID", "BOX_ZERO", "DUP_BOX"}


def fix(ds, issues, cfg):
    err_issues = [i for i in issues if i.severity == ERROR]
    fixes = []
    removed_images = set()

    # 1. Xoá ảnh (và file nhãn đi kèm) cho lỗi cửa vào
    for i in err_issues:
        if i.code not in REMOVE_CODES or i.image in removed_images:
            continue
        img_p = ds.image_path(i.image)
        lbl_p = ds.label_path(i.image)
        if (img_p is not None and img_p.exists()) or lbl_p.exists():
            if img_p is not None and img_p.exists():
                img_p.unlink()
            if lbl_p.exists():
                lbl_p.unlink()
            removed_images.add(i.image)
            ds._size_cache.pop(i.image, None)
            fixes.append(
                {
                    "image": i.image,
                    "line": "",
                    "action": "remove_image",
                    "before": i.code,
                    "after": "đã xoá ảnh và nhãn",
                }
            )

    # 2. Sửa danh sách tên lớp trong data.yaml
    if any(i.code == "BAD_CLASS_NAME" for i in err_issues):
        target_names = list(cfg["classes"])
        cur_map = ds.yaml_names()
        cur_list = [cur_map.get(idx) for idx in range(len(target_names))]
        if len(cur_map) != len(target_names) or cur_list != target_names:
            if ds.yaml_path.exists():
                data = yaml.safe_load(ds.yaml_path.read_text(encoding="utf-8")) or {}
            else:
                data = {"path": "."}
            before_names = str(data.get("names", {}))
            data["names"] = {idx: name for idx, name in enumerate(target_names)}
            ds.yaml_path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            fixes.append(
                {
                    "image": "data.yaml",
                    "line": "",
                    "action": "rename_classes",
                    "before": before_names,
                    "after": str(data["names"]),
                }
            )

    # 3. Đánh dấu ảnh thiếu file nhãn để đẩy sang hàng đợi người gán (không tạo file rỗng)
    missing_seen = set()
    for i in err_issues:
        if i.code != "MISSING_LABEL_FILE" or i.image in removed_images or i.image in missing_seen:
            continue
        if not ds.label_path(i.image).exists():
            missing_seen.add(i.image)
            fixes.append(
                {
                    "image": i.image,
                    "line": "",
                    "action": "needs_label",
                    "before": "(không có file)",
                    "after": "chuyển người gán",
                }
            )

    # 4. Gom các lỗi theo dòng để sửa từng file một lần theo số dòng gốc
    drop_by_img = defaultdict(set)
    clip_by_img = defaultdict(set)
    for i in err_issues:
        if i.image in removed_images or i.line is None:
            continue
        if i.code in DROP_LINE_CODES:
            drop_by_img[i.image].add(i.line)
        elif i.code == "BOX_OUT_OF_IMAGE":
            clip_by_img[i.image].add(i.line)

    for stem in sorted(set(drop_by_img) | set(clip_by_img)):
        lp = ds.label_path(stem)
        if not lp.exists():
            continue
        raw_lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()
        drops = drop_by_img[stem]
        clips = clip_by_img[stem] - drops  # drop_line luôn thắng nếu một dòng dính cả 2 lỗi
        new_lines = []
        for idx, raw_ln in enumerate(raw_lines, 1):
            if idx in drops:
                fixes.append(
                    {
                        "image": stem,
                        "line": idx,
                        "action": "drop_line",
                        "before": raw_ln,
                        "after": "",
                    }
                )
                continue
            if idx in clips:
                try:
                    cls_id, cx, cy, w, h = parse_yolo_line(raw_ln)
                except ValueError:
                    fixes.append(
                        {
                            "image": stem,
                            "line": idx,
                            "action": "drop_line",
                            "before": raw_ln,
                            "after": "",
                        }
                    )
                    continue
                x1 = max(0.0, cx - w / 2)
                y1 = max(0.0, cy - h / 2)
                x2 = min(1.0, cx + w / 2)
                y2 = min(1.0, cy + h / 2)
                nw, nh = x2 - x1, y2 - y1
                if nw <= 1e-9 or nh <= 1e-9:
                    fixes.append(
                        {
                            "image": stem,
                            "line": idx,
                            "action": "drop_line",
                            "before": raw_ln,
                            "after": "",
                        }
                    )
                    continue
                ncx, ncy = (x1 + x2) / 2, (y1 + y2) / 2
                fixed_ln = f"{cls_id} {ncx:.6f} {ncy:.6f} {nw:.6f} {nh:.6f}"
                new_lines.append(fixed_ln)
                fixes.append(
                    {
                        "image": stem,
                        "line": idx,
                        "action": "clip_box",
                        "before": raw_ln,
                        "after": fixed_ln,
                    }
                )
            else:
                new_lines.append(raw_ln)
        lp.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")

    return fixes
