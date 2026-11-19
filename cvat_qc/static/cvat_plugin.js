(function () {
  if (window.__AUTOQC_CVAT_PLUGIN__) return;
  window.__AUTOQC_CVAT_PLUGIN__ = true;

  const STATION_URL = "http://127.0.0.1:8765";

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function detectJobFromUrl() {
    const m = window.location.pathname.match(/\/tasks\/(\d+)\/jobs\/(\d+)/);
    return m ? { taskId: Number(m[1]), jobId: Number(m[2]) } : null;
  }

  const style = document.createElement("style");
  style.textContent = `
    #autoqc-fab {
      position: fixed; right: 20px; bottom: 20px; z-index: 99999;
      background: #ffffff; color: rgba(0, 0, 0, 0.85);
      border: 1px solid #d9d9d9; border-radius: 6px;
      height: 36px; padding: 0 14px; font-weight: 600; font-size: 13px;
      box-shadow: 0 3px 12px rgba(0, 0, 0, 0.12);
      cursor: pointer; display: inline-flex; align-items: center; gap: 8px;
      font-family: "Roboto Flex", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      transition: all 0.2s;
    }
    #autoqc-fab:hover { border-color: #1890ff; color: #1890ff; box-shadow: 0 4px 16px rgba(24, 144, 255, 0.2); }
    #autoqc-fab::before {
      content: ""; width: 8px; height: 8px; border-radius: 50%; background: #1890ff; display: inline-block;
    }
    #autoqc-nav-btn {
      background: transparent; border: none; color: rgba(0, 0, 0, 0.65);
      font-weight: 500; font-size: 14px; cursor: pointer; padding: 0 12px; height: 48px;
      display: inline-flex; align-items: center; gap: 6px;
      font-family: "Roboto Flex", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    #autoqc-nav-btn:hover { color: #1890ff; }
    #autoqc-drawer {
      position: fixed; top: 0; right: 0; width: 580px; max-width: 96vw; height: 100vh;
      background: #ffffff; color: rgba(0, 0, 0, 0.88); z-index: 100000;
      box-shadow: -6px 0 24px rgba(0, 0, 0, 0.12);
      display: none; flex-direction: column;
      font-family: "Roboto Flex", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      border-left: 1px solid #f0f0f0;
    }
    #autoqc-drawer.open { display: flex; }
    .aq-head {
      padding: 14px 18px; border-bottom: 1px solid #f0f0f0;
      display: flex; justify-content: space-between; align-items: center;
      background: #fafafa;
    }
    .aq-head h3 { margin: 0; font-size: 15px; font-weight: 600; color: #141414; }
    .aq-body { padding: 14px 18px; overflow-y: auto; flex: 1; font-size: 13px; background: #f0f2f5; }
    .aq-card {
      background: #ffffff; border: 1px solid #d9d9d9; border-radius: 6px;
      padding: 12px 14px; margin-bottom: 12px; box-shadow: 0 1px 2px rgba(0,0,0,0.02);
    }
    .aq-kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 12px; }
    .aq-kpi {
      background: #ffffff; border: 1px solid #d9d9d9; border-radius: 6px;
      padding: 9px 8px; text-align: center;
    }
    .aq-kpi .l { font-size: 11px; color: rgba(0, 0, 0, 0.5); font-weight: 500; }
    .aq-kpi .v { font-size: 18px; font-weight: 600; color: #1890ff; margin-top: 2px; }
    .aq-btn {
      height: 30px; padding: 0 11px; border-radius: 6px; border: 1px solid transparent; cursor: pointer;
      font-weight: 500; font-size: 12px; text-decoration: none;
      display: inline-flex; align-items: center; justify-content: center; gap: 4px; transition: all 0.2s;
    }
    .aq-btn-pri { background: #1890ff; border-color: #1890ff; color: #ffffff; }
    .aq-btn-pri:hover { background: #40a9ff; border-color: #40a9ff; color: #ffffff; }
    .aq-btn-warn { background: #fa8c16; border-color: #fa8c16; color: #ffffff; }
    .aq-btn-warn:hover { background: #ffa940; border-color: #ffa940; color: #ffffff; }
    .aq-btn-acc { background: #722ed1; border-color: #722ed1; color: #ffffff; }
    .aq-btn-acc:hover { background: #9254de; border-color: #9254de; color: #ffffff; }
    .aq-btn-sec { background: #13c2c2; border-color: #13c2c2; color: #ffffff; }
    .aq-btn-sec:hover { background: #36cfc9; border-color: #36cfc9; color: #ffffff; }
    .aq-btn-out { background: #ffffff; border-color: #d9d9d9; color: rgba(0, 0, 0, 0.85); }
    .aq-btn-out:hover { border-color: #40a9ff; color: #1890ff; }
    .aq-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 8px; }
    .aq-select {
      flex: 1; min-width: 190px; height: 30px; background: #ffffff; color: rgba(0, 0, 0, 0.88);
      border: 1px solid #d9d9d9; border-radius: 6px; padding: 0 8px; font-size: 12px; outline: none;
    }
    .aq-select:hover, .aq-select:focus { border-color: #40a9ff; }
    .aq-badge { padding: 2px 9px; border-radius: 4px; font-weight: 600; font-size: 11px; border: 1px solid transparent; }
    .aq-RELEASE { background: #f6ffed; border-color: #b7eb8f; color: #389e0d; }
    .aq-HOLD { background: #fff7e6; border-color: #ffd591; color: #d46b08; }
    .aq-REWORK { background: #fff1f0; border-color: #ffa39e; color: #cf1322; }
    .aq-table { width: 100%; border-collapse: collapse; font-size: 12px; table-layout: fixed; }
    .aq-table th { background: #fafafa; color: rgba(0, 0, 0, 0.78); font-weight: 600; border-bottom: 1px solid #f0f0f0; padding: 7px 6px; text-align: left; }
    .aq-table td { text-align: left; padding: 7px 6px; border-bottom: 1px solid #f0f0f0; vertical-align: middle; color: rgba(0, 0, 0, 0.85); }
    .aq-table tr:hover td { background: #e6f7ff; }
    .aq-code {
      background: #f5f5f5; border: 1px solid #e8e8e8; padding: 1px 5px; border-radius: 4px;
      color: #096dd9; font-size: 11px; word-break: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    #aq-preview img { max-width: 100%; max-height: 240px; object-fit: contain; display: block; margin: 8px auto 0; border-radius: 6px; border: 1px solid #d9d9d9; background: #fafafa; }
  `;
  document.head.appendChild(style);

  const fab = document.createElement("button");
  fab.id = "autoqc-fab";
  fab.textContent = "AutoQC Station";
  document.body.appendChild(fab);

  const drawer = document.createElement("div");
  drawer.id = "autoqc-drawer";
  drawer.innerHTML = `
    <div class="aq-head">
      <h3>CVAT AutoQC Pre-flight Station</h3>
      <div class="aq-row" style="margin:0">
        <a class="aq-btn aq-btn-out" href="${STATION_URL}" target="_blank">↗ Full Dashboard</a>
        <button id="aq-close" class="aq-btn aq-btn-out">✕</button>
      </div>
    </div>
    <div class="aq-body">
      <div class="aq-card">
        <div class="aq-row">
          <select id="aq-job-sel" class="aq-select"></select>
          <button id="aq-scan-btn" class="aq-btn aq-btn-pri">Quét lỗi</button>
        </div>
        <div id="aq-status" style="font-size:12px;color:#096dd9;"></div>
      </div>
      <div id="aq-summary-box"></div>
      <div class="aq-card" id="aq-actions-card" style="display:none">
        <div class="aq-row">
          <select id="aq-rework-issue-sel" class="aq-select">
            <option value="ALL">Rework: Tất cả lỗi ưu tiên</option>
            <option value="SPLIT_ALL">Rework: Tách mỗi loại Issue 1 Task</option>
          </select>
          <button id="aq-rework-btn" class="aq-btn aq-btn-acc">+ Tạo Rework Job</button>
        </div>
        <div class="aq-row">
          <button id="aq-flag-btn" class="aq-btn aq-btn-warn">Gắn can_review &amp; Tạo Issue</button>
          <button id="aq-track-btn" class="aq-btn aq-btn-pri">Ghi Lịch Sử Sửa</button>
        </div>
        <div class="aq-row" style="margin-bottom:0">
          <a class="aq-btn aq-btn-sec" href="${STATION_URL}/download/handover">Tải QC Handover (.zip)</a>
          <a class="aq-btn aq-btn-out" href="${STATION_URL}/download/rework">Tải Rework (.zip)</a>
          <a class="aq-btn aq-btn-out" href="${STATION_URL}/download/rework-history">CSV Lịch sử</a>
        </div>
      </div>
      <div id="aq-preview" class="aq-card" style="display:none"></div>
      <div id="aq-history-box"></div>
      <div id="aq-queue-box"></div>
    </div>
  `;
  document.body.appendChild(drawer);

  const statusEl = drawer.querySelector("#aq-status");
  const jobSel = drawer.querySelector("#aq-job-sel");
  const reworkIssueSel = drawer.querySelector("#aq-rework-issue-sel");
  const summaryBox = drawer.querySelector("#aq-summary-box");
  const actionsCard = drawer.querySelector("#aq-actions-card");
  const historyBox = drawer.querySelector("#aq-history-box");
  const queueBox = drawer.querySelector("#aq-queue-box");
  const previewBox = drawer.querySelector("#aq-preview");

  function toggleDrawer() {
    drawer.classList.toggle("open");
    if (drawer.classList.contains("open")) refreshJobs();
  }

  function ensureHeaderButton() {
    if (document.getElementById("autoqc-nav-btn")) return;
    const host = document.querySelector(".cvat-header-left-section") || document.querySelector("header.ant-layout-header");
    if (!host) return;
    const navBtn = document.createElement("button");
    navBtn.id = "autoqc-nav-btn";
    navBtn.textContent = "AutoQC";
    navBtn.addEventListener("click", toggleDrawer);
    host.appendChild(navBtn);
  }

  function updateFabLabel() {
    const cur = detectJobFromUrl();
    fab.textContent = cur ? `AutoQC Job #${cur.jobId}` : "AutoQC Station";
    ensureHeaderButton();
  }
  setInterval(updateFabLabel, 1200);

  async function refreshJobs() {
    const cur = detectJobFromUrl();
    try {
      const r = await fetch(`${STATION_URL}/api/jobs`);
      const data = await r.json();
      jobSel.innerHTML = "";
      const optRaw = document.createElement("option");
      optRaw.value = "local:data/raw";
      optRaw.textContent = "Dataset: data/raw (224 ảnh mẫu Day 09)";
      jobSel.appendChild(optRaw);
      for (const j of data.jobs || []) {
        const opt = document.createElement("option");
        opt.value = `cvat:${j.id}`;
        opt.textContent = `Job #${j.id} — ${j.task_name} (${j.frame_count} ảnh)`;
        if (cur && Number(cur.jobId) === Number(j.id)) opt.selected = true;
        jobSel.appendChild(opt);
      }
      await loadCurrentSummary();
      await loadHistoryBox();
    } catch (e) {
      statusEl.innerHTML = `Chưa kết nối được AutoQC Server (<code>${STATION_URL}</code>). Hãy chạy <code>python scripts/cvat_autoqc.py --serve</code>.`;
    }
  }

  async function loadCurrentSummary() {
    try {
      const r = await fetch(`${STATION_URL}/api/summary`);
      if (!r.ok) return;
      const s = await r.json();
      renderSummary(s);
    } catch (e) {}
  }

  async function loadHistoryBox() {
    try {
      const r = await fetch(`${STATION_URL}/api/rework-history`);
      if (!r.ok) return;
      const d = await r.json();
      renderHistory(d.history || []);
    } catch (e) {}
  }

  function renderHistory(hist) {
    if (!hist || !hist.length) {
      historyBox.innerHTML = "";
      return;
    }
    const items = [];
    for (const rev of [...hist].reverse().slice(0, 5)) {
      if (!rev.events || !rev.events.length) {
        items.push(`<li><strong>Phiên #${esc(rev.revision)}</strong> (${esc(rev.target_job_id)}): chưa có thay đổi mới</li>`);
      } else {
        for (const ev of rev.events.slice(0, 6)) {
          items.push(`<li><strong>#${esc(rev.revision)}</strong> <code class="aq-code">${esc(ev.image)}</code> [<code class="aq-code">${esc(ev.change_type)}</code>]: ${esc(ev.detail)}</li>`);
        }
      }
    }
    historyBox.innerHTML = `
      <div class="aq-card">
        <strong>Lịch sử chỉnh sửa Rework (${esc(hist.length)} phiên)</strong>
        <ul style="margin:6px 0 0 16px;padding:0;font-size:11px;color:rgba(0,0,0,0.75)">${items.join("")}</ul>
      </div>
    `;
  }

  function renderSummary(s) {
    const v = s.verdict || { decision: "HOLD", reasons: [] };
    const dec = String(v.decision || "").toUpperCase();
    actionsCard.style.display = "block";

    const curFilter = reworkIssueSel.value;
    const codes = (s.code_summary_v2 || []).map((x) => x.code);
    reworkIssueSel.innerHTML = `
      <option value="ALL">Rework: Tất cả lỗi ưu tiên</option>
      <option value="SPLIT_ALL">Rework: Tách mỗi loại Issue 1 Task</option>
      ${codes.map((c) => `<option value="${esc(c)}">Chỉ lỗi: ${esc(c)}</option>`).join("")}
    `;
    if ([...reworkIssueSel.options].some((o) => o.value === curFilter)) reworkIssueSel.value = curFilter;

    summaryBox.innerHTML = `
      <div class="aq-kpis">
        <div class="aq-kpi"><div class="l">Score</div><div class="v">${esc(s.quality_score)}</div></div>
        <div class="aq-kpi"><div class="l">Ảnh OK</div><div class="v">${esc(s.n_accepted)}/${esc(s.n_scanned)}</div></div>
        <div class="aq-kpi"><div class="l">Lỗi v1</div><div class="v">${esc(s.n_v1_errors)}</div></div>
        <div class="aq-kpi"><div class="l">Hàng đợi</div><div class="v">${esc(s.queue_size)}</div></div>
      </div>
      <div class="aq-card">
        <div class="aq-row" style="justify-content:space-between">
          <strong>Job: ${esc(s.job_id)}</strong>
          <span class="aq-badge aq-${esc(dec)}">${esc(dec)}</span>
        </div>
        <div style="color:rgba(0,0,0,0.68);font-size:12px;">${esc((v.reasons || []).join("; "))}</div>
        <div style="margin-top:6px;font-size:11px;color:rgba(0,0,0,0.5);">
          CVAT Filter: <code class="aq-code">attr["can_review"] == true</code> · <code class="aq-code">width &lt; 20</code>
        </div>
      </div>
    `;
    const rows = (s.review_queue || []).slice(0, 20).map((q) => {
      const jumpBtn = q.frame !== null && q.frame !== undefined && s.live_meta?.is_live
        ? `<a class="aq-btn aq-btn-out" style="height:24px;padding:0 7px;font-size:11px" href="/tasks/${esc(s.live_meta.task_id)}/jobs/${esc(s.live_meta.job_id)}?frame=${esc(q.frame)}">Frame ${esc(q.frame)}</a>`
        : "";
      return `<tr>
        <td><code class="aq-code">${esc(q.image)}</code></td>
        <td>${esc(q.priority)}</td>
        <td><code class="aq-code">${esc(q.codes)}</code></td>
        <td style="white-space:nowrap">
          <button class="aq-btn aq-btn-pri aq-inspect-btn" data-stem="${esc(q.image)}" style="height:24px;padding:0 8px;font-size:11px">Soi</button>
          ${jumpBtn}
        </td>
      </tr>`;
    }).join("");
    queueBox.innerHTML = `
      <div class="aq-card">
        <strong>Top ưu tiên cần kiểm tra (${esc(s.queue_size)} ảnh)</strong>
        <table class="aq-table" style="margin-top:8px">
          <thead><tr><th style="width:74px">Ảnh</th><th style="width:48px">Điểm</th><th>Mã lỗi</th><th style="width:108px"></th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;
    queueBox.querySelectorAll(".aq-inspect-btn").forEach((btn) => {
      btn.addEventListener("click", () => inspectStem(btn.dataset.stem));
    });
  }

  async function inspectStem(stem) {
    previewBox.style.display = "block";
    previewBox.innerHTML = `<strong>Đang vẽ đối chiếu ${esc(stem)}...</strong>`;
    const r = await fetch(`${STATION_URL}/api/frame-detail?image=${encodeURIComponent(stem)}`);
    const d = await r.json();
    const iss = (d.issues || []).map((i) => `<li><code class="aq-code">${esc(i.code)}</code>: ${esc(i.message)}</li>`).join("");
    previewBox.innerHTML = `
      <div class="aq-row" style="justify-content:space-between">
        <strong>${esc(stem)}</strong>
        <button class="aq-btn aq-btn-out" style="height:24px;padding:0 8px" onclick="this.closest('#aq-preview').style.display='none'">Đóng</button>
      </div>
      <img src="${STATION_URL}/viz/${encodeURIComponent(stem)}.png?t=${Date.now()}" crossorigin="anonymous" alt="${esc(stem)}">
      <ul style="margin:8px 0 0 16px;padding:0;font-size:11px;color:rgba(0,0,0,0.78)">${iss || "<li>Không còn lỗi</li>"}</ul>
    `;
  }

  fab.addEventListener("click", toggleDrawer);
  drawer.querySelector("#aq-close").addEventListener("click", () => drawer.classList.remove("open"));

  drawer.querySelector("#aq-scan-btn").addEventListener("click", async () => {
    const val = jobSel.value;
    statusEl.textContent = "Đang quét lỗi Job qua AutoQC...";
    const body = val.startsWith("cvat:")
      ? { mode: "cvat", job_id: Number(val.slice(5)) }
      : { mode: "local", src: val.slice(6), job_id: "job_raw_224" };
    const r = await fetch(`${STATION_URL}/api/scan`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await r.json();
    if (data.ok) {
      statusEl.textContent = "Đã quét xong!";
      renderSummary(data.summary);
      loadHistoryBox();
    } else {
      statusEl.textContent = `Lỗi: ${data.message}`;
    }
  });

  drawer.querySelector("#aq-flag-btn").addEventListener("click", async () => {
    statusEl.textContent = "Đang gắn can_review=true & tạo Issues trên CVAT...";
    const r = await fetch(`${STATION_URL}/api/cvat/flag`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const data = await r.json();
    statusEl.textContent = data.ok ? `${data.message} (Tải lại trang CVAT để xem Issues)` : `Lỗi: ${data.message}`;
  });

  drawer.querySelector("#aq-rework-btn").addEventListener("click", async () => {
    const fval = reworkIssueSel.value || "ALL";
    const payload = { max_images: 40 };
    if (fval === "SPLIT_ALL") {
      payload.split_by_code = true;
      statusEl.textContent = "Đang tách riêng từng Rework Task theo mỗi loại Issue trên CVAT Local...";
    } else if (fval !== "ALL") {
      payload.issue_codes = [fval];
      statusEl.textContent = `Đang tạo Rework Job chỉ lọc lỗi ${fval}...`;
    } else {
      statusEl.textContent = "Đang tạo Rework Job mới trên CVAT Local...";
    }
    const r = await fetch(`${STATION_URL}/api/cvat/create-rework`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (data.ok) {
      if (data.split_by_code && Array.isArray(data.tasks)) {
        const links = data.tasks.map((t) => {
          const u = t.cvat_task?.url || "";
          const m = u.match(/\/tasks\/\d+\/jobs\/\d+/);
          const href = m ? m[0] : u;
          return `<a href="${esc(href)}" style="color:#1890ff;font-weight:600">[${esc((t.issue_codes || []).join(","))}] (${esc(t.n_images)} ảnh)</a>`;
        }).join(" · ");
        statusEl.innerHTML = `Đã tách ${esc(data.tasks.length)} Rework Task: ${links}`;
      } else if (data.cvat_task?.url) {
        const m = data.cvat_task.url.match(/\/tasks\/\d+\/jobs\/\d+/);
        const href = m ? m[0] : data.cvat_task.url;
        statusEl.innerHTML = `Đã tạo Rework Job (${esc(data.n_images)} ảnh): <a href="${esc(href)}" style="color:#1890ff;font-weight:600">Mở ngay Rework Job →</a>`;
      } else {
        statusEl.textContent = data.message;
      }
    } else {
      statusEl.textContent = `Lỗi: ${data.message || "Không tạo được Rework Job"}`;
    }
  });

  drawer.querySelector("#aq-track-btn").addEventListener("click", async () => {
    const cur = detectJobFromUrl();
    const selVal = jobSel.value;
    const jid = cur ? cur.jobId : (selVal.startsWith("cvat:") ? Number(selVal.slice(5)) : null);
    if (!jid) {
      statusEl.textContent = "Hãy mở hoặc chọn một CVAT Job live để đối chiếu lịch sử sửa.";
      return;
    }
    statusEl.textContent = `Đang kéo nhãn mới nhất từ CVAT Job #${jid} để ghi lịch sử sửa...`;
    const r = await fetch(`${STATION_URL}/api/cvat/track-rework`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cvat_job_id: jid, baseline_key: `cvat_${jid}` }),
    });
    const data = await r.json();
    if (data.ok) {
      statusEl.textContent = data.message;
      renderHistory(data.history || []);
    } else {
      statusEl.textContent = `Lỗi: ${data.message}`;
    }
  });
})();
