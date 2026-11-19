"""Đóng gói báo cáo lỗi trực quan & hồ sơ bàn giao cho bộ phận QC."""
from __future__ import annotations

import html
import json
import shutil
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

from pipeline.common import ROOT, Dataset, parse_yolo_line

from .analyzer import JobQCResult
from .job_creator import create_rework_job

CLASS_COLORS = ["#2563eb", "#dc2626", "#ea580c", "#9333ea", "#0284c7", "#16a34a", "#db2777", "#64748b"]
AI_COLOR = "#facc15"


def _draw_dashed_rect(draw: ImageDraw.ImageDraw, box: list[float], color: str, width: int = 2, dash: int = 8) -> None:
    x1, y1, x2, y2 = box
    edges = [((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))]
    for (ax, ay), (bx, by) in edges:
        length = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
        if length <= 0:
            continue
        steps = max(1, int(length // dash))
        for k in range(0, steps, 2):
            t0 = k / steps
            t1 = min(1.0, (k + 1) / steps)
            draw.line(
                [(ax + (bx - ax) * t0, ay + (by - ay) * t0), (ax + (bx - ax) * t1, ay + (by - ay) * t1)],
                fill=color,
                width=width,
            )


def render_single_viz(qc_result: JobQCResult, stem: str, out_png: Path) -> bool:
    """Vẽ trực quan 1 ảnh (nét liền: nhãn Annotator, nét đứt vàng: gợi ý AI) ra file PNG."""
    ds = Dataset(qc_result.clean_ds_dir, qc_result.cfg["classes"])
    classes = qc_result.cfg["classes"]
    img_p = ds.image_path(stem)
    if img_p is None or not img_p.exists():
        # Nếu ảnh bị loại ở bước intake, thử tìm trong source_ds_dir để vẫn xem được bằng mắt
        src_ds = Dataset(qc_result.source_ds_dir, classes)
        img_p = src_ds.image_path(stem)
        if img_p is None or not img_p.exists():
            return False
        active_ds = src_ds
    else:
        active_ds = ds

    try:
        with Image.open(img_p) as src_im:
            im = src_im.convert("RGB")
    except Exception:
        return False

    img_w, img_h = im.size
    draw = ImageDraw.Draw(im)
    for line_no, raw_ln in active_ds.read_label_lines(stem) or []:
        try:
            cls_id, cx, cy, bw, bh = parse_yolo_line(raw_ln)
        except ValueError:
            continue
        col = CLASS_COLORS[cls_id % len(CLASS_COLORS)] if 0 <= cls_id < len(classes) else "#111827"
        name = classes[cls_id] if 0 <= cls_id < len(classes) else str(cls_id)
        x1, y1 = (cx - bw / 2) * img_w, (cy - bh / 2) * img_h
        x2, y2 = (cx + bw / 2) * img_w, (cy + bh / 2) * img_h
        flags = qc_result.flagged_lines.get((stem, line_no), [])
        tag = f"{line_no}:{name}" + (f" [!{','.join(flags)}]" if flags else "")
        draw.rectangle([x1, y1, x2, y2], outline=col, width=3 if not flags else 4)
        draw.text((max(2.0, x1 + 3), max(2.0, y1 + 2)), tag, fill=col)

    for sug in qc_result.model_suggestions.get(stem, []):
        sc, sconf = sug["class_id"], sug["conf"]
        bx, by, bw, bh = sug["box"]
        sname = classes[sc] if 0 <= sc < len(classes) else str(sc)
        box = [(bx - bw / 2) * img_w, (by - bh / 2) * img_h, (bx + bw / 2) * img_w, (by + bh / 2) * img_h]
        _draw_dashed_rect(draw, box, AI_COLOR, width=2)
        draw.text((max(2.0, box[0] + 3), max(2.0, box[3] - 14)), f"AI:{sname} ({sconf:.2f})", fill=AI_COLOR)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_png)
    return True


def render_qc_viz(qc_result: JobQCResult, viz_dir: Path, top_n: int = 12) -> list[dict]:
    """Vẽ ảnh đối chiếu box Annotator (nét liền) vs box AI `yolo26l` gợi ý (nét đứt vàng) cho top N ảnh."""
    viz_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[dict] = []
    for row in qc_result.review_queue[:top_n]:
        stem = row["image"]
        out_png = viz_dir / f"{stem}.png"
        if render_single_viz(qc_result, stem, out_png):
            rendered.append(
                {
                    "image": stem,
                    "filename": out_png.name,
                    "priority": row["priority"],
                    "codes": row["codes"],
                }
            )
    return rendered


def build_markdown_report(qc_result: JobQCResult, rework_info: dict | None = None) -> str:
    """Tạo báo cáo tóm tắt Markdown chuẩn hóa gửi Slack / GitHub / QC."""
    s = qc_result.to_summary_dict()
    v = s["verdict"]
    lines = [
        f"# Báo Cáo Kiểm Soát Nhanh AutoQC — CVAT Job `{qc_result.job_id}`",
        "",
        f"- **Annotator:** {qc_result.annotator} · **Thời gian:** {qc_result.timestamp}",
        f"- **Quyết định Gate:** **{v['decision'].upper()}** — {'; '.join(v['reasons'])}",
        f"- **Điểm chất lượng (Quality Score):** **{qc_result.quality_score}/100**",
        f"- **Ảnh quét / Nhận / Loại ở cửa vào:** {qc_result.n_scanned} / {qc_result.n_accepted} / {qc_result.n_rejected}",
        f"- **Lỗi thô (v1):** {s['n_v1_errors']} lỗi bắt buộc, {s['n_v1_warns']} cảnh báo · **Máy tự sửa:** {s['n_fixes']} thay đổi",
        f"- **Hàng đợi ưu tiên (v2):** {s['queue_size']} ảnh cần người xem",
    ]
    if rework_info:
        lines.append(
            f"- **CVAT Rework Sub-Job:** `{Path(rework_info['rework_zip']).name}` "
            f"({rework_info['n_images']} ảnh, {rework_info['n_flagged_boxes']} box gắn `can_review=true`)"
        )
    lines += ["", "## Thống kê theo mã lỗi (v1)", "", "| Mức độ | Mã lỗi | Số lượng |", "|---|---|---:|"]
    for row in s["code_summary_v1"]:
        lines.append(f"| {row['severity'].upper()} | `{row['code']}` | {row['count']} |")
    lines += ["", "## Top 15 ảnh ưu tiên cao nhất cần kiểm tra", "", "| Ảnh | Điểm ưu tiên | Mã cảnh báo / sửa |", "|---|---:|---|"]
    for q in qc_result.review_queue[:15]:
        lines.append(f"| `{q['image']}` | {q['priority']} | `{q['codes']}` |")
    return "\n".join(lines) + "\n"


def build_html_report(qc_result: JobQCResult, viz_items: list[dict], rework_info: dict | None = None, standalone: bool = True) -> str:
    """Tạo trang HTML Dashboard trực quan (cả bản tĩnh offline và bản Live Dashboard `:8765`)."""
    e = html.escape
    s = qc_result.to_summary_dict()
    verdict = s["verdict"]
    dec = verdict["decision"].upper()
    badge_cls = "badge-release" if dec == "RELEASE" else ("badge-hold" if dec == "HOLD" else "badge-rework")

    code_rows_html = "\n".join(
        f"<tr class='clickable' onclick=\"filterByCode('{e(r['code'])}')\">"
        f"<td><span class='sev sev-{e(r['severity'])}'>{e(r['severity'].upper())}</span></td>"
        f"<td><code>{e(r['code'])}</code></td><td><strong>{r['count']}</strong></td></tr>"
        for r in s["code_summary_v1"]
    )
    queue_rows_html = "\n".join(
        f"<tr class='q-row clickable' data-image='{e(q['image'])}' data-codes='{e(q['codes'])}' "
        f"onclick=\"inspectFrame('{e(q['image'])}')\">"
        f"<td><code>{e(q['image'])}</code></td>"
        f"<td>{q['priority']}</td>"
        f"<td><code>{e(q['codes'])}</code></td></tr>"
        for q in s["review_queue"]
    )
    viz_prefix = "viz/" if standalone else "/viz/"
    gallery_html = "\n".join(
        f"<div class='viz-card clickable' onclick=\"inspectFrame('{e(item['image'])}')\">"
        f"<img src='{viz_prefix}{e(item['filename'])}' alt='{e(item['image'])}' loading='lazy'>"
        f"<div class='viz-meta'><strong>{e(item['image'])}</strong> (Priority: {item['priority']})<br>"
        f"<code>{e(item['codes'])}</code></div></div>"
        for item in viz_items
    )

    live_toolbar = ""
    inspector_panel = ""
    history_panel = ""
    live_script = ""
    if not standalone:
        live_meta = s["live_meta"]
        open_cvat_btn = (
            f"<a class='btn btn-outline' href='{e(live_meta['job_url'])}' target='_blank'>↗ Mở Job #{e(str(live_meta['job_id']))} trên CVAT</a>"
            if live_meta.get("job_url")
            else "<a class='btn btn-outline' href='http://localhost:8080' target='_blank'>↗ Mở CVAT Local (:8080)</a>"
        )
        v2_codes = sorted({i.code for i in qc_result.v2_issues if i.image not in ("data.yaml", "<dataset>")})
        issue_opts_html = "".join(
            f"<option value='{e(c)}'>Chỉ lỗi: {e(c)}</option>" for c in v2_codes
        )
        live_toolbar = f"""
        <div class='toolbar card'>
          <div class='toolbar-row'>
            <label><strong>Nguồn CVAT / Dataset:</strong></label>
            <select id='jobSelect'></select>
            <button class='btn btn-primary' onclick='scanSelected()'>🔍 Quét Lỗi Job Đã Chọn</button>
            {open_cvat_btn}
          </div>
          <div class='toolbar-row'>
            <button class='btn btn-warn' onclick='flagCVATJob()'>🏷️ Gắn cờ can_review=true &amp; Tạo Issue lên CVAT</button>
            <select id='reworkIssueFilter' style='min-width:220px;flex:0 1 auto'>
              <option value='ALL'>Rework: Tất cả lỗi ưu tiên</option>
              <option value='SPLIT_ALL'>Rework: Tách riêng mỗi loại Issue 1 Task</option>
              {issue_opts_html}
            </select>
            <button class='btn btn-accent' onclick='createLiveRework()'>➕ Tạo Rework Job Theo Lọc</button>
            <a class='btn btn-secondary' href='/download/handover'>📦 Tải QC Handover (.zip)</a>
            <a class='btn btn-outline' href='/download/rework'>📥 Tải Rework YOLO 1.1 (.zip)</a>
          </div>
          <div id='status-banner' class='status-banner'></div>
        </div>
        """
        inspector_panel = """
        <div class='card' id='inspectorCard'>
          <div class='inspector-head'>
            <h3>🖼️ Trình Soi Lỗi Trực Quan (Click bất kỳ dòng nào trong Hàng đợi)</h3>
            <span id='inspectorLink'></span>
          </div>
          <div class='inspector-body'>
            <div class='inspector-img-wrap'><img id='inspectorImg' src='' alt='Chọn một ảnh để xem chi tiết'></div>
            <div class='inspector-info'>
              <h4 id='inspectorTitle'>Chưa chọn ảnh</h4>
              <div id='inspectorDetails' class='sub'>Bấm vào một ảnh trong bảng Hàng đợi hoặc Gallery bên dưới để phóng to đối chiếu box Annotator (nét liền) và box AI yolo26l gợi ý (nét đứt vàng).</div>
            </div>
          </div>
        </div>
        """
        history_panel = """
        <div class='card' id='historyCard'>
          <div class='inspector-head'>
            <h3>📜 Lịch Sử Chỉnh Sửa Rework (Audit Trail: Trước ↔ Sau)</h3>
            <div class='toolbar-row' style='margin:0'>
              <select id='trackBaselineSel' style='min-width:240px;flex:0 1 auto'></select>
              <button class='btn btn-primary' onclick='trackReworkHistory()'>🔄 Đồng bộ &amp; Ghi Lịch Sử Sửa</button>
              <a class='btn btn-outline' href='/download/rework-history'>📥 Tải CSV Lịch sử</a>
            </div>
          </div>
          <div id='historySummary' class='sub' style='margin-bottom:8px'>Chưa có phiên chỉnh sửa Rework nào được ghi nhận. Sau khi sửa trên CVAT và bấm Save, nhấn <strong>Đồng bộ &amp; Ghi Lịch Sử Sửa</strong> để đối chiếu từng box.</div>
          <div class='scroll-table'>
            <table>
              <thead><tr><th>Phiên</th><th>Thời gian</th><th>Job đích</th><th>Lọc Issue</th><th>Ảnh</th><th>Loại sửa</th><th>Chi tiết (Trước → Sau)</th></tr></thead>
              <tbody id='historyBody'></tbody>
            </table>
          </div>
        </div>
        """
        live_script = """
async function loadJobs() {
  try {
    const r = await fetch('/api/jobs');
    const data = await r.json();
    const sel = document.getElementById('jobSelect');
    if (!sel) return;
    sel.innerHTML = '';
    const optLocal = document.createElement('option');
    optLocal.value = 'local:data/raw';
    optLocal.textContent = '📁 Dataset Cục Bộ: data/raw (224 ảnh mẫu Day 09)';
    sel.appendChild(optLocal);
    for (const j of (data.jobs || [])) {
      const opt = document.createElement('option');
      opt.value = 'cvat:' + j.id;
      opt.textContent = `🖥️ CVAT Job #${j.id} — ${j.task_name} (${j.project_name || 'No project'}, ${j.frame_count} ảnh, ${j.state})`;
      if (String(j.id) === String(CURRENT_CVAT_JOB_ID)) opt.selected = true;
      sel.appendChild(opt);
    }
    if (!CURRENT_CVAT_JOB_ID) optLocal.selected = true;
  } catch (err) {
    console.warn('Không tải được danh sách CVAT jobs:', err);
  }
}
function setStatus(msg, isErr=false) {
  const el = document.getElementById('status-banner');
  if (!el) return;
  el.style.display = 'block';
  el.style.background = isErr ? '#fff2f0' : '#e6f7ff';
  el.style.borderColor = isErr ? '#ffccc7' : '#91d5ff';
  el.style.color = isErr ? '#cf1322' : '#096dd9';
  el.innerHTML = msg;
}
async function scanSelected() {
  const val = document.getElementById('jobSelect').value;
  setStatus('⏳ Đang kéo dữ liệu & chạy AutoQC Pre-flight (Intake + 5 Checks + Second Opinion + Autofix)...');
  const payload = val.startsWith('cvat:')
    ? {mode: 'cvat', job_id: Number(val.slice(5))}
    : {mode: 'local', src: val.slice(6), job_id: 'job_raw_224'};
  const r = await fetch('/api/scan', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  const data = await r.json();
  if (r.ok && data.ok) location.reload();
  else setStatus('❌ Lỗi quét job: ' + (data.message || r.status), true);
}
async function flagCVATJob() {
  setStatus('⏳ Đang cập nhật thuộc tính can_review=true và tạo CVAT Issues lên Job hiện tại...');
  const r = await fetch('/api/cvat/flag', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({})});
  const data = await r.json();
  if (r.ok && data.ok) setStatus(`✅ ${data.message}`);
  else setStatus('❌ ' + (data.message || 'Không thực hiện được'), true);
}
async function createLiveRework() {
  const filterVal = document.getElementById('reworkIssueFilter')?.value || 'ALL';
  const payload = {max_images: 40};
  if (filterVal === 'SPLIT_ALL') {
    payload.split_by_code = true;
    setStatus('⏳ Đang tách và tạo riêng từng Rework Task theo mỗi loại Issue trên CVAT Local...');
  } else if (filterVal !== 'ALL') {
    payload.issue_codes = [filterVal];
    setStatus(`⏳ Đang tạo Rework Task chỉ lọc mã lỗi ${filterVal} trên CVAT Local...`);
  } else {
    setStatus('⏳ Đang tạo Task & Job Rework mới trực tiếp trên CVAT Local...');
  }
  const r = await fetch('/api/cvat/create-rework', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  const data = await r.json();
  if (r.ok && data.ok) {
    if (data.split_by_code && Array.isArray(data.tasks)) {
      const links = data.tasks.map(t => {
        const u = t.cvat_task?.url || '';
        const lbl = (t.issue_codes || []).join(',');
        return `<li><code>${lbl}</code> (${t.n_images} ảnh, ${t.n_flagged_boxes} box): <a href="${u}" target="_blank" style="color:#1890ff;font-weight:600">${u}</a></li>`;
      }).join('');
      setStatus(`✅ ${data.message}<ul>${links}</ul>`);
    } else {
      const link = data.cvat_task?.url ? `<a href="${data.cvat_task.url}" target="_blank" style="color:#1890ff;font-weight:700">${data.cvat_task.url}</a>` : '';
      setStatus(`✅ ${data.message || ''} (${data.n_images} ảnh, ${data.n_flagged_boxes} box): ${link}`);
    }
    loadReworkHistory();
  } else {
    setStatus('❌ ' + (data.message || 'Lỗi tạo Rework Job'), true);
  }
}
async function loadReworkHistory() {
  try {
    const r = await fetch('/api/rework-history');
    const data = await r.json();
    const sel = document.getElementById('trackBaselineSel');
    if (sel) {
      const prev = sel.value;
      sel.innerHTML = '';
      for (const b of (data.baselines || [])) {
        if (!b.key.startsWith('cvat_')) continue;
        const opt = document.createElement('option');
        opt.value = b.key;
        const codes = (b.issue_codes || []).join(',');
        opt.textContent = `Đối chiếu ${b.key} [${codes || 'ORIGINAL'}]`;
        sel.appendChild(opt);
      }
      if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
      else if (sel.options.length > 0) sel.selectedIndex = sel.options.length - 1;
    }
    renderHistoryRows(data.history || []);
  } catch (err) {}
}
function renderHistoryRows(hist) {
  const esc = (v) => String(v ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const body = document.getElementById('historyBody');
  const sum = document.getElementById('historySummary');
  if (!body || !sum) return;
  if (!hist.length) {
    body.innerHTML = '';
    return;
  }
  const totalChanges = hist.reduce((acc, h) => acc + (h.n_changes || 0), 0);
  sum.innerHTML = `Đã ghi <strong>${hist.length}</strong> phiên kiểm tra Rework · Tổng <strong>${totalChanges}</strong> thay đổi box.`;
  const rows = [];
  for (const rev of [...hist].reverse()) {
    if (!rev.events || !rev.events.length) {
      rows.push(`<tr><td>#${esc(rev.revision)}</td><td>${esc(rev.timestamp)}</td><td><code>${esc(rev.target_job_id)}</code></td><td><code>${esc(rev.issue_filter)}</code></td><td>—</td><td><code>NO_CHANGE</code></td><td>Chưa có thay đổi mới</td></tr>`);
      continue;
    }
    for (const ev of rev.events) {
      rows.push(`<tr><td>#${esc(rev.revision)}</td><td>${esc(rev.timestamp)}</td><td><code>${esc(rev.target_job_id)}</code></td><td><code>${esc(rev.issue_filter)}</code></td><td><code>${esc(ev.image)}</code></td><td><code>${esc(ev.change_type)}</code></td><td>${esc(ev.detail)}<br><span class='sub'>${esc(ev.before)} → ${esc(ev.after)}</span></td></tr>`);
    }
  }
  body.innerHTML = rows.join('');
}
async function trackReworkHistory() {
  const sel = document.getElementById('trackBaselineSel');
  const key = sel?.value;
  if (!key) {
    setStatus('❌ Chưa có Job CVAT baseline nào để đối chiếu. Hãy quét hoặc tạo Rework Job trên CVAT trước.', true);
    return;
  }
  const jid = Number(key.replace('cvat_', ''));
  setStatus(`⏳ Đang kéo nhãn mới nhất của CVAT Job #${jid} để đối chiếu lịch sử chỉnh sửa...`);
  const r = await fetch('/api/cvat/track-rework', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cvat_job_id: jid, baseline_key: key})
  });
  const data = await r.json();
  if (r.ok && data.ok) {
    setStatus(`✅ ${data.message}`);
    renderHistoryRows(data.history || []);
  } else {
    setStatus('❌ ' + (data.message || 'Lỗi ghi lịch sử Rework'), true);
  }
}
async function inspectFrame(stem) {
  const esc = (v) => String(v ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  const img = document.getElementById('inspectorImg');
  const title = document.getElementById('inspectorTitle');
  const det = document.getElementById('inspectorDetails');
  const lnk = document.getElementById('inspectorLink');
  if (!img) return;
  img.src = '/viz/' + encodeURIComponent(stem) + '.png?t=' + Date.now();
  title.textContent = 'Đang tải chi tiết ' + stem + '...';
  const r = await fetch('/api/frame-detail?image=' + encodeURIComponent(stem));
  const d = await r.json();
  title.textContent = `${stem} (${d.filename || ''})`;
  if (d.cvat_frame_url) {
    lnk.innerHTML = `<a class='btn btn-primary' href='${esc(d.cvat_frame_url)}' target='_blank'>↗ Sửa ngay Frame #${esc(d.frame)} trên CVAT</a>`;
  } else {
    lnk.innerHTML = '';
  }
  const items = (d.issues || []).map(x => `<li><span class='sev sev-${esc(x.severity)}'>${esc(x.severity).toUpperCase()}</span> <code>${esc(x.code)}</code> ${x.line ? '(dòng ' + esc(x.line) + ')' : ''}: ${esc(x.message)}</li>`).join('');
  const sugs = (d.suggestions || []).map(x => `<li>🤖 <code>${esc(x.code)}</code>: lớp <strong>${esc(x.class_name)}</strong> (conf=${esc(x.conf)})</li>`).join('');
  det.innerHTML = `<p><strong>Số lỗi/cảnh báo (${(d.issues||[]).length}):</strong></p><ul>${items || '<li>Không có lỗi còn lại</li>'}</ul>` +
                  (sugs ? `<p><strong>Gợi ý từ AI yolo26l:</strong></p><ul>${sugs}</ul>` : '');
  document.getElementById('inspectorCard').scrollIntoView({behavior: 'smooth', block: 'nearest'});
}
const CURRENT_CVAT_JOB_ID = """ + json.dumps(s["live_meta"].get("job_id")) + """;
loadJobs();
loadReworkHistory();
if (document.querySelector('#qBody .q-row')) {
  inspectFrame(document.querySelector('#qBody .q-row').dataset.image);
}
"""
    else:
        live_script = """
function inspectFrame() {}
"""

    rework_summary = (
        f"<p><strong>Rework Sub-Job:</strong> <code>{e(Path(rework_info['rework_zip']).name)}</code> "
        f"({rework_info['n_images']} ảnh, {rework_info['n_flagged_boxes']} box gắn <code>can_review=true</code>)</p>"
        if rework_info
        else ""
    )

    nav_bar = (
        """<div class="cvat-top-nav">
  <div class="cvat-nav-inner">
    <a class="cvat-brand" href="http://localhost:8080" target="_blank">CV&#923;T</a>
    <a class="cvat-nav-link" href="http://localhost:8080/projects" target="_blank">Projects</a>
    <a class="cvat-nav-link" href="http://localhost:8080/tasks" target="_blank">Tasks</a>
    <a class="cvat-nav-link" href="http://localhost:8080/jobs" target="_blank">Jobs</a>
    <span class="cvat-nav-link active">AutoQC Pre-flight Station</span>
  </div>
</div>"""
        if not standalone
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>CVAT Local AutoQC Pre-flight Station — Job {e(qc_result.job_id)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Roboto+Flex:opsz,wght@8..144,400;8..144,500;8..144,600;8..144,700&family=Sora:wght@600;700&display=swap" rel="stylesheet">
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: "Roboto Flex", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; background: #f0f2f5; color: rgba(0, 0, 0, 0.88); font-size: 14px; line-height: 1.5; }}
  .cvat-top-nav {{ background: #f0f2f5; border-bottom: 1px solid #d9d9d9; height: 48px; position: sticky; top: 0; z-index: 100; }}
  .cvat-nav-inner {{ max-width: 1320px; margin: 0 auto; height: 100%; padding: 0 24px; display: flex; align-items: center; gap: 22px; }}
  .cvat-brand {{ font-family: "Sora", sans-serif; font-weight: 700; font-size: 18px; letter-spacing: 2px; color: #141414; text-decoration: none; margin-right: 8px; }}
  .cvat-nav-link {{ font-size: 14px; font-weight: 500; color: rgba(0, 0, 0, 0.65); text-decoration: none; height: 48px; display: inline-flex; align-items: center; border-bottom: 2px solid transparent; }}
  .cvat-nav-link:hover {{ color: #1890ff; }}
  .cvat-nav-link.active {{ color: #1890ff; border-bottom-color: #1890ff; font-weight: 600; }}
  .wrap {{ max-width: 1320px; margin: 0 auto; padding: 20px 24px 32px; }}
  .header {{ display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 16px; background: #ffffff; border: 1px solid #d9d9d9; border-radius: 8px; padding: 16px 20px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03); }}
  h1 {{ margin: 0; font-size: 20px; font-weight: 600; color: #141414; }}
  h3 {{ margin: 0 0 12px 0; font-size: 15px; font-weight: 600; color: #141414; }}
  h4 {{ margin: 0 0 8px 0; font-size: 14px; font-weight: 600; color: #141414; }}
  .sub {{ color: rgba(0, 0, 0, 0.55); font-size: 13px; margin-top: 4px; }}
  .badge {{ padding: 4px 12px; border-radius: 4px; font-weight: 600; font-size: 13px; border: 1px solid transparent; }}
  .badge-release {{ background: #f6ffed; border-color: #b7eb8f; color: #389e0d; }}
  .badge-hold {{ background: #fff7e6; border-color: #ffd591; color: #d46b08; }}
  .badge-rework {{ background: #fff1f0; border-color: #ffa39e; color: #cf1322; }}
  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 12px; margin: 0 0 16px 0; }}
  .kpi {{ background: #ffffff; border: 1px solid #d9d9d9; border-radius: 8px; padding: 14px 16px; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03); }}
  .kpi .lbl {{ font-size: 12px; color: rgba(0, 0, 0, 0.55); font-weight: 500; }}
  .kpi .val {{ font-size: 24px; font-weight: 600; margin-top: 4px; color: #1890ff; }}
  .card {{ background: #ffffff; border: 1px solid #d9d9d9; border-radius: 8px; padding: 16px 20px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03); }}
  .toolbar-row {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 10px; }}
  .toolbar-row:last-child {{ margin-bottom: 0; }}
  select, input[type=text] {{ background: #ffffff; border: 1px solid #d9d9d9; color: rgba(0, 0, 0, 0.88); padding: 6px 11px; border-radius: 6px; font-size: 13px; height: 32px; outline: none; transition: border-color 0.2s; }}
  select:hover, input[type=text]:hover, select:focus, input[type=text]:focus {{ border-color: #40a9ff; }}
  select {{ min-width: 320px; flex: 1; }}
  .status-banner {{ display: none; margin-top: 10px; padding: 8px 12px; border-radius: 6px; background: #e6f7ff; border: 1px solid #91d5ff; color: #096dd9; font-size: 13px; }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1.45fr; gap: 16px; }}
  @media (max-width: 900px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 9px 12px; border-bottom: 1px solid #f0f0f0; }}
  th {{ background: #fafafa; color: rgba(0, 0, 0, 0.85); font-weight: 600; border-bottom: 1px solid #f0f0f0; position: sticky; top: 0; z-index: 1; }}
  .clickable {{ cursor: pointer; transition: background 0.15s; }}
  .clickable:hover {{ background: #e6f7ff; }}
  code {{ background: #f5f5f5; border: 1px solid #e8e8e8; padding: 1px 6px; border-radius: 4px; color: #096dd9; font-size: 12px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  .sev {{ padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: 600; border: 1px solid transparent; }}
  .sev-error {{ background: #fff1f0; border-color: #ffa39e; color: #cf1322; }}
  .sev-warn {{ background: #fffbe6; border-color: #ffe58f; color: #d48806; }}
  .inspector-head {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }}
  .inspector-body {{ display: grid; grid-template-columns: 1.4fr 1fr; gap: 16px; align-items: start; }}
  @media (max-width: 900px) {{ .inspector-body {{ grid-template-columns: 1fr; }} }}
  .inspector-img-wrap {{ background: #fafafa; border: 1px solid #d9d9d9; border-radius: 6px; padding: 8px; text-align: center; }}
  .inspector-img-wrap img {{ max-width: 100%; max-height: 460px; object-fit: contain; border-radius: 4px; }}
  .gallery {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }}
  .viz-card {{ background: #ffffff; border: 1px solid #d9d9d9; border-radius: 6px; overflow: hidden; transition: border-color 0.2s, box-shadow 0.2s; }}
  .viz-card:hover {{ border-color: #40a9ff; box-shadow: 0 2px 8px rgba(24, 144, 255, 0.15); }}
  .viz-card img {{ width: 100%; height: 205px; object-fit: contain; background: #fafafa; display: block; border-bottom: 1px solid #f0f0f0; }}
  .viz-meta {{ padding: 10px 12px; font-size: 12px; color: rgba(0, 0, 0, 0.78); }}
  .filter-bar {{ display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }}
  .btn {{ display: inline-flex; align-items: center; justify-content: center; gap: 5px; height: 32px; padding: 0 13px; border-radius: 6px; text-decoration: none; font-size: 13px; font-weight: 500; cursor: pointer; border: 1px solid transparent; transition: all 0.2s; }}
  .btn-primary {{ background: #1890ff; border-color: #1890ff; color: #ffffff; }}
  .btn-primary:hover {{ background: #40a9ff; border-color: #40a9ff; }}
  .btn-secondary {{ background: #13c2c2; border-color: #13c2c2; color: #ffffff; }}
  .btn-secondary:hover {{ background: #36cfc9; border-color: #36cfc9; }}
  .btn-accent {{ background: #722ed1; border-color: #722ed1; color: #ffffff; }}
  .btn-accent:hover {{ background: #9254de; border-color: #9254de; }}
  .btn-warn {{ background: #fa8c16; border-color: #fa8c16; color: #ffffff; }}
  .btn-warn:hover {{ background: #ffa940; border-color: #ffa940; }}
  .btn-outline {{ background: #ffffff; border: 1px solid #d9d9d9; color: rgba(0, 0, 0, 0.85); }}
  .btn-outline:hover {{ border-color: #40a9ff; color: #1890ff; }}
  .scroll-table {{ max-height: 360px; overflow-y: auto; border: 1px solid #f0f0f0; border-radius: 6px; }}
</style>
</head>
<body>
{nav_bar}
<div class="wrap">
  <div class="header">
    <div>
      <h1>CVAT Local AutoQC Pre-flight Station — Job: {e(qc_result.job_id)}</h1>
      <div class="sub">Annotator: <strong>{e(qc_result.annotator)}</strong> · Quét lúc: {e(qc_result.timestamp)} · Thời gian xử lý: {qc_result.elapsed_sec}s</div>
    </div>
    <span class="badge {badge_cls}">{e(dec)}</span>
  </div>

  {live_toolbar}

  <div class="kpis">
    <div class="kpi"><div class="lbl">Quality Score</div><div class="val">{qc_result.quality_score}/100</div></div>
    <div class="kpi"><div class="lbl">Ảnh Nhận / Quét</div><div class="val">{qc_result.n_accepted}/{qc_result.n_scanned}</div></div>
    <div class="kpi"><div class="lbl">Lỗi Thô (v1 Error)</div><div class="val">{s['n_v1_errors']}</div></div>
    <div class="kpi"><div class="lbl">Máy Tự Sửa (Autofix)</div><div class="val">{s['n_fixes']}</div></div>
    <div class="kpi"><div class="lbl">Hàng Đợi QC (v2)</div><div class="val">{s['queue_size']}</div></div>
  </div>

  <div class="card">
    <p><strong>Lý do Gate:</strong> {e('; '.join(verdict['reasons']))}</p>
    {rework_summary}
    <p><strong>Bộ lọc nhanh trên CVAT Local:</strong> <code>attr["can_review"] == true</code> · <code>width &lt; 20</code> · <code>label == "truck"</code></p>
  </div>

  {inspector_panel}
  {history_panel}

  <div class="grid-2">
    <div class="card">
      <h3>Thống kê lỗi ban đầu (v1)</h3>
      <table><thead><tr><th>Mức</th><th>Mã lỗi</th><th>Số lượng</th></tr></thead><tbody>{code_rows_html}</tbody></table>
    </div>
    <div class="card">
      <h3>Hàng đợi ưu tiên sửa &amp; kiểm tra ({s['queue_size']} ảnh)</h3>
      <div class="filter-bar">
        <input id="qFilter" type="text" style="flex:1" placeholder="Lọc theo tên ảnh hoặc mã lỗi (vd: POSSIBLE_WRONG_CLASS, NEEDS_LABEL)..." oninput="filterQueue()">
      </div>
      <div class="scroll-table">
        <table><thead><tr><th>Ảnh</th><th>Ưu tiên</th><th>Mã cảnh báo</th></tr></thead><tbody id="qBody">{queue_rows_html}</tbody></table>
      </div>
    </div>
  </div>

  <div class="card">
    <h3>Trực quan hóa Top {len(viz_items)} ảnh ưu tiên (Nét liền: Nhãn hiện tại | Nét đứt vàng: Gợi ý AI yolo26l)</h3>
    <div class="gallery">{gallery_html}</div>
  </div>
</div>
<script>
function filterQueue() {{
  const val = document.getElementById('qFilter').value.toLowerCase();
  for (const tr of document.querySelectorAll('#qBody .q-row')) {{
    const txt = tr.textContent.toLowerCase();
    tr.style.display = txt.includes(val) ? '' : 'none';
  }}
}}
function filterByCode(code) {{
  const inp = document.getElementById('qFilter');
  if (inp) {{ inp.value = code; filterQueue(); }}
}}
{live_script}
</script>
</body>
</html>
"""


def build_qc_bundle(
    qc_result: JobQCResult,
    out_dir: str | Path,
    top_viz: int = 12,
    rework_info: dict | None = None,
) -> dict:
    """Đóng gói toàn bộ báo cáo HTML, Markdown, CSV, ảnh viz và Rework Job thành `qc_handover_<job_id>.zip`."""
    out_path = Path(out_dir)
    bundle_dir = out_path / f"qc_bundle_{qc_result.job_id}"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)

    if rework_info is None:
        rework_info = create_rework_job(qc_result, out_path / "rework")

    viz_items = render_qc_viz(qc_result, bundle_dir / "viz", top_n=top_viz)

    md_text = build_markdown_report(qc_result, rework_info)
    (bundle_dir / "qc_report.md").write_text(md_text, encoding="utf-8")

    html_standalone = build_html_report(qc_result, viz_items, rework_info, standalone=True)
    (bundle_dir / "qc_report.html").write_text(html_standalone, encoding="utf-8")

    html_live = build_html_report(qc_result, viz_items, rework_info, standalone=False)
    (out_path / "dashboard.html").write_text(html_live, encoding="utf-8")

    rep_dst = bundle_dir / "reports"
    shutil.copytree(qc_result.reports_dir, rep_dst)

    rework_zip = Path(rework_info["rework_zip"])
    if rework_zip.exists():
        shutil.copy2(rework_zip, bundle_dir / rework_zip.name)
    xml_path = Path(rework_info["cvat_xml"])
    if xml_path.exists():
        shutil.copy2(xml_path, bundle_dir / xml_path.name)
    labels_json = ROOT / "cvat" / "labels.json"
    if labels_json.exists():
        shutil.copy2(labels_json, bundle_dir / "cvat_labels.json")
    hist_dir = out_path / "history"
    for hfile in ("rework_history.json", "rework_history.csv"):
        hp = hist_dir / hfile
        if hp.exists():
            shutil.copy2(hp, bundle_dir / hfile)

    handover_zip = out_path / f"qc_handover_{qc_result.job_id}.zip"
    with zipfile.ZipFile(handover_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(bundle_dir.rglob("*")):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(bundle_dir).as_posix())

    return {
        "bundle_dir": str(bundle_dir),
        "handover_zip": str(handover_zip),
        "html_report": str(bundle_dir / "qc_report.html"),
        "md_report": str(bundle_dir / "qc_report.md"),
        "dashboard_html": str(out_path / "dashboard.html"),
        "viz_dir": str(bundle_dir / "viz"),
        "rework_zip": str(rework_zip),
        "n_viz": len(viz_items),
    }
