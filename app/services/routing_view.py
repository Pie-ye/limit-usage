"""Quota-aware model routing view for the Trellis / orchestrating-development dispatcher.

Projects the provider snapshots plus recent history into one JSON document that
answers, per vendor CLI pool and per model id:

* how much of each rate-limit window is left (``remaining_percent``)
* how long until that window resets (``seconds_until_reset``)
* how fast it is being consumed (``burn_per_hour``, % of window per hour)
* whether the current pace exhausts the window before it resets
  (``will_last_until_reset``, ``projected_used_at_reset``)
* a single ``score`` (0–100) and ``level`` (ok/low/critical/unknown) per pool,
  and a ``recommended`` model per dispatch tier (tier = task difficulty).

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

Tier recommendation: no model is pinned to a tier. A tier's candidates are
all models whose ``max_tier`` covers it; ``recommended`` is the usable one
with the highest quota score, then the highest ``bench`` (benchmark index)
among equal scores, then the cheapest. ``candidates`` is always
returned in that order so the caller can apply its own policy instead.
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

# Every model the four local CLIs expose (claude / codex / grok / agy), with:
#   pool       quota pool it draws on
#   slots      which windows bind it
#   max_tier   hardest tier it may take, set from benchmarks (2026-09-10):
#              T3 ≈ SWE-bench Pro ≥ ~65 or Terminal-Bench 4.0 top group
#              T2 ≈ SWE-bench Pro ~60–65 / Terminal-Bench 2.1 ≥ ~84
#              T1 ≈ previous-gen or reduced-effort variants
#              T0 ≈ no published coding benchmark, "fast and affordable"
#   bench      composite coding-benchmark index (0–100, higher = stronger),
#              derived from the same evidence as max_tier; used as the
#              tie-break after quota score so equal-quota candidates (same
#              pool) resolve to the strongest model, not the cheapest
#   cost_rank  price order, cheapest first; last tie-break only
#   role       "orchestrator" (may run the planning session; also dispatchable
#              at its tier) or "subagent" (dispatch only)
#   reviewer   strong enough to cross-review T2/T3 work
#   dispatchable=False  reported for its quota but never handed to dispatch
# Evidence per row is in orchestrating-development references/routing.md.
MODELS: dict[str, dict[str, Any]] = {
    # --- agy (Antigravity, gemini-* only) ---
    "gemini-3.8-flash-low": {"pool": "agy", "slots": ["5h", "1w"], "max_tier": 0, "cost_rank": 0, "bench": 55, "role": "subagent"},
    "gemini-3.8-flash-medium": {"pool": "agy", "slots": ["5h", "1w"], "max_tier": 1, "cost_rank": 1, "bench": 70, "role": "subagent"},
    "gemini-3.8-flash-high": {"pool": "agy", "slots": ["5h", "1w"], "max_tier": 2, "cost_rank": 2, "bench": 81, "role": "subagent"},
    "gemini-3.1-pro-low": {"pool": "agy", "slots": ["5h", "1w"], "max_tier": 1, "cost_rank": 5, "bench": 65, "role": "subagent"},
    "gemini-3.1-pro-high": {"pool": "agy", "slots": ["5h", "1w"], "max_tier": 2, "cost_rank": 6, "bench": 74, "role": "subagent"},
    # --- codex ---
    "gpt-reserve": {"pool": "codex", "slots": ["5h", "1w"], "max_tier": 0, "cost_rank": 3, "bench": 50, "role": "subagent"},
    "gpt-5.6-luna": {"pool": "codex", "slots": ["5h", "1w"], "max_tier": 2, "cost_rank": 7, "bench": 78, "role": "subagent"},
    "gpt-5.5": {"pool": "codex", "slots": ["5h", "1w"], "max_tier": 1, "cost_rank": 9, "bench": 72, "role": "subagent"},
    "gpt-5.6-terra": {"pool": "codex", "slots": ["5h", "1w"], "max_tier": 2, "cost_rank": 12, "bench": 82, "role": "subagent"},
    "gpt-5.6-sol": {"pool": "codex", "slots": ["5h", "1w"], "max_tier": 3, "cost_rank": 13, "bench": 90, "role": "orchestrator", "reviewer": True},
    # --- grok ---
    "grok-4.5": {"pool": "grok", "slots": ["1w"], "max_tier": 2, "cost_rank": 8, "bench": 77, "role": "subagent"},
    "grok-4.6": {"pool": "grok", "slots": ["1w"], "max_tier": 3, "cost_rank": 10, "bench": 86, "role": "orchestrator", "reviewer": True},
    # --- claude ---
    "claude-haiku-4-5-20251001": {"pool": "claude", "slots": ["5h", "1w"], "max_tier": 1, "cost_rank": 4, "bench": 60, "role": "subagent"},
    "claude-sonnet-5": {"pool": "claude", "slots": ["5h", "1w"], "max_tier": 2, "cost_rank": 11, "bench": 80, "role": "subagent"},
    "claude-opus-5": {"pool": "claude", "slots": ["5h", "1w"], "max_tier": 3, "cost_rank": 14, "bench": 92, "role": "orchestrator", "reviewer": True},
    # Orchestrator-only: reported under ``models`` for its Fable weekly cap,
    # never offered as a dispatch candidate (it would burn the planner's cap).
    "claude-fable-5-1": {"pool": "claude", "slots": ["5h", "1w", "1w-fable"], "max_tier": 3, "cost_rank": 15, "bench": 95, "role": "orchestrator", "dispatchable": False},
}

# Tier = task difficulty score only (orchestrating-development P2: 0–2 T0,
# 3–5 T1, 6–8 T2, 9–12 T3). No model is pinned to a tier: every model whose
# ``max_tier`` covers the tier is a candidate, ranked by live quota score,
# then ``bench``, then ``cost_rank``. ``review`` only admits ``reviewer`` models;
# the cross-vendor rule (reviewer ≠ implementer vendor) is applied by dispatch,
# which knows who actually implemented.
TIER_LEVELS = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}


def tier_candidates(tier_id: str) -> list[str]:
    if tier_id == "review":
        pool = [m for m, spec in MODELS.items() if spec.get("reviewer")]
    else:
        level = TIER_LEVELS[tier_id]
        pool = [
            m for m, spec in MODELS.items()
            if spec["max_tier"] >= level and spec.get("dispatchable", True)
        ]
    return sorted(pool, key=lambda m: MODELS[m]["cost_rank"])


TIERS: dict[str, dict[str, Any]] = {
    **{t: {"role": "implement", "candidates": tier_candidates(t)} for t in TIER_LEVELS},
    "review": {"role": "review", "candidates": tier_candidates("review")},
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
            "role": spec["role"],
            "max_tier": spec["max_tier"],
            "bench": spec["bench"],
            "cost_rank": spec["cost_rank"],
            "dispatchable": spec.get("dispatchable", True),
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
                not models_out[m]["usable"],
                -(models_out[m]["score"] if models_out[m]["score"] is not None else -1.0),
                -MODELS[m]["bench"],
                MODELS[m]["cost_rank"],
            ),
        )
        recommended = ranked[0] if ranked else None
        rec = models_out.get(recommended, {}) if recommended else {}
        usable_n = sum(1 for m in ranked if models_out[m]["usable"])
        tiers_out[tier_id] = {
            "role": spec["role"],
            "recommended": recommended,
            "vendor": rec.get("vendor"),
            "usable_candidates": usable_n,
            "reason": (
                f"{recommended} has the highest quota score ({rec.get('score')}) of {usable_n} usable candidates"
                + f", strongest benchmark (bench {rec.get('bench')}) among equals"
                if recommended and rec.get("usable")
                else "no usable candidate; static ladder should decide"
            ),
            "candidates": [
                {
                    "model": m,
                    "vendor": models_out[m]["vendor"],
                    "role": MODELS[m]["role"],
                    "max_tier": MODELS[m]["max_tier"],
                    "bench": MODELS[m]["bench"],
                    "cost_rank": MODELS[m]["cost_rank"],
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
            "candidates": "every model whose max_tier covers the tier (review: reviewer models)",
            "recommended": "highest quota score among usable candidates; bench (benchmark index) breaks ties, then cost_rank",
        },
        "pools": pools_out,
        "models": models_out,
        "orchestrators": sorted(
            (m for m, spec in MODELS.items() if spec["role"] == "orchestrator" and m in models_out),
            key=lambda m: (
                not models_out[m]["usable"],
                -(models_out[m]["score"] if models_out[m]["score"] is not None else -1.0),
                -MODELS[m]["bench"],
                MODELS[m]["cost_rank"],
            ),
        ),
        "tiers": tiers_out,
    }
