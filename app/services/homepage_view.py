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


def _pick_balance_cny(windows: Iterable[UsageWindow]) -> UsageWindow | None:
    for w in windows:
        if (w.currency or "").upper() == "CNY":
            return w
        if (w.key or "").lower() in {"balance-cny", "balance", "cny"}:
            return w
    return None


def _empty_provider(prefix: str, status: str = "error") -> dict[str, Any]:
    if prefix == "deepseek":
        return {
            "deepseek_cny": None,
            "deepseek_status": status,
        }
    return {
        f"{prefix}_weekly_used_percent": None,
        f"{prefix}_weekly_remaining_percent": None,
        f"{prefix}_resets_at": None,
        f"{prefix}_reset_display": None,
        f"{prefix}_status": status,
    }


def build_homepage_payload(
    snapshots: list[AccountSnapshot], now: datetime | None = None
) -> dict[str, Any]:
    current = now or utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    by_id = {s.provider: s for s in snapshots}
    out: dict[str, Any] = {}
    updated_candidates: list[datetime] = []

    # Codex weekly
    codex = by_id.get(ProviderId.CODEX)
    if not codex:
        out.update(_empty_provider("codex", "missing"))
    else:
        updated_candidates.append(codex.fetched_at)
        w = _pick_weekly(codex.windows)
        out["codex_status"] = codex.status.value
        out["codex_weekly_used_percent"] = w.used_percent if w else None
        out["codex_weekly_remaining_percent"] = w.remaining_percent if w else None
        out["codex_resets_at"] = _iso(w.resets_at) if w else None
        out["codex_reset_display"] = _countdown_display(w.resets_at, current) if w else None

    # SuperGrok weekly
    grok = by_id.get(ProviderId.SUPERGROK)
    if not grok:
        out.update(_empty_provider("grok", "missing"))
    else:
        updated_candidates.append(grok.fetched_at)
        w = _pick_weekly(grok.windows)
        out["grok_status"] = grok.status.value
        out["grok_weekly_used_percent"] = w.used_percent if w else None
        out["grok_weekly_remaining_percent"] = w.remaining_percent if w else None
        out["grok_resets_at"] = _iso(w.resets_at) if w else None
        out["grok_reset_display"] = _countdown_display(w.resets_at, current) if w else None

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

    out["updated_at"] = _iso(max(updated_candidates)) if updated_candidates else _iso(current)
    return out
