from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.models import AccountSnapshot, utcnow

# Heuristic "task" sizes as % of a rate-limit window (for estimates only).
TASK_COST_PERCENT = {
    "light": 2.0,   # small edit / short Q&A
    "medium": 5.0,  # normal coding turn
    "heavy": 15.0,  # long agent / large context
}

# DeepSeek: rough average cost per "medium" chat (USD equivalent scale free)
# Used only for wording when balance is currency; prefer CNY if primary.
BALANCE_PER_MEDIUM_TASK = {
    "USD": 0.02,
    "CNY": 0.15,
}

LOW_REMAINING_PCT = 20.0
CRITICAL_REMAINING_PCT = 10.0
RESET_SOON_HOURS = 2.0
RESET_CRITICAL_HOURS = 0.5
# DeepSeek: no period reset; only warn when CNY balance is low
DEEPSEEK_CNY_WARN = 10.0


def _parse_ts(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _window_metric(window: dict[str, Any]) -> tuple[str, float | None, float | None]:
    """
    Return (metric_kind, remaining_value, used_percent).
    metric_kind: 'percent' | 'balance'
    """
    key = str(window.get("key") or "")
    rem = window.get("remaining_percent")
    used = window.get("used_percent")

    # If explicit percentage fields exist, prefer percent metric
    if rem is not None or used is not None:
        try:
            rem_f = float(rem) if rem is not None else None
        except (TypeError, ValueError):
            rem_f = None
        try:
            used_f = float(used) if used is not None else None
        except (TypeError, ValueError):
            used_f = None
        if rem_f is None and used_f is not None:
            rem_f = max(0.0, min(100.0, 100.0 - used_f))
        if used_f is None and rem_f is not None:
            used_f = max(0.0, min(100.0, 100.0 - rem_f))
        return "percent", rem_f, used_f

    if key.startswith("balance") or window.get("amount") is not None:
        try:
            amount = float(window.get("amount") or 0)
        except (TypeError, ValueError):
            amount = None
        return "balance", amount, None

    return "percent", None, None


def extract_series_points(
    history_rows: list[dict[str, Any]],
    *,
    window_key: str | None = None,
) -> list[dict[str, Any]]:
    """
    Build chronological points from history rows.
    Each point: {t, remaining, used, amount, window_key, label}
    Prefers primary windows (5h, weekly, balance) unless window_key set.
    """
    points: list[dict[str, Any]] = []
    for row in history_rows:
        snap = row.get("snapshot") or {}
        fetched = _parse_ts(row.get("fetched_at") or snap.get("fetched_at"))
        if not fetched:
            continue
        windows = snap.get("windows") or []
        if not isinstance(windows, list):
            continue
        chosen = None
        if window_key:
            for w in windows:
                if isinstance(w, dict) and w.get("key") == window_key:
                    chosen = w
                    break
        else:
            # Prefer main meters: 5h, 1w/weekly, first balance
            for prefer in ("5h", "1w", "weekly"):
                for w in windows:
                    if isinstance(w, dict) and w.get("key") == prefer:
                        chosen = w
                        break
                if chosen:
                    break
            if not chosen:
                for w in windows:
                    if isinstance(w, dict) and str(w.get("key") or "").startswith(
                        "balance"
                    ):
                        chosen = w
                        break
            if not chosen:
                for w in windows:
                    if isinstance(w, dict) and not str(w.get("key") or "").startswith(
                        "product-"
                    ):
                        chosen = w
                        break
        if not chosen:
            continue
        kind, rem, used = _window_metric(chosen)
        points.append(
            {
                "t": fetched.isoformat(),
                "ts": fetched.timestamp(),
                "kind": kind,
                "remaining": rem,
                "used": used,
                "amount": rem if kind == "balance" else None,
                "window_key": chosen.get("key"),
                "label": chosen.get("label"),
                "currency": chosen.get("currency"),
            }
        )
    points.sort(key=lambda p: p["ts"])
    return points


def downsample_series(
    points: list[dict[str, Any]], max_points: int = 168
) -> list[dict[str, Any]]:
    """Downsample to ~max_points (default: hourly for 7d)."""
    if len(points) <= max_points:
        return points
    # Bucket by equal index stride
    stride = max(1, len(points) // max_points)
    out = points[::stride]
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def compute_burn_estimate(
    points: list[dict[str, Any]],
    *,
    lookback_hours: float = 24.0,
) -> dict[str, Any]:
    """
    Estimate burn rate from recent points and remaining work capacity.
    """
    now = utcnow()
    if len(points) < 2:
        return {
            "ok": False,
            "reason": "歷史樣本不足（需至少 2 筆）",
            "burn_per_hour": None,
            "hours_until_empty": None,
            "tasks_remaining": None,
        }

    cutoff = now.timestamp() - lookback_hours * 3600
    recent = [p for p in points if p["ts"] >= cutoff]
    if len(recent) < 2:
        recent = points[-min(len(points), 30) :]

    first, last = recent[0], recent[-1]
    elapsed_h = max((last["ts"] - first["ts"]) / 3600.0, 1e-6)
    kind = last.get("kind") or first.get("kind") or "percent"

    if kind == "balance":
        a0 = first.get("amount")
        a1 = last.get("amount")
        if a0 is None or a1 is None:
            return {"ok": False, "reason": "餘額資料不完整", "burn_per_hour": None}
        # Balance decreases when used
        burned = float(a0) - float(a1)
        burn_per_hour = burned / elapsed_h
        remaining = float(a1)
        hours_left = (remaining / burn_per_hour) if burn_per_hour > 1e-9 else None
        currency = last.get("currency") or "USD"
        unit_cost = BALANCE_PER_MEDIUM_TASK.get(currency, BALANCE_PER_MEDIUM_TASK["USD"])
        medium_left = int(remaining / unit_cost) if unit_cost > 0 else None
        return {
            "ok": True,
            "kind": "balance",
            "currency": currency,
            "burn_per_hour": round(burn_per_hour, 4),
            "burn_per_day": round(burn_per_hour * 24, 4),
            "remaining": remaining,
            "hours_until_empty": round(hours_left, 1) if hours_left is not None else None,
            "days_until_empty": round(hours_left / 24, 2) if hours_left else None,
            "tasks_remaining": {
                "light": int(remaining / (unit_cost * 0.4)) if unit_cost else None,
                "medium": medium_left,
                "heavy": int(remaining / (unit_cost * 3)) if unit_cost else None,
            },
            "lookback_hours": round(elapsed_h, 2),
            "note": f"以近 {elapsed_h:.1f}h 餘額變化估算；medium ≈ {unit_cost} {currency}/次",
            "window_key": last.get("window_key"),
            "label": last.get("label"),
        }

    # percent windows: use used% increase
    u0 = first.get("used")
    u1 = last.get("used")
    r1 = last.get("remaining")
    if u0 is None or u1 is None:
        return {"ok": False, "reason": "百分比資料不完整", "burn_per_hour": None}
    # Handle reset mid-window (used drops): only count positive burn segments
    burned = float(u1) - float(u0)
    if burned < -1:
        # likely reset; use last half of series
        mid = recent[len(recent) // 2 :]
        if len(mid) >= 2:
            u0 = mid[0].get("used") or 0
            u1 = mid[-1].get("used") or 0
            burned = max(0.0, float(u1) - float(u0))
            elapsed_h = max((mid[-1]["ts"] - mid[0]["ts"]) / 3600.0, 1e-6)
        else:
            burned = 0.0
    burned = max(0.0, burned)
    burn_per_hour = burned / elapsed_h
    remaining = float(r1) if r1 is not None else max(0.0, 100.0 - float(u1))
    hours_left = (remaining / burn_per_hour) if burn_per_hour > 1e-6 else None

    tasks = {}
    for name, cost in TASK_COST_PERCENT.items():
        tasks[name] = int(remaining / cost) if cost > 0 else None

    return {
        "ok": True,
        "kind": "percent",
        "burn_per_hour": round(burn_per_hour, 3),
        "burn_per_day": round(burn_per_hour * 24, 2),
        "remaining_percent": round(remaining, 2),
        "hours_until_empty": round(hours_left, 1) if hours_left is not None else None,
        "days_until_empty": round(hours_left / 24, 2) if hours_left else None,
        "tasks_remaining": tasks,
        "task_cost_percent": TASK_COST_PERCENT,
        "lookback_hours": round(elapsed_h, 2),
        "note": (
            f"以近 {elapsed_h:.1f}h 消耗速率估算；"
            f"light/medium/heavy 假設各耗 {TASK_COST_PERCENT['light']}/"
            f"{TASK_COST_PERCENT['medium']}/{TASK_COST_PERCENT['heavy']}% 額度"
        ),
        "window_key": last.get("window_key"),
        "label": last.get("label"),
        "idle": burn_per_hour < 0.05,
    }


def _is_weekly_window(window: dict[str, Any]) -> bool:
    key = str(window.get("key") or "").lower()
    label = str(window.get("label") or "").lower()
    if (
        key in {"1w", "weekly"}
        or key.startswith("1w")
        or key.endswith("-1w")
        or key.endswith("-weekly")
        or "weekly" in key
    ):
        return True
    if "week" in label or "週" in label:
        return True
    # SuperGrok usage pool labeled Weekly via reset span
    limit_s = window.get("limit_window_seconds")
    try:
        if limit_s is not None and abs(int(limit_s) - 604800) <= 600:
            return True
    except (TypeError, ValueError):
        pass
    return False


def urgency_for_window(window: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """
    UI urgency flags:
    - Rate-limit windows: only Weekly (not 5h / product breakdowns)
    - DeepSeek CNY balance: warn when < 10 CNY (no reset reminders)
    """
    now = now or utcnow()
    kind, rem, _used = _window_metric(window)
    level = "ok"  # ok | low | critical
    reasons: list[str] = []
    key = str(window.get("key") or "")

    # DeepSeek / balance: only CNY, threshold 10, never reset-based
    if kind == "balance" or key.startswith("balance"):
        currency = str(window.get("currency") or "").upper()
        if currency and currency != "CNY":
            return {
                "level": "ok",
                "reasons": [],
                "remaining": rem,
                "resets_at": None,
            }
        if rem is not None and rem < DEEPSEEK_CNY_WARN:
            level = "critical" if rem <= 5 else "low"
            reasons.append(f"CNY 餘額 {rem:.2f}（低於 {DEEPSEEK_CNY_WARN:g}）")
        return {
            "level": level,
            "reasons": reasons,
            "remaining": rem,
            "resets_at": None,
        }

    # Percent / subscription windows: Weekly only
    if not _is_weekly_window(window):
        return {
            "level": "ok",
            "reasons": [],
            "remaining": rem,
            "resets_at": window.get("resets_at"),
        }

    if rem is not None:
        if rem <= CRITICAL_REMAINING_PCT:
            level = "critical"
            reasons.append(f"Weekly 剩餘僅 {rem:.1f}%")
        elif rem <= LOW_REMAINING_PCT:
            level = "low"
            reasons.append(f"Weekly 剩餘 {rem:.1f}% 偏低")

    resets_at = _parse_ts(window.get("resets_at"))
    if resets_at:
        hours = (resets_at - now).total_seconds() / 3600.0
        if 0 < hours <= RESET_CRITICAL_HOURS:
            level = "critical"
            reasons.append(f"Weekly 重置倒數 {hours * 60:.0f} 分鐘")
        elif 0 < hours <= RESET_SOON_HOURS:
            if level == "ok":
                level = "low"
            reasons.append(f"Weekly 即將重置（{hours:.1f}h 內）")

    return {
        "level": level,
        "reasons": reasons,
        "remaining": rem,
        "resets_at": resets_at.isoformat() if resets_at else None,
    }


def build_trends_payload(
    history_by_provider: dict[str, list[dict[str, Any]]],
    latest_snapshots: list[AccountSnapshot],
    *,
    days: int = 7,
) -> dict[str, Any]:
    """Assemble chart series + estimates + urgency for dashboard."""
    providers_out: list[dict[str, Any]] = []
    for snap in latest_snapshots:
        pid = snap.provider.value
        rows = history_by_provider.get(pid, [])

        # Build multi-series for main windows present in latest snapshot
        series_list: list[dict[str, Any]] = []
        main_keys: list[str] = []
        for w in snap.windows:
            key = w.key
            if key.startswith("product-"):
                continue
            # Skip non-CNY balances (e.g. DeepSeek USD)
            if key.startswith("balance") and (w.currency or "").upper() not in ("", "CNY"):
                continue
            if "usd" in key.lower():
                continue
            main_keys.append(key)

        if not main_keys:
            main_keys = [None]  # type: ignore

        estimates: list[dict[str, Any]] = []
        seen = set()
        for key in main_keys:
            if key in seen:
                continue
            seen.add(key)
            pts = extract_series_points(rows, window_key=key if key else None)
            # If specific key empty, try auto
            if not pts and key:
                pts = extract_series_points(rows, window_key=None)
            chart_pts = downsample_series(pts, max_points=days * 24)
            series_list.append(
                {
                    "window_key": key or (chart_pts[-1]["window_key"] if chart_pts else "main"),
                    "label": (
                        next((w.label for w in snap.windows if w.key == key), None)
                        if key
                        else (chart_pts[-1].get("label") if chart_pts else "Usage")
                    ),
                    "kind": chart_pts[-1]["kind"] if chart_pts else "percent",
                    "points": [
                        {
                            "t": p["t"],
                            "remaining": p["remaining"],
                            "used": p["used"],
                            "amount": p.get("amount"),
                        }
                        for p in chart_pts
                    ],
                }
            )
            est = compute_burn_estimate(pts, lookback_hours=min(24.0, days * 24.0))
            estimates.append(est)

        urgencies = [
            {
                "window_key": w.key,
                "label": w.label,
                **urgency_for_window(w.model_dump(mode="json")),
            }
            for w in snap.windows
            if not w.key.startswith("product-")
        ]

        providers_out.append(
            {
                "provider": pid,
                "display_name": snap.display_name,
                "status": snap.status.value,
                "series": series_list,
                "estimates": estimates,
                "urgency": urgencies,
            }
        )

    return {
        "days": days,
        "server_time": utcnow().isoformat(),
        "providers": providers_out,
    }
