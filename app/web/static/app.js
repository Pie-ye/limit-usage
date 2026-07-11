(() => {
  const cardsEl = document.getElementById("cards");
  const serverTimeEl = document.getElementById("server-time");
  const nextPollEl = document.getElementById("next-poll");
  const refreshBtn = document.getElementById("refresh-btn");
  const refreshMsg = document.getElementById("refresh-msg");

  let latest = null;

  function parseIso(value) {
    if (!value) return null;
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function fmtTime(d) {
    if (!d) return "—";
    return d.toLocaleString();
  }

  function fmtCountdown(target) {
    if (!target) return "無週期重置";
    const ms = target.getTime() - Date.now();
    if (ms <= 0) return "已可刷新 / 已重置";
    const totalSec = Math.floor(ms / 1000);
    const days = Math.floor(totalSec / 86400);
    const hours = Math.floor((totalSec % 86400) / 3600);
    const mins = Math.floor((totalSec % 3600) / 60);
    const secs = totalSec % 60;
    const parts = [];
    if (days) parts.push(`${days}d`);
    parts.push(`${hours}h`, `${String(mins).padStart(2, "0")}m`, `${String(secs).padStart(2, "0")}s`);
    return parts.join(" ");
  }

  function statusBadge(status) {
    return `<span class="badge ${status}">${status}</span>`;
  }

  function remainingPct(win) {
    if (win.remaining_percent != null) return Number(win.remaining_percent);
    if (win.used_percent != null) return Math.max(0, 100 - Number(win.used_percent));
    return null;
  }

  function renderWindow(win) {
    const rem = remainingPct(win);
    const isBalance = win.key && String(win.key).startsWith("balance");
    const extra = win.raw_extra || {};

    if (isBalance || win.amount != null) {
      const granted = extra.granted_balance;
      const topped = extra.topped_up_balance;
      return `
        <div class="window">
          <div class="window-title">
            <span class="label">${escapeHtml(win.label || "Balance")}</span>
            <span class="pct">${win.currency || ""}</span>
          </div>
          <div class="balance-line">${escapeHtml(String(win.amount ?? "—"))}</div>
          <div class="balance-sub">
            Paid/Topped: ${escapeHtml(String(topped ?? "—"))}
            · Granted: ${escapeHtml(String(granted ?? "—"))}
          </div>
          <div class="countdown">重置：按量扣款，無固定週期</div>
        </div>`;
    }

    const width = rem == null ? 0 : Math.max(0, Math.min(100, rem));
    const low = rem != null && rem <= 20;
    const resetAt = parseIso(win.resets_at);
    return `
      <div class="window" data-reset="${win.resets_at || ""}">
        <div class="window-title">
          <span class="label">${escapeHtml(win.label || win.key)}</span>
          <span class="pct">${rem == null ? "—" : rem.toFixed(1) + "% 剩餘"}</span>
        </div>
        <div class="bar ${low ? "low" : ""}"><span style="width:${width}%"></span></div>
        <div class="countdown">
          重置倒數：<strong class="cd">${fmtCountdown(resetAt)}</strong>
          ${resetAt ? `<span> · ${fmtTime(resetAt)}</span>` : ""}
        </div>
      </div>`;
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function renderCards(data) {
    latest = data;
    serverTimeEl.textContent = fmtTime(parseIso(data.server_time));

    const snaps = data.snapshots || [];
    let nextPoll = null;
    for (const s of snaps) {
      const n = parseIso(s.next_poll_at);
      if (n && (!nextPoll || n < nextPoll)) nextPoll = n;
    }
    nextPollEl.textContent = nextPoll ? `${fmtTime(nextPoll)} (${fmtCountdown(nextPoll)})` : "—";

    if (!snaps.length) {
      cardsEl.innerHTML = `<p class="loading">尚無資料，等待首次輪詢…</p>`;
      return;
    }

    cardsEl.innerHTML = snaps
      .map((s) => {
        const windows = (s.windows || []).map(renderWindow).join("") ||
          `<p class="message">此 provider 目前沒有額度視窗資料</p>`;
        return `
          <article class="card" data-provider="${escapeHtml(s.provider)}">
            <div class="card-head">
              <div>
                <h2>${escapeHtml(s.display_name || s.provider)}</h2>
                <div class="hint">${escapeHtml(s.account_hint || "")}${s.source ? " · " + escapeHtml(s.source) : ""}</div>
              </div>
              ${statusBadge(s.status)}
            </div>
            ${s.message ? `<p class="message">${escapeHtml(s.message)}</p>` : ""}
            <div class="hint">上次抓取：${fmtTime(parseIso(s.fetched_at))}</div>
            ${windows}
          </article>`;
      })
      .join("");
  }

  function tickCountdowns() {
    if (!latest) return;
    // Re-render countdown texts without full rebuild for smoothness
    document.querySelectorAll(".window[data-reset]").forEach((el) => {
      const raw = el.getAttribute("data-reset");
      const cd = el.querySelector(".cd");
      if (cd) cd.textContent = fmtCountdown(parseIso(raw));
    });
    // Update next poll countdown in header
    const snaps = latest.snapshots || [];
    let nextPoll = null;
    for (const s of snaps) {
      const n = parseIso(s.next_poll_at);
      if (n && (!nextPoll || n < nextPoll)) nextPoll = n;
    }
    if (nextPoll) {
      nextPollEl.textContent = `${fmtTime(nextPoll)} (${fmtCountdown(nextPoll)})`;
    }
  }

  async function loadUsage() {
    try {
      const res = await fetch("/api/usage", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      renderCards(data);
      refreshMsg.textContent = "";
    } catch (err) {
      refreshMsg.textContent = `載入失敗：${err.message}`;
    }
  }

  async function doRefresh() {
    refreshBtn.disabled = true;
    refreshMsg.textContent = "刷新中…";
    try {
      const res = await fetch("/api/refresh", { method: "POST" });
      const data = await res.json();
      if (data.warning) {
        refreshMsg.textContent = data.warning;
      } else {
        refreshMsg.textContent = "已刷新";
      }
      await loadUsage();
    } catch (err) {
      refreshMsg.textContent = `刷新失敗：${err.message}`;
    } finally {
      refreshBtn.disabled = false;
    }
  }

  refreshBtn.addEventListener("click", doRefresh);
  loadUsage();
  setInterval(loadUsage, 20000);
  setInterval(tickCountdowns, 1000);
})();
