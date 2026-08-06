(() => {
  const cardsEl = document.getElementById("cards");
  const trendsEl = document.getElementById("trends");
  const serverTimeEl = document.getElementById("server-time");
  const nextPollEl = document.getElementById("next-poll");
  const refreshBtn = document.getElementById("refresh-btn");
  const refreshMsg = document.getElementById("refresh-msg");

  let latest = null;
  let trendsData = null;
  const chartInstances = {};

  const LOW_REM = 20;
  const CRIT_REM = 10;
  const RESET_SOON_H = 2;
  const RESET_CRIT_H = 0.5;
  const DEEPSEEK_CNY_WARN = 10;

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

  function hoursUntil(target) {
    if (!target) return null;
    return (target.getTime() - Date.now()) / 3600000;
  }

  function remainingPct(win) {
    if (win.remaining_percent != null) return Number(win.remaining_percent);
    if (win.used_percent != null) return Math.max(0, 100 - Number(win.used_percent));
    return null;
  }

  function isWeeklyWindow(win) {
    const key = String(win.key || "").toLowerCase();
    const label = String(win.label || "").toLowerCase();
    if (key === "1w" || key === "weekly" || key.startsWith("1w")) return true;
    if (label.includes("week")) return true;
    const lim = Number(win.limit_window_seconds);
    if (Number.isFinite(lim) && Math.abs(lim - 604800) <= 600) return true;
    return false;
  }

  function urgencyLevel(win) {
    let level = "ok";
    const reasons = [];
    const rem = remainingPct(win);
    const isBalance = win.key && String(win.key).startsWith("balance");

    // DeepSeek: only CNY, warn when < 10; no reset reminders
    if (isBalance) {
      const cur = String(win.currency || "").toUpperCase();
      if (cur && cur !== "CNY") return { level: "ok", reasons: [] };
      if (win.amount != null) {
        const amt = Number(win.amount);
        if (Number.isFinite(amt) && amt < DEEPSEEK_CNY_WARN) {
          level = amt <= 5 ? "critical" : "low";
          reasons.push(`CNY 餘額 ${amt.toFixed(2)}（低於 ${DEEPSEEK_CNY_WARN}）`);
        }
      }
      return { level, reasons };
    }

    // Rate limits: only Weekly (skip 5h / product rows)
    if (!isWeeklyWindow(win)) return { level: "ok", reasons: [] };

    if (rem != null) {
      if (rem <= CRIT_REM) {
        level = "critical";
        reasons.push(`Weekly 剩餘僅 ${rem.toFixed(1)}%`);
      } else if (rem <= LOW_REM) {
        level = "low";
        reasons.push(`Weekly 剩餘 ${rem.toFixed(1)}% 偏低`);
      }
    }

    const resetAt = parseIso(win.resets_at);
    const h = hoursUntil(resetAt);
    if (h != null && h > 0) {
      if (h <= RESET_CRIT_H) {
        level = "critical";
        reasons.push(`Weekly 重置倒數 ${(h * 60).toFixed(0)} 分鐘`);
      } else if (h <= RESET_SOON_H) {
        if (level === "ok") level = "low";
        reasons.push(`Weekly 即將重置（${h.toFixed(1)}h 內）`);
      }
    }
    return { level, reasons };
  }

  function statusBadge(status) {
    return `<span class="badge ${status}">${status}</span>`;
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function renderWindow(win) {
    const rem = remainingPct(win);
    const isBalance = win.key && String(win.key).startsWith("balance");
    const extra = win.raw_extra || {};
    const urg = urgencyLevel(win);
    const urgClass = urg.level === "ok" ? "" : `urgent-${urg.level}`;
    const windowClass = urg.level === "ok" ? "" : urgClass;

    if (isBalance || win.amount != null) {
      const granted = extra.granted_balance;
      const topped = extra.topped_up_balance;
      return `
        <div class="window ${windowClass}" data-reset="">
          ${urg.reasons.length ? `<div class="urgency-banner">${escapeHtml(urg.reasons.join(" · "))}</div>` : ""}
          <div class="window-title">
            <span class="label ${urgClass}">${escapeHtml(win.label || "Balance")}</span>
            <span class="pct ${urgClass}">${win.currency || ""}</span>
          </div>
          <div class="balance-line ${urgClass}">${escapeHtml(String(win.amount ?? "—"))}</div>
          <div class="balance-sub">
            Paid/Topped: ${escapeHtml(String(topped ?? "—"))}
            · Granted: ${escapeHtml(String(granted ?? "—"))}
          </div>
          <div class="countdown">重置：按量扣款，無固定週期</div>
        </div>`;
    }

    const width = rem == null ? 0 : Math.max(0, Math.min(100, rem));
    const lowBar = isWeeklyWindow(win) && rem != null && rem <= LOW_REM;
    const resetAt = parseIso(win.resets_at);
    return `
      <div class="window ${windowClass}" data-reset="${win.resets_at || ""}" data-urgency="${urg.level}">
        ${urg.reasons.length ? `<div class="urgency-banner">${escapeHtml(urg.reasons.join(" · "))}</div>` : ""}
        <div class="window-title">
          <span class="label ${urgClass}">${escapeHtml(win.label || win.key)}</span>
          <span class="pct ${urgClass}">${rem == null ? "—" : rem.toFixed(1) + "% 剩餘"}</span>
        </div>
        <div class="bar ${lowBar ? "low" : ""}"><span style="width:${width}%"></span></div>
        <div class="countdown ${urgClass}">
          重置倒數：<strong class="cd ${urgClass}">${fmtCountdown(resetAt)}</strong>
          ${resetAt ? `<span class="${urgClass}"> · ${fmtTime(resetAt)}</span>` : ""}
        </div>
      </div>`;
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
        const wins = (s.windows || []).filter((w) => {
          // Hide DeepSeek USD (and any non-CNY balance)
          if (w.key && String(w.key).startsWith("balance")) {
            const cur = String(w.currency || "").toUpperCase();
            if (cur && cur !== "CNY") return false;
            if (String(w.key).toLowerCase().includes("usd")) return false;
          }
          return true;
        });
        const windows = wins.map(renderWindow).join("") ||
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

  function fmtHours(h) {
    if (h == null || !Number.isFinite(h)) return "—";
    if (h >= 48) return `${(h / 24).toFixed(1)} 天`;
    if (h >= 1) return `${h.toFixed(1)} 小時`;
    return `${Math.max(0, h * 60).toFixed(0)} 分鐘`;
  }

  function renderEstimate(est) {
    if (!est || !est.ok) {
      return `<div class="estimate-box">${escapeHtml(est?.reason || "尚無足夠歷史，持續輪詢後會出現估算")}</div>`;
    }

    let main = "";
    let urgClass = "";
    if (est.kind === "balance") {
      main = `以目前消耗，餘額約還能撐 ${fmtHours(est.hours_until_empty)}`;
      if (est.hours_until_empty != null && est.hours_until_empty < 24) urgClass = "urgent-low";
      if (est.hours_until_empty != null && est.hours_until_empty < 6) urgClass = "urgent-critical";
    } else {
      if (est.idle) {
        main = `近 ${est.lookback_hours}h 幾乎無消耗（速率 ${est.burn_per_hour}%/h）`;
      } else {
        main = `以目前速率，約 ${fmtHours(est.hours_until_empty)} 後額度耗盡（${est.burn_per_hour}%/h）`;
      }
      if (est.hours_until_empty != null && est.hours_until_empty < 6) urgClass = "urgent-critical";
      else if (est.hours_until_empty != null && est.hours_until_empty < 24) urgClass = "urgent-low";
    }

    const tasks = est.tasks_remaining || {};
    const chips = [];
    if (tasks.light != null) chips.push(`<span>輕量 ≈ ${tasks.light} 次</span>`);
    if (tasks.medium != null) chips.push(`<span>中等 ≈ ${tasks.medium} 次</span>`);
    if (tasks.heavy != null) chips.push(`<span>重度 ≈ ${tasks.heavy} 次</span>`);

    return `
      <div class="estimate-box">
        <div class="estimate-main ${urgClass}">${escapeHtml(est.label || "")}：${escapeHtml(main)}</div>
        <div class="estimate-tasks">${chips.join("")}</div>
        <div>${escapeHtml(est.note || "")}</div>
      </div>`;
  }

  function destroyCharts() {
    Object.keys(chartInstances).forEach((k) => {
      try { chartInstances[k].destroy(); } catch (_) {}
      delete chartInstances[k];
    });
  }

  function renderTrends(data) {
    trendsData = data;
    const providers = data.providers || [];
    if (!providers.length) {
      trendsEl.innerHTML = `<p class="loading">尚無趨勢資料</p>`;
      return;
    }

    destroyCharts();
    trendsEl.innerHTML = providers
      .map((p) => {
        const series = (p.series || []).filter((s) => (s.points || []).length > 0);
        const chartsHtml = series.length
          ? series
              .map((s, i) => {
                const canvasId = `chart-${p.provider}-${i}`;
                return `
                  <div class="chart-wrap">
                    <canvas id="${canvasId}" data-provider="${p.provider}" data-idx="${i}"></canvas>
                  </div>
                  <div class="hint">${escapeHtml(s.label || s.window_key)}（剩餘量）</div>`;
              })
              .join("")
          : `<p class="loading">此 provider 尚無 7 日歷史點（需持續運行累積）</p>`;

        const estimatesHtml = (p.estimates || []).map(renderEstimate).join("");

        return `
          <article class="trend-card" data-provider="${escapeHtml(p.provider)}">
            <h3>${escapeHtml(p.display_name || p.provider)}</h3>
            ${chartsHtml}
            ${estimatesHtml}
          </article>`;
      })
      .join("");

    // Build charts after DOM ready
    if (typeof Chart === "undefined") {
      setTimeout(() => renderTrends(data), 200);
      return;
    }

    providers.forEach((p) => {
      (p.series || []).forEach((s, i) => {
        if (!(s.points || []).length) return;
        const canvasId = `chart-${p.provider}-${i}`;
        const el = document.getElementById(canvasId);
        if (!el) return;
        const labels = s.points.map((pt) => {
          const d = parseIso(pt.t);
          return d ? d.toLocaleString(undefined, { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : pt.t;
        });
        const values = s.points.map((pt) => {
          if (s.kind === "balance") return pt.amount ?? pt.remaining;
          return pt.remaining ?? (pt.used != null ? 100 - pt.used : null);
        });
        chartInstances[canvasId] = new Chart(el, {
          type: "line",
          data: {
            labels,
            datasets: [
              {
                label: s.label || "remaining",
                data: values,
                borderColor: "#66b3ff",
                backgroundColor: "rgba(102, 179, 255, 0.12)",
                fill: true,
                tension: 0.25,
                pointRadius: 0,
                borderWidth: 2,
              },
            ],
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { display: false },
              tooltip: {
                callbacks: {
                  label: (ctx) => {
                    const v = ctx.parsed.y;
                    if (v == null) return "";
                    return s.kind === "balance" ? `餘額 ${v}` : `剩餘 ${v.toFixed(1)}%`;
                  },
                },
              },
            },
            scales: {
              x: {
                ticks: { maxTicksLimit: 6, color: "#8b9bb4", font: { size: 10 } },
                grid: { color: "rgba(255,255,255,0.04)" },
              },
              y: {
                min: s.kind === "percent" ? 0 : undefined,
                max: s.kind === "percent" ? 100 : undefined,
                ticks: { color: "#8b9bb4", font: { size: 10 } },
                grid: { color: "rgba(255,255,255,0.06)" },
              },
            },
          },
        });
      });
    });
  }

  function tickCountdowns() {
    if (!latest) return;
    document.querySelectorAll(".window[data-reset]").forEach((el) => {
      const raw = el.getAttribute("data-reset");
      if (!raw) return;
      const cd = el.querySelector(".cd");
      if (cd) cd.textContent = fmtCountdown(parseIso(raw));
    });
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

  async function loadTrends() {
    try {
      const res = await fetch("/api/trends?days=7", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      renderTrends(data);
    } catch (err) {
      trendsEl.innerHTML = `<p class="loading">趨勢載入失敗：${escapeHtml(err.message)}</p>`;
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
      await Promise.all([loadUsage(), loadTrends()]);
    } catch (err) {
      refreshMsg.textContent = `刷新失敗：${err.message}`;
    } finally {
      refreshBtn.disabled = false;
    }
  }

  refreshBtn.addEventListener("click", doRefresh);
  loadUsage();
  loadTrends();
  setInterval(loadUsage, 20000);
  setInterval(loadTrends, 60000);
  setInterval(tickCountdowns, 1000);
})();
