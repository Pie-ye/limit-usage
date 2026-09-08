"""Quota-aware model routing view for the Trellis / orchestrating-development dispatcher.

Projects the provider snapshots plus recent history into one JSON document that
answers, per vendor CLI pool and per model id:

* how much of each rate-limit window is left (``remaining_percent``)
* how long until that window resets (``seconds_until_reset``)
* how fast it is being consumed (``burn_per_hour``, % of window per hour)
* whether the current pace exhausts the window before it resets
  (``will_last_until_reset``, ``projected_used_at_reset``)
* a single ``score`` (0–100) and ``level`` (ok/low/critical/unknown) per pool,
  and a ``recommended`` model per dispatch tier.

Scoring rule (deliberately simple so a shell caller can reason about it):

1. ``binding`` window = the window with the lowest remaining % among the
   windows the model actually draws on.
2. ``score`` starts at the binding window's remaining %.
3. If the pace of the last few hours would empty the binding window before it
   resets, the score is halved (``PACE_PENALTY``).
4. If the pool's data is older than ``stale_after_seconds`` the score is left
   as-is but ``stale`` is set; callers decide how much to trust it.
5. ``level``: critical ≤ 10 %, low ≤ 20 %, unknown when no window data; the
   thresholds are shared with ``app.services.analytics``.

Tier recommendation: the routing ladder's primary model unless its pool is
``critical`` / not ``ok``; then the highest-scoring candidate in the tier's
fallback list. ``candidates`` is always returned in score order so the caller
can apply its own policy instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from app.models import AccountSnapshot, ProviderId, UsageWindow, utcnow
from app.services.analytics import (
    CRITICAL_REMAINING_PCT,
    LOW_REMAINING_PCT,
    compute_burn_estimate,
    extract_series_points,
)

# Data older than this is flagged ``stale`` (matches the 15-minute wording the
# Claude card uses for "官方額度資料為 N 分鐘前").
DEFAULT_STALE_AFTER_SECONDS = 900
# Multiplier applied to ``score`` when the recent pace exhausts the binding
# window before it resets.
PACE_PENALTY = 0.5
# Burn-rate lookback per window slot (hours). A 5-hour window is estimated from
# the last 2 hours so a mid-window reset does not swamp the sample; weekly
# windows use a full day.
LOOKBACK_HOURS = {"5h": 2.0, "1w": 24.0, "1w-fable": 24.0}
# Below this %/hour the pool is considered idle (mirrors analytics ``idle``).
IDLE_BURN_PER_HOUR = 0.05

# Vendor CLI pool → (provider, {slot: window matcher}).
# Slots are normalised names so callers never see provider-specific keys.
POOLS: dict[str, dict[str, Any]] = {
    "claude": {
        "provider": ProviderId.CLAUDE,
        "display_name": "Claude Code (claude)",
        "slots": {"5h": "5h", "1w": "1w", "1w-fable": "1w-fable"},
    },
    "codex": {
        "provider": ProviderId.CODEX,
        "display_name": "Codex (codex)",
        "slots": {"5h": "5h", "1w": "1w"},
    },
    "grok": {
        "provider": ProviderId.SUPERGROK,
        "display_name": "SuperGrok (grok)",
        "slots": {"1w": "weekly"},
    },
    "agy": {
        "provider": ProviderId.ANTIGRAVITY,
        "display_name": "Antigravity · Gemini (agy)",
        "slots": {"5h": "5h", "1w": "1w"},
    },
    "agy-3p": {
        "provider": ProviderId.ANTIGRAVITY,
        "display_name": "Antigravity · Claude/GPT (agy)",
        "slots": {"5h": "3p-5h", "1w": "3p-1w"},
    },
}

# Model id → pool and which slots it draws on. Fable has its own weekly cap on
# top of the shared 5h / weekly windows; every other Claude model ignores it.
MODELS: dict[str, dict[str, Any]] = {
    "claude-sonnet-5": {"pool": "claude", "slots": ["5h", "1w"]},
    "claude-opus-5": {"pool": "claude", "slots": ["5h", "1w"]},
    "claude-fable-5-1": {"pool": "claude", "slots": ["5h", "1w", "1w-fable"]},
    "gpt-5.6-luna": {"pool": "codex", "slots": ["5h", "1w"]},
    "gpt-5.6-terra": {"pool": "codex", "slots": ["5h", "1w"]},
    "gpt-5.6-sol": {"pool": "codex", "slots": ["5h", "1w"]},
    "grok-4.6": {"pool": "grok", "slots": ["1w"]},
    "gemini-3.8-flash-high": {"pool": "agy", "slots": ["5h", "1w"]},
    "gemini-3.8-pro": {"pool": "agy", "slots": ["5h", "1w"]},
}

# Dispatch ladder from orchestrating-development/references/routing.md
# (2026-09-08). ``primary`` is what ``dispatch --tier`` picks today; the rest of
# ``candidates`` is the quota fallback order, one entry per vendor so a
# fallback also changes eyes.
TIERS: dict[str, dict[str, Any]] = {
    "T0": {
        "role": "implement",
        "primary": "gemini-3.8-flash-high",
        "candidates": ["gemini-3.8-flash-high", "grok-4.6", "claude-sonnet-5"],
    },
    "T1": {
        "role": "implement",
        "primary": "grok-4.6",
        "candidates": ["grok-4.6", "gemini-3.8-flash-high", "claude-sonnet-5"],
    },
    "T2": {
        "role": "implement",
        "primary": "claude-sonnet-5",
        "candidates": ["claude-sonnet-5", "gpt-5.6-terra", "grok-4.6"],
    },
    "T3": {
        "role": "implement",
        "primary": "gpt-5.6-luna",
        "candidates": ["gpt-5.6-luna", "claude-opus-5"],
    },
    "review": {
        "role": "review",
        "primary": "claude-opus-5",
        "candidates": ["claude-opus-5", "gpt-5.6-terra"],
    },
}


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _find_window(windows: Iterable[UsageWindow], key: str) -> UsageWindow | None:
    for w in windows:
        if (w.key or "") == key:
            return w
    if key == "1w-fable":
        for w in windows:
            if "fable" in (w.key or "").lower():
                return w
    return None


def _burn(rows: list[dict[str, Any]], key: str, slot: str) -> dict[str, Any]:
    if not rows:
        return {"ok": False}
    points = extract_series_points(rows, window_key=key)
    if len(points) < 2:
        return {"ok": False}
    return compute_burn_estimate(points, lookback_hours=LOOKBACK_HOURS.get(slot, 24.0))


def _window_view(
    window: UsageWindow | None,
    *,
    slot: str,
    key: str,
    rows: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    if window is None:
        return None
    remaining = window.remaining_percent
    used = window.used_percent
    if remaining is None and used is not None:
        remaining = max(0.0, min(100.0, 100.0 - used))
    if used is None and remaining is not None:
        used = max(0.0, min(100.0, 100.0 - remaining))

    seconds_until_reset: int | None = None
    if window.resets_at is not None:
        seconds_until_reset = max(0, int((_as_utc(window.resets_at) - now).total_seconds()))

    est = _burn(rows, key, slot)
    burn_per_hour = est.get("burn_per_hour") if est.get("ok") else None
    hours_until_empty = est.get("hours_until_empty") if est.get("ok") else None

    will_last: bool | None = None
    projected_used: float | None = None
    if remaining is not None and burn_per_hour is not None and seconds_until_reset is not None:
        hours_to_reset = seconds_until_reset / 3600.0
        projected_used = min(100.0, round((used or 0.0) + burn_per_hour * hours_to_reset, 2))
        will_last = burn_per_hour * hours_to_reset <= remaining

    return {
        "key": window.key,
        "label": window.label,
        "remaining_percent": remaining,
        "used_percent": used,
        "resets_at": _iso(window.resets_at),
        "seconds_until_reset": seconds_until_reset,
        "limit_window_seconds": window.limit_window_seconds,
        "burn_per_hour": burn_per_hour,
        "hours_until_empty": hours_until_empty,
        "idle": (burn_per_hour is not None and burn_per_hour < IDLE_BURN_PER_HOUR)
        if burn_per_hour is not None
        else None,
        "will_last_until_reset": will_last,
        "projected_used_at_reset": projected_used,
        "burn_samples_hours": est.get("lookback_hours") if est.get("ok") else None,
    }


def _level(remaining: float | None) -> str:
    if remaining is None:
        return "unknown"
    if remaining <= CRITICAL_REMAINING_PCT:
        return "critical"
    if remaining <= LOW_REMAINING_PCT:
        return "low"
    return "ok"


def _score(windows: dict[str, dict[str, Any] | None], slots: list[str]) -> dict[str, Any]:
    """Binding window + score for the given slots. Missing slots are ignored."""
    binding: dict[str, Any] | None = None
    binding_slot: str | None = None
    for slot in slots:
        w = windows.get(slot)
        if not w or w.get("remaining_percent") is None:
            continue
        if binding is None or w["remaining_percent"] < binding["remaining_percent"]:
            binding, binding_slot = w, slot
    if binding is None:
        return {"binding_slot": None, "score": None, "level": "unknown", "pace_penalty": False}
    score = float(binding["remaining_percent"])
    penalised = binding.get("will_last_until_reset") is False
    if penalised:
        score *= PACE_PENALTY
    return {
        "binding_slot": binding_slot,
        "score": round(score, 1),
        "level": _level(binding["remaining_percent"]),
        "pace_penalty": penalised,
    }


def build_routing_payload(
    snapshots: list[AccountSnapshot],
    history_by_provider: dict[str, list[dict[str, Any]]] | None = None,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    current = _as_utc(now or utcnow())
    history_by_provider = history_by_provider or {}
    by_id = {s.provider: s for s in snapshots}

    pools_out: dict[str, Any] = {}
    for pool_id, spec in POOLS.items():
        snap = by_id.get(spec["provider"])
        if snap is None:
            pools_out[pool_id] = {
                "provider": spec["provider"].value,
                "display_name": spec["display_name"],
                "status": "missing",
                "usable": False,
                "stale": True,
                "data_age_seconds": None,
                "fetched_at": None,
                "windows": {},
                "binding_slot": None,
                "score": None,
                "level": "unknown",
                "pace_penalty": False,
                "message": "no snapshot",
            }
            continue
        rows = history_by_provider.get(spec["provider"].value, [])
        age = max(0, int((current - _as_utc(snap.fetched_at)).total_seconds()))
        windows: dict[str, dict[str, Any] | None] = {}
        for slot, key in spec["slots"].items():
            windows[slot] = _window_view(
                _find_window(snap.windows, key), slot=slot, key=key, rows=rows, now=current
            )
        scored = _score(windows, list(spec["slots"].keys()))
        usable = snap.status.value == "ok" and scored["level"] not in {"critical", "unknown"}
        pools_out[pool_id] = {
            "provider": spec["provider"].value,
            "display_name": spec["display_name"],
            "status": snap.status.value,
            "usable": usable,
            "stale": age > stale_after_seconds,
            "data_age_seconds": age,
            "fetched_at": _iso(snap.fetched_at),
            "source": snap.source,
            "windows": windows,
            "message": snap.message,
            **scored,
        }

    models_out: dict[str, Any] = {}
    for model_id, spec in MODELS.items():
        pool = pools_out.get(spec["pool"])
        if not pool:
            continue
        scored = _score(pool["windows"], spec["slots"])
        binding = pool["windows"].get(scored["binding_slot"]) if scored["binding_slot"] else None
        models_out[model_id] = {
            "vendor": spec["pool"].split("-", 1)[0],
            "pool": spec["pool"],
            "slots": spec["slots"],
            "status": pool["status"],
            "stale": pool["stale"],
            "usable": pool["status"] == "ok" and scored["level"] not in {"critical", "unknown"},
            "remaining_percent": binding["remaining_percent"] if binding else None,
            "seconds_until_reset": binding["seconds_until_reset"] if binding else None,
            "resets_at": binding["resets_at"] if binding else None,
            "burn_per_hour": binding["burn_per_hour"] if binding else None,
            "will_last_until_reset": binding["will_last_until_reset"] if binding else None,
            **scored,
        }

    tiers_out: dict[str, Any] = {}
    for tier_id, spec in TIERS.items():
        ranked = sorted(
            (m for m in spec["candidates"] if m in models_out),
            key=lambda m: (
                models_out[m]["usable"],
                models_out[m]["score"] if models_out[m]["score"] is not None else -1.0,
            ),
            reverse=True,
        )
        primary = spec["primary"]
        primary_ok = primary in models_out and models_out[primary]["usable"]
        recommended = primary if primary_ok else (ranked[0] if ranked else primary)
        rec = models_out.get(recommended, {})
        tiers_out[tier_id] = {
            "role": spec["role"],
            "primary": primary,
            "recommended": recommended,
            "vendor": rec.get("vendor"),
            "fallback_used": recommended != primary,
            "reason": (
                "primary usable"
                if primary_ok
                else f"primary {primary} is {models_out.get(primary, {}).get('level', 'missing')}"
                + (
                    f" (status={models_out[primary]['status']})"
                    if primary in models_out and models_out[primary]["status"] != "ok"
                    else ""
                )
            ),
            "candidates": [
                {
                    "model": m,
                    "vendor": models_out[m]["vendor"],
                    "score": models_out[m]["score"],
                    "level": models_out[m]["level"],
                    "usable": models_out[m]["usable"],
                    "remaining_percent": models_out[m]["remaining_percent"],
                    "seconds_until_reset": models_out[m]["seconds_until_reset"],
                }
                for m in ranked
            ],
        }

    return {
        "server_time": _iso(current),
        "stale_after_seconds": stale_after_seconds,
        "algorithm": {
            "score": "binding window remaining % × 0.5 if pace exhausts it before reset",
            "level": {"critical_max": CRITICAL_REMAINING_PCT, "low_max": LOW_REMAINING_PCT},
            "usable": "status == ok and level not in (critical, unknown)",
            "recommended": "tier primary if usable, else highest-scoring usable candidate",
        },
        "pools": pools_out,
        "models": models_out,
        "tiers": tiers_out,
    }
