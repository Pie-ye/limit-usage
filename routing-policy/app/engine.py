"""Pure functional model recommendation engine for routing policies.

This engine evaluates real-time quota signals against declarative policy rules to
select the optimal model for a requested tier and role. Operating as a pure function
without network or filesystem I/O, it maintains identical ranking semantics to the
legacy routing view (score -> bench -> cost_rank) while enforcing a strict API boundary
that exposes only public vendor/model identifiers and stable reason codes.

Unlike legacy routing_view which falls back to ranked[0] when no candidate is
eligible, this engine returns None for recommended so callers can invoke their own
static fallback ladder honestly rather than receiving an unusable model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from app.policy import Policy

Signals = Mapping[str, Mapping[str, Any]]

REASON_CODES: tuple[str, ...] = (
    "tier_capable",
    "quota_healthy",
    "quota_degraded",
    "provider_healthy",
    "cross_vendor_review",
    "vendor_filtered",
    "stale_signals",
    "no_eligible_candidate",
    "fallback_static",
)


@dataclass(frozen=True)
class Recommendation:
    """Public recommendation response containing minimal operational surface."""

    tier: str
    role: str
    recommended: dict[str, str] | None
    alternatives: list[dict[str, str]]
    reason_codes: list[str]
    wait_seconds: int | None


def _model_vendor(pool_id: str) -> str:
    """Derive public vendor by taking the leading segment before any delimiter."""
    return pool_id.split("-", 1)[0]


def _is_candidate_eligible(
    *,
    usable: bool,
    vendor: str,
    score: float | None,
    role: str,
    implemented_by_vendor: str | None,
    available_vendors: set[str] | None,
    min_score: float | None,
    review_cross_vendor: bool,
) -> bool:
    """Determine candidate eligibility using caller constraints layered on live pool health."""
    if not usable:
        return False
    # Cross-vendor review prevents a model vendor from approving its own work.
    if implemented_by_vendor and role == "review" and review_cross_vendor:
        if vendor == implemented_by_vendor:
            return False
    # Allow caller to restrict recommendations to pre-approved providers.
    if available_vendors is not None and vendor not in available_vendors:
        return False
    # Minimum score threshold avoids selecting severely exhausted pools.
    if min_score is not None and (score is None or score < min_score):
        return False
    return True


def _compute_wait_seconds(
    signal: Mapping[str, Any] | None,
) -> int | None:
    """Compute recovery wait duration for a non-eligible pool.

    Cooldown remaining time is absent from sanitized signals, so reset time acts as
    the conservative return estimate when cooling or degraded.
    """
    if signal is None:
        return None
    cooling = bool(signal.get("cooling", False))
    level = signal.get("level", "unknown")
    seconds_until_reset = signal.get("seconds_until_reset")
    if seconds_until_reset is not None:
        reset_int = int(seconds_until_reset)
        if cooling or level in ("critical", "low"):
            return reset_int
    return None


def recommend(
    policy: Policy,
    signals: Signals,
    *,
    tier: str,
    role: str,
    implemented_by_vendor: str | None = None,
    available_vendors: list[str] | None = None,
    min_score: float | None = None,
    now: datetime | None = None,
) -> Recommendation:
    """Select the best candidate model according to quota signals and policy.

    The ranking key mirrors legacy routing: eligible models sort first, followed by
    usable status, descending live quota score, descending benchmark capability, and
    ascending cost rank. Unlike legacy routing_view which falls back to ranked[0],
    this function sets recommended to None when no candidate is eligible to enable
    clean client-side static fallbacks.
    """
    # 1. Candidate population based on requested role.
    if role == "review":
        candidates = [
            m for m, spec in policy.models.items() if spec.reviewer
        ]
    elif role == "orchestrate":
        # Orchestrators permit dispatchable=False models (e.g. planner-dedicated fable)
        # while honoring explicit vendor exclusions from policy.
        excluded_vendors = set(policy.orchestrator_excluded_vendors)
        candidates = [
            m
            for m, spec in policy.models.items()
            if spec.role == "orchestrator"
            and _model_vendor(spec.pool) not in excluded_vendors
        ]
    else:
        # Implementation tasks require dispatchable models whose max_tier covers tier.
        candidates = policy.tier_candidates(tier)

    # 2. Extract per-candidate sanitized metrics from pool signals.
    normalized_available_vendors = (
        {v.strip() for v in available_vendors} if available_vendors is not None else None
    )

    candidate_data: dict[str, dict[str, Any]] = {}
    for m in candidates:
        spec = policy.models[m]
        vendor = _model_vendor(spec.pool)
        sig = signals.get(spec.pool)
        if sig is None:
            usable = False
            score = None
            level = "unknown"
            cooling = False
            stale = False
        else:
            usable = bool(sig.get("usable", False))
            raw_score = sig.get("score")
            score = float(raw_score) if raw_score is not None else None
            level = str(sig.get("level", "unknown"))
            cooling = bool(sig.get("cooling", False))
            stale = bool(sig.get("stale", False))

        eligible = _is_candidate_eligible(
            usable=usable,
            vendor=vendor,
            score=score,
            role=role,
            implemented_by_vendor=implemented_by_vendor,
            available_vendors=normalized_available_vendors,
            min_score=min_score,
            review_cross_vendor=policy.review_cross_vendor,
        )

        candidate_data[m] = {
            "vendor": vendor,
            "spec": spec,
            "usable": usable,
            "score": score,
            "level": level,
            "cooling": cooling,
            "stale": stale,
            "eligible": eligible,
            "signal": sig,
        }

    # 3. Sort candidates using identical key structure to routing_view.
    ranked = sorted(
        candidates,
        key=lambda m: (
            not candidate_data[m]["eligible"],
            not candidate_data[m]["usable"],
            -(
                candidate_data[m]["score"]
                if candidate_data[m]["score"] is not None
                else -1.0
            ),
            -candidate_data[m]["spec"].bench,
            candidate_data[m]["spec"].cost_rank,
        ),
    )

    eligible_models = [m for m in ranked if candidate_data[m]["eligible"]]

    # 4. Determine recommendation and alternatives (maximum 3 runners-up).
    if eligible_models:
        top_model: str | None = eligible_models[0]
        recommended: dict[str, str] | None = {
            "vendor": candidate_data[top_model]["vendor"],
            "model": top_model,
        }
        alternatives = [
            {
                "vendor": candidate_data[m]["vendor"],
                "model": m,
            }
            for m in eligible_models[1:4]
        ]
        wait_seconds = None
    else:
        top_model = None
        recommended = None
        alternatives = []
        # Minimum expected recovery time across all non-eligible candidates.
        waits = [
            w
            for m in candidates
            if (w := _compute_wait_seconds(candidate_data[m]["signal"])) is not None
        ]
        wait_seconds = min(waits) if waits else None

    # 5. Populate stable, ordered reason codes.
    top_info = candidate_data[top_model] if top_model is not None else None
    reason_codes: list[str] = []

    if (
        top_info is not None
        and tier in policy.tiers
        and top_info["spec"].max_tier >= policy.tiers[tier].level
    ):
        reason_codes.append("tier_capable")

    if top_info is not None and top_info["level"] == "ok":
        reason_codes.append("quota_healthy")

    if top_info is not None and top_info["level"] == "low":
        reason_codes.append("quota_degraded")

    if top_info is not None and top_info["usable"] and not top_info["cooling"]:
        reason_codes.append("provider_healthy")

    if bool(implemented_by_vendor) and role == "review" and policy.review_cross_vendor:
        reason_codes.append("cross_vendor_review")

    if available_vendors is not None:
        reason_codes.append("vendor_filtered")

    if top_info is not None and top_info["stale"]:
        reason_codes.append("stale_signals")

    if top_model is None:
        reason_codes.append("no_eligible_candidate")
        reason_codes.append("fallback_static")

    return Recommendation(
        tier=tier,
        role=role,
        recommended=recommended,
        alternatives=alternatives,
        reason_codes=reason_codes,
        wait_seconds=wait_seconds,
    )
