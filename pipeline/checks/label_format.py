"""CHECK 2 — Dòng nhãn YOLO: sai định dạng, ID lớp lạ, box tràn ảnh, box rỗng, box lặp.   (Script 2 trong slide)   ★★

Test:  pytest tests/test_label_format.py -q

Mỗi dòng nhãn: `id cx cy w h` (5 số, toạ độ 0–1). Trả về list[Issue]; `Issue.line` = số dòng (đếm từ 1).

| code             | severity | khi nào                                                                  |
|------------------|----------|--------------------------------------------------------------------------|
| BAD_FORMAT       | ERROR    | không đúng 5 cột số (dùng `parse_yolo_line`, bắt `ValueError`)           |
| BAD_CLASS_ID     | ERROR    | id lớp ngoài 0..len(ds.classes)-1                                        |
| BOX_ZERO         | ERROR    | w <= 0 hoặc h <= 0                                                       |
| BOX_OUT_OF_IMAGE | ERROR    | mép box vượt [0,1] quá `cfg["label_format"]["overflow_tol"]`; extra={"overflow": mức tràn} |
| DUP_BOX          | ERROR    | cùng lớp với một dòng TRƯỚC ĐÓ và IoU > `dup_box_iou`; báo ở dòng SAU    |
| BOX_TINY         | WARN     | cạnh ngắn của box (px) < `min_box_px`  (cần `ds.image_size(stem)`)       |
| BOX_HUGE         | WARN     | w*h > `max_box_frac`                                                     |

Gợi ý
- Mép trái = x - w/2, mép phải = x + w/2 (xem câu hỏi "dòng YOLO nào lỗi?" trong slide).
- Một dòng hỏng (BAD_FORMAT, BAD_CLASS_ID, BOX_ZERO) thì `continue`, đừng kiểm tiếp hình học của dòng đó.
- Dòng thiếu cột làm script của slide dừng giữa chừng: đây là lý do phải `try/except`.
- `iou_xywh(a, b)` có sẵn trong common.py.
- Ngưỡng nằm trong `cfg["label_format"]` — đừng viết cứng số vào code.
"""
from ..common import ERROR, WARN, Issue, iou_xywh, parse_yolo_line  # noqa: F401


def run(ds, cfg):
    lf_cfg = cfg["label_format"]
    overflow_tol = lf_cfg["overflow_tol"]
    min_box_px = lf_cfg["min_box_px"]
    max_box_frac = lf_cfg["max_box_frac"]
    dup_box_iou = lf_cfg["dup_box_iou"]

    issues = []
    n_classes = len(ds.classes)
    for lp in ds.label_files():
        stem = lp.stem
        lines = ds.read_label_lines(stem)
        if not lines:
            continue
        size = ds.image_size(stem)
        seen_boxes = []
        for line_no, text in lines:
            try:
                cls_id, cx, cy, w, h = parse_yolo_line(text)
            except ValueError as e:
                issues.append(Issue("label_format", ERROR, "BAD_FORMAT", stem, f"sai định dạng dòng nhãn: {e}", line=line_no))
                continue

            if not (0 <= cls_id < n_classes):
                issues.append(
                    Issue(
                        "label_format",
                        ERROR,
                        "BAD_CLASS_ID",
                        stem,
                        f"id lớp {cls_id} ngoài 0..{n_classes - 1}",
                        line=line_no,
                        extra={"class_id": cls_id},
                    )
                )
                continue

            if w <= 0 or h <= 0:
                issues.append(
                    Issue(
                        "label_format",
                        ERROR,
                        "BOX_ZERO",
                        stem,
                        f"box có kích thước <= 0 (w={w}, h={h})",
                        line=line_no,
                    )
                )
                continue

            x1, y1, x2, y2 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
            overflow = max(0.0, -x1, -y1, x2 - 1.0, y2 - 1.0)
            if overflow > overflow_tol:
                issues.append(
                    Issue(
                        "label_format",
                        ERROR,
                        "BOX_OUT_OF_IMAGE",
                        stem,
                        f"box tràn ra ngoài ảnh ({overflow:.4f} > {overflow_tol})",
                        line=line_no,
                        extra={"overflow": round(overflow, 6)},
                    )
                )

            box = (cx, cy, w, h)
            dup_line = next(
                (prev_line for prev_line, prev_cls, prev_box in seen_boxes if prev_cls == cls_id and iou_xywh(box, prev_box) > dup_box_iou),
                None,
            )
            if dup_line is not None:
                issues.append(
                    Issue(
                        "label_format",
                        ERROR,
                        "DUP_BOX",
                        stem,
                        f"box trùng cùng lớp với dòng {dup_line}",
                        line=line_no,
                        extra={"dup_of": dup_line},
                    )
                )
            seen_boxes.append((line_no, cls_id, box))

            if size is not None:
                img_w, img_h = size
                short_side = min(w * img_w, h * img_h)
                if short_side < min_box_px:
                    issues.append(
                        Issue(
                            "label_format",
                            WARN,
                            "BOX_TINY",
                            stem,
                            f"cạnh ngắn {short_side:.1f}px < {min_box_px}px",
                            line=line_no,
                            extra={"short_side_px": round(short_side, 2)},
                        )
                    )

            area_frac = w * h
            if area_frac > max_box_frac:
                issues.append(
                    Issue(
                        "label_format",
                        WARN,
                        "BOX_HUGE",
                        stem,
                        f"diện tích box {area_frac:.3f} > {max_box_frac}",
                        line=line_no,
                        extra={"area_frac": round(area_frac, 4)},
                    )
                )
    return issues
