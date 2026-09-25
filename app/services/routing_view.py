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
the dispatchable models whose catalog-derived ``[min_tier, max_tier]`` interval
contains it (``review`` uses the derived ``reviewer`` flag); ``recommended`` is
the usable one with the highest quota score, then the highest ``bench``
(benchmark index) among equal scores, then the cheapest. ``candidates`` is
always returned in that order so the caller can apply its own policy instead.

Dispatch feedback (see ``app.services.routing_feedback``): a pool that a
subagent just found rate-limited or broken is ``cooling`` until
``unavailable_until`` and is not usable meanwhile, whatever the poller says.

Caller filters (all optional, applied to ``eligible`` and ``recommended`` but
never to ``usable``, which stays a fact about the pool):

* ``avoid_vendor`` — cross-vendor review rule (reviewer ≠ implementer vendor)
* ``vendors`` — restrict to these vendor CLIs (Trellis channel only spawns
  ``claude`` / ``codex`` workers)
* ``min_score`` — floor on the quota score

When a tier has no eligible candidate, ``next_available_at`` /
``wait_seconds`` say when the earliest one comes back (end of cooldown or
window reset), so the dispatcher can choose between waiting and the static
ladder.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from app.models import AccountSnapshot, ProviderId, UsageWindow, utcnow
from app.services.analytics import (
    CRITICAL_REMAINING_PCT,
    LOW_REMAINING_PCT,
    compute_burn_estimate,
    extract_series_points,
)
from app.services.cli_models import CliModels
from catalog.tiering import (
    Catalog,
    Derived,
    load_catalog,
    tier_candidates as catalog_tier_candidates,
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

CATALOG = load_catalog()


def _tier_int(tier_str: str | None) -> int | None:
    if tier_str is None:
        return None
    if tier_str.startswith("T") and tier_str[1:].isdigit():
        return int(tier_str[1:])
    return None


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


_NO_COOLDOWN: dict[str, Any] = {
    "cooling": False,
    "unavailable_until": None,
    "seconds_left": 0,
    "backoff_level": 0,
    "kind": None,
    "last_error": None,
}


def _cooldown_view(cooldowns: dict[str, dict[str, Any]], pool_id: str) -> dict[str, Any]:
    state = cooldowns.get(pool_id)
    if not state or not state.get("cooling"):
        return dict(_NO_COOLDOWN)
    return {k: state.get(k, _NO_COOLDOWN[k]) for k in _NO_COOLDOWN}


def _wait_seconds(row: dict[str, Any]) -> int | None:
    """Seconds until a non-eligible model row could become usable again."""
    if row.get("listed") is False:
        return None
    missing = row.get("missing")
    if missing and isinstance(missing, dict):
        return int(missing.get("seconds_left") or 0)
    cd = row.get("cooldown") or {}
    if cd.get("cooling"):
        return int(cd.get("seconds_left") or 0)
    if row.get("level") in {"critical", "low"} and row.get("seconds_until_reset") is not None:
        return int(row["seconds_until_reset"])
    return None


def build_routing_payload(
    snapshots: list[AccountSnapshot],
    history_by_provider: dict[str, list[dict[str, Any]]] | None = None,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    cooldowns: dict[str, dict[str, Any]] | None = None,
    missing_models: Mapping[str, Mapping[str, Any]] | None = None,
    cli_models: CliModels | None = None,
    avoid_vendor: str | None = None,
    vendors: Iterable[str] | None = None,
    min_score: float | None = None,
    catalog: Catalog | None = None,
) -> dict[str, Any]:
    active_catalog = catalog if catalog is not None else CATALOG
    current = _as_utc(now or utcnow())
    history_by_provider = history_by_provider or {}
    cooldowns = cooldowns or {}
    vendor_set = {v.strip() for v in vendors if v.strip()} if vendors else None
    by_id = {s.provider: s for s in snapshots}

    pools_spec = {
        pool_id: {
            "provider": ProviderId(pool["provider"]),
            "display_name": pool["display_name"],
            "slots": dict(pool["slots"]),
        }
        for pool_id, pool in active_catalog.pools.items()
    }

    pools_out: dict[str, Any] = {}
    for pool_id, spec in pools_spec.items():
        snap = by_id.get(spec["provider"])
        cooldown = _cooldown_view(cooldowns, pool_id)
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
                "cooldown": cooldown,
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
        usable = (
            snap.status.value == "ok"
            and scored["level"] not in {"critical", "unknown"}
            and not cooldown["cooling"]
        )
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
            "cooldown": cooldown,
            "message": snap.message,
            **scored,
        }

    models_out: dict[str, Any] = {}
    for model_id, d in active_catalog.models.items():
        pool = pools_out.get(d.pool)
        if not pool:
            continue
        scored = _score(pool["windows"], list(d.slots))
        binding = pool["windows"].get(scored["binding_slot"]) if scored["binding_slot"] else None
        listed = cli_models.listed(d.vendor, model_id) if cli_models else None
        missing = (missing_models or {}).get(model_id)
        usable = (
            pool["status"] == "ok"
            and scored["level"] not in {"critical", "unknown"}
            and not pool["cooldown"]["cooling"]
            and listed is not False
            and missing is None
        )
        models_out[model_id] = {
            "vendor": d.vendor,
            "pool": d.pool,
            "slots": list(d.slots),
            "role": d.role,
            "max_tier": _tier_int(d.max_tier),
            "min_tier": _tier_int(d.min_tier),
            "tiers": list(d.tiers),
            "bench": d.bench,
            "blended_price": d.blended_price,
            "effort": d.effort,
            "reviewer": d.reviewer,
            "orchestrator": d.orchestrator,
            "dispatchable": d.dispatchable,
            "listed": listed,
            "missing": missing,
            "status": pool["status"],
            "stale": pool["stale"],
            "usable": usable,
            "cooldown": pool["cooldown"],
            "remaining_percent": binding["remaining_percent"] if binding else None,
            "seconds_until_reset": binding["seconds_until_reset"] if binding else None,
            "resets_at": binding["resets_at"] if binding else None,
            "burn_per_hour": binding["burn_per_hour"] if binding else None,
            "will_last_until_reset": binding["will_last_until_reset"] if binding else None,
            **scored,
        }

    def _eligible(m: str) -> bool:
        row = models_out[m]
        if not row["usable"]:
            return False
        if avoid_vendor and row["vendor"] == avoid_vendor:
            return False
        if vendor_set is not None and row["vendor"] not in vendor_set:
            return False
        if min_score is not None and (row["score"] is None or row["score"] < min_score):
            return False
        return True

    filters_active = bool(avoid_vendor) or vendor_set is not None or min_score is not None

    tier_list = list(active_catalog.tier_ids) + ["review"]
    tiers_out: dict[str, Any] = {}
    for tier_id in tier_list:
        role = "review" if tier_id == "review" else "implement"
        candidates = catalog_tier_candidates(active_catalog, tier_id)
        ranked = sorted(
            (m for m in candidates if m in models_out),
            key=lambda m: (
                not _eligible(m),
                not models_out[m]["usable"],
                -(models_out[m]["score"] if models_out[m]["score"] is not None else -1.0),
                -active_catalog.models[m].bench,
                active_catalog.models[m].blended_price,
            ),
        )
        eligible = [m for m in ranked if _eligible(m)]
        recommended = eligible[0] if eligible else (ranked[0] if ranked else None)
        rec = models_out.get(recommended, {}) if recommended else {}
        usable_n = sum(1 for m in ranked if models_out[m]["usable"])
        waits = [w for w in (_wait_seconds(models_out[m]) for m in ranked if m not in eligible) if w is not None]
        wait = min(waits) if (waits and not eligible) else None
        if eligible:
            reason = (
                f"{recommended} has the highest quota score ({rec.get('score')}) of {len(eligible)} eligible candidates"
                f", strongest benchmark (bench {rec.get('bench')}) among equals"
            )
        elif usable_n and filters_active:
            reason = "no candidate passes the caller's filters; static ladder should decide"
        else:
            reason = "no usable candidate; static ladder should decide"
        if wait is not None:
            reason += f"; earliest candidate back in {wait}s"
        tiers_out[tier_id] = {
            "role": role,
            "recommended": recommended,
            "vendor": rec.get("vendor"),
            "usable_candidates": usable_n,
            "eligible_candidates": len(eligible),
            "next_available_at": _iso(current + timedelta(seconds=wait)) if wait is not None else None,
            "wait_seconds": wait,
            "reason": reason,
            "candidates": [
                {
                    "model": m,
                    "vendor": models_out[m]["vendor"],
                    "role": active_catalog.models[m].role,
                    "max_tier": _tier_int(active_catalog.models[m].max_tier),
                    "min_tier": _tier_int(active_catalog.models[m].min_tier),
                    "bench": active_catalog.models[m].bench,
                    "blended_price": active_catalog.models[m].blended_price,
                    "effort": active_catalog.models[m].effort,
                    "score": models_out[m]["score"],
                    "level": models_out[m]["level"],
                    "usable": models_out[m]["usable"],
                    "eligible": m in eligible,
                    "cooling": models_out[m]["cooldown"]["cooling"],
                    "remaining_percent": models_out[m]["remaining_percent"],
                    "seconds_until_reset": models_out[m]["seconds_until_reset"],
                    "wait_seconds": None if m in eligible else _wait_seconds(models_out[m]),
                }
                for m in ranked
            ],
        }

    return {
        "server_time": _iso(current),
        "stale_after_seconds": stale_after_seconds,
        "filters": {
            "avoid_vendor": avoid_vendor or None,
            "vendors": sorted(vendor_set) if vendor_set is not None else None,
            "min_score": min_score,
        },
        "algorithm": {
            "score": "binding window remaining % × 0.5 if pace exhausts it before reset",
            "level": {"critical_max": CRITICAL_REMAINING_PCT, "low_max": LOW_REMAINING_PCT},
            "usable": "status == ok and level not in (critical, unknown) and pool not cooling after dispatch feedback",
            "eligible": "usable and passes avoid_vendor / vendors / min_score filters",
            "candidates": "every dispatchable model whose derived tier range [min_tier, max_tier] contains the tier (review: derived reviewers)",
            "recommended": "highest quota score among eligible candidates; bench (benchmark index) breaks ties, then blended_price",
            "tiering": "blended = (input*blend.input + output*blend.output)/(blend.input+blend.output); max_tier = highest tier with bench >= min_bench; min_tier = lowest tier with blended <= max_blended_price",
            "wait_seconds": "when nothing is eligible: earliest cooldown end or window reset among candidates",
        },
        "catalog": {
            "catalog_version": active_catalog.catalog_version,
            "blend": dict(active_catalog.blend),
            "thresholds": {t: dict(v) for t, v in active_catalog.thresholds.items()},
            "uncatalogued": cli_models.uncatalogued(active_catalog.models.keys()) if cli_models else {},
            "cli_models": cli_models.summary() if cli_models else None,
        },
        "pools": pools_out,
        "models": models_out,
        "orchestrators": sorted(
            (m for m, d in active_catalog.models.items() if d.orchestrator and m in models_out),
            key=lambda m: (
                not models_out[m]["usable"],
                -(models_out[m]["score"] if models_out[m]["score"] is not None else -1.0),
                -active_catalog.models[m].bench,
                active_catalog.models[m].blended_price,
            ),
        ),
        "tiers": tiers_out,
    }
