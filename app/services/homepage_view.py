"""Project nested usage snapshots into a flat JSON for gethomepage customapi."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from app.models import AccountSnapshot, ProviderId, UsageWindow, utcnow


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.isoformat() + "Z"
    return dt.isoformat().replace("+00:00", "Z")


def _countdown_display(dt: datetime | None, now: datetime) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    seconds = int((dt - now).total_seconds())
    if seconds <= 0:
        return "已到期"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = (remainder + 59) // 60
    if days:
        return f"{days}天" if not hours else f"{days}天 {hours}小時"
    if hours:
        return f"{hours}小時 {minutes}分"
    return f"{max(1, minutes)}分"


def _pick_weekly(windows: Iterable[UsageWindow]) -> UsageWindow | None:
    wins = list(windows)
    for w in wins:
        key = (w.key or "").lower()
        label = (w.label or "").lower()
        if key in {"1w", "weekly", "week"} or "week" in label:
            return w
        if w.limit_window_seconds is not None and 500_000 <= w.limit_window_seconds <= 700_000:
            return w
    return None


def _is_5h_meter(window: UsageWindow) -> bool:
    key = (window.key or "").lower()
    label = (window.label or "").lower()
    if key in {"5h", "5-hour", "5hour", "five_hour"} or key.endswith("-5h"):
        return True
    if "5h" in key or "5h" in label or "5-hour" in label or "5小時" in label or "5 小時" in label or "5小時" in (window.label or ""):
        return True
    if window.limit_window_seconds is not None and 10_000 <= window.limit_window_seconds <= 25_000:
        return True
    return False


def _pick_5h(windows: Iterable[UsageWindow]) -> UsageWindow | None:
    wins = list(windows)
    for w in wins:
        if _is_5h_meter(w):
            return w
    return None


def _pick_balance_cny(windows: Iterable[UsageWindow]) -> UsageWindow | None:
    for w in windows:
        if (w.currency or "").upper() == "CNY":
            return w
        if (w.key or "").lower() in {"balance-cny", "balance", "cny"}:
            return w
    return None


def _agy_family(window: UsageWindow) -> str:
    extra = window.raw_extra or {}
    family = extra.get("family")
    if family:
        return str(family)
    key = (window.key or "").lower()
    group = str(extra.get("group") or extra.get("bucket_id") or "").lower()
    if key.startswith("3p-") or "claude" in group or "gpt" in group or key.startswith("3p"):
        return "3p"
    if key.startswith("gemini") or "gemini" in group or key in {"1w", "5h", "weekly"}:
        return "gemini"
    if "-" in key:
        return key.split("-", 1)[0]
    return "other"


def _is_weekly_meter(window: UsageWindow) -> bool:
    key = (window.key or "").lower()
    label = (window.label or "").lower()
    if key in {"1w", "weekly", "week"} or key.endswith("-1w") or key.endswith("-weekly"):
        return True
    if "week" in key or "week" in label or "週" in (window.label or ""):
        return True
    if window.limit_window_seconds is not None and 500_000 <= window.limit_window_seconds <= 700_000:
        return True
    return False


def _pick_agy_weekly(windows: Iterable[UsageWindow], family: str) -> UsageWindow | None:
    for w in windows:
        if _agy_family(w) != family:
            continue
        if _is_weekly_meter(w):
            return w
    return None


def _pick_agy_5h(windows: Iterable[UsageWindow], family: str) -> UsageWindow | None:
    for w in windows:
        if _agy_family(w) != family:
            continue
        if _is_5h_meter(w):
            return w
    return None


def _pick_antigravity(windows: Iterable[UsageWindow]) -> UsageWindow | None:
    """Fallback primary meter: Gemini weekly, else any weekly, else first % window."""
    wins = list(windows)
    gemini = _pick_agy_weekly(wins, "gemini")
    if gemini:
        return gemini
    for w in wins:
        if _is_weekly_meter(w):
            return w
    for w in wins:
        key = (w.key or "").lower()
        if key in {"monthly", "month", "prompt-credit", "prompt_credit", "promptcredits"}:
            return w
    for w in wins:
        if w.remaining_percent is not None:
            return w
    return wins[0] if wins else None


def _empty_provider(prefix: str, status: str = "error") -> dict[str, Any]:
    if prefix == "deepseek":
        return {
            "deepseek_cny": None,
            "deepseek_status": status,
        }
    out = {
        f"{prefix}_5h_used_percent": None,
        f"{prefix}_5h_remaining_percent": None,
        f"{prefix}_5h_resets_at": None,
        f"{prefix}_5h_reset_display": None,
        f"{prefix}_weekly_used_percent": None,
        f"{prefix}_weekly_remaining_percent": None,
        f"{prefix}_resets_at": None,
        f"{prefix}_reset_display": None,
        f"{prefix}_status": status,
    }
    if prefix == "antigravity":
        out["antigravity_3p_5h_used_percent"] = None
        out["antigravity_3p_5h_remaining_percent"] = None
        out["antigravity_3p_5h_resets_at"] = None
        out["antigravity_3p_5h_reset_display"] = None
        out["antigravity_other_weekly_used_percent"] = None
        out["antigravity_other_weekly_remaining_percent"] = None
    return out


def build_homepage_payload(
    snapshots: list[AccountSnapshot], now: datetime | None = None
) -> dict[str, Any]:
    current = now or utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    by_id = {s.provider: s for s in snapshots}
    out: dict[str, Any] = {}
    updated_candidates: list[datetime] = []

    # Codex 5h & weekly
    codex = by_id.get(ProviderId.CODEX)
    if not codex:
        out.update(_empty_provider("codex", "missing"))
    else:
        updated_candidates.append(codex.fetched_at)
        w_5h = _pick_5h(codex.windows)
        w_1w = _pick_weekly(codex.windows)
        out["codex_status"] = codex.status.value
        out["codex_5h_used_percent"] = w_5h.used_percent if w_5h else None
        out["codex_5h_remaining_percent"] = w_5h.remaining_percent if w_5h else None
        out["codex_5h_resets_at"] = _iso(w_5h.resets_at) if w_5h else None
        out["codex_5h_reset_display"] = _countdown_display(w_5h.resets_at, current) if w_5h else None
        out["codex_weekly_used_percent"] = w_1w.used_percent if w_1w else None
        out["codex_weekly_remaining_percent"] = w_1w.remaining_percent if w_1w else None
        out["codex_resets_at"] = _iso(w_1w.resets_at) if w_1w else None
        out["codex_reset_display"] = _countdown_display(w_1w.resets_at, current) if w_1w else None

    # SuperGrok 5h & weekly
    grok = by_id.get(ProviderId.SUPERGROK)
    if not grok:
        out.update(_empty_provider("grok", "missing"))
    else:
        updated_candidates.append(grok.fetched_at)
        w_5h = _pick_5h(grok.windows)
        w_1w = _pick_weekly(grok.windows)
        out["grok_status"] = grok.status.value
        out["grok_5h_used_percent"] = w_5h.used_percent if w_5h else None
        out["grok_5h_remaining_percent"] = w_5h.remaining_percent if w_5h else None
        out["grok_5h_resets_at"] = _iso(w_5h.resets_at) if w_5h else None
        out["grok_5h_reset_display"] = _countdown_display(w_5h.resets_at, current) if w_5h else None
        out["grok_weekly_used_percent"] = w_1w.used_percent if w_1w else None
        out["grok_weekly_remaining_percent"] = w_1w.remaining_percent if w_1w else None
        out["grok_resets_at"] = _iso(w_1w.resets_at) if w_1w else None
        out["grok_reset_display"] = _countdown_display(w_1w.resets_at, current) if w_1w else None

    # DeepSeek CNY
    ds = by_id.get(ProviderId.DEEPSEEK)
    if not ds:
        out.update(_empty_provider("deepseek", "missing"))
    else:
        updated_candidates.append(ds.fetched_at)
        w = _pick_balance_cny(ds.windows)
        out["deepseek_status"] = ds.status.value
        amount = None
        if w and w.amount is not None:
            try:
                amount = float(w.amount)
            except (TypeError, ValueError):
                amount = None
        out["deepseek_cny"] = amount

    # Antigravity: Gemini 5h/weekly + Claude/GPT 5h/weekly
    agy = by_id.get(ProviderId.ANTIGRAVITY)
    if not agy:
        out.update(_empty_provider("antigravity", "missing"))
    else:
        updated_candidates.append(agy.fetched_at)
        out["antigravity_status"] = agy.status.value
        gemini_5h = _pick_agy_5h(agy.windows, "gemini")
        gemini_1w = _pick_agy_weekly(agy.windows, "gemini") or _pick_antigravity(agy.windows)
        other_5h = _pick_agy_5h(agy.windows, "3p") or _pick_agy_5h(agy.windows, "other")
        other_1w = _pick_agy_weekly(agy.windows, "3p") or _pick_agy_weekly(agy.windows, "other")

        out["antigravity_5h_used_percent"] = gemini_5h.used_percent if gemini_5h else None
        out["antigravity_5h_remaining_percent"] = gemini_5h.remaining_percent if gemini_5h else None
        out["antigravity_5h_resets_at"] = _iso(gemini_5h.resets_at) if gemini_5h else None
        out["antigravity_5h_reset_display"] = _countdown_display(gemini_5h.resets_at, current) if gemini_5h else None

        out["antigravity_3p_5h_used_percent"] = other_5h.used_percent if other_5h else None
        out["antigravity_3p_5h_remaining_percent"] = other_5h.remaining_percent if other_5h else None
        out["antigravity_3p_5h_resets_at"] = _iso(other_5h.resets_at) if other_5h else None
        out["antigravity_3p_5h_reset_display"] = _countdown_display(other_5h.resets_at, current) if other_5h else None

        out["antigravity_weekly_used_percent"] = gemini_1w.used_percent if gemini_1w else None
        out["antigravity_weekly_remaining_percent"] = gemini_1w.remaining_percent if gemini_1w else None
        out["antigravity_other_weekly_used_percent"] = other_1w.used_percent if other_1w else None
        out["antigravity_other_weekly_remaining_percent"] = other_1w.remaining_percent if other_1w else None
        reset = gemini_1w.resets_at if gemini_1w else None
        out["antigravity_resets_at"] = _iso(reset) if reset else None
        out["antigravity_reset_display"] = _countdown_display(reset, current) if reset else None

    # Claude 5h & weekly (matches Codex & SuperGrok schema)
    claude = by_id.get(ProviderId.CLAUDE)
    if not claude:
        out.update(_empty_provider("claude", "missing"))
    else:
        updated_candidates.append(claude.fetched_at)
        w_5h = _pick_5h(claude.windows)
        w_1w = _pick_weekly(claude.windows) or (claude.windows[0] if claude.windows else None)
        out["claude_status"] = claude.status.value
        out["claude_5h_used_percent"] = w_5h.used_percent if w_5h else None
        out["claude_5h_remaining_percent"] = w_5h.remaining_percent if w_5h else None
        out["claude_5h_resets_at"] = _iso(w_5h.resets_at) if w_5h else None
        out["claude_5h_reset_display"] = _countdown_display(w_5h.resets_at, current) if w_5h else None
        out["claude_weekly_used_percent"] = w_1w.used_percent if w_1w else None
        out["claude_weekly_remaining_percent"] = w_1w.remaining_percent if w_1w else None
        reset = (w_1w.resets_at if w_1w else None) or (w_5h.resets_at if w_5h else None)
        out["claude_resets_at"] = _iso(reset) if reset else None
        out["claude_reset_display"] = _countdown_display(reset, current) if reset else None

    out["updated_at"] = _iso(max(updated_candidates)) if updated_candidates else _iso(current)
    return out
