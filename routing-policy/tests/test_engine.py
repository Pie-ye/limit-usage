"""Tests for the pure functional routing policy recommendation engine.

Exercises candidate ranking, role-based filtering, cross-vendor review restrictions,
static ladder fallback indicators, and parity against the legacy routing view without
making network calls or modifying persistent policy definitions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import types
from typing import Any

import pytest

ROUTING_POLICY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROUTING_POLICY_ROOT))

from app.engine import REASON_CODES, Recommendation, recommend  # noqa: E402
from app.policy import Model, Policy, load_policy  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import app as routing_policy_app  # noqa: E402

LEGACY_APP_PATH = REPO_ROOT / "app"
if str(LEGACY_APP_PATH) not in routing_policy_app.__path__:
    routing_policy_app.__path__.append(str(LEGACY_APP_PATH))

# The legacy package initializer imports the poller and database even though
# parity only needs the pure routing module. A package shim keeps this focused
# test independent of optional SQLite support in the Python runtime.
legacy_services = types.ModuleType("app.services")
legacy_services.__path__ = [str(LEGACY_APP_PATH / "services")]
sys.modules["app.services"] = legacy_services

from app.models import AccountSnapshot, ProviderId, SnapshotStatus, UsageWindow  # noqa: E402
from app.services.routing_view import build_routing_payload  # noqa: E402

POLICY_DIR = ROUTING_POLICY_ROOT / "policy"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def policy() -> Policy:
    """Load the declarative routing policy documents."""
    return load_policy(POLICY_DIR)


def _default_signals(
    overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Generate baseline healthy signals for all five quota pools."""
    pools = {
        "claude": {
            "usable": True,
            "score": 90.0,
            "level": "ok",
            "cooling": False,
            "seconds_until_reset": 7200,
            "stale": False,
        },
        "codex": {
            "usable": True,
            "score": 85.0,
            "level": "ok",
            "cooling": False,
            "seconds_until_reset": 7200,
            "stale": False,
        },
        "grok": {
            "usable": True,
            "score": 80.0,
            "level": "ok",
            "cooling": False,
            "seconds_until_reset": 7200,
            "stale": False,
        },
        "agy": {
            "usable": True,
            "score": 75.0,
            "level": "ok",
            "cooling": False,
            "seconds_until_reset": 7200,
            "stale": False,
        },
        "agy-3p": {
            "usable": True,
            "score": 70.0,
            "level": "ok",
            "cooling": False,
            "seconds_until_reset": 7200,
            "stale": False,
        },
    }
    if overrides:
        for pool, vals in overrides.items():
            if pool in pools:
                pools[pool].update(vals)
            else:
                pools[pool] = vals
    return pools


def test_tier_selection_highest_score(policy: Policy) -> None:
    """a. When all pools are healthy with distinct scores, T0-T3 pick the highest-score candidate."""
    # agy has highest score (95.0), but agy models reach only up to max_tier=2.
    # grok (90.0) covers up to max_tier=3.
    signals = _default_signals(
        {
            "agy": {"score": 95.0},
            "grok": {"score": 90.0},
            "codex": {"score": 80.0},
            "claude": {"score": 70.0},
            "agy-3p": {"score": 60.0},
        }
    )

    # T0, T1, T2 have agy models available: gemini-3.8-flash-high (bench 81)
    for tier in ("T0", "T1", "T2"):
        rec = recommend(policy, signals, tier=tier, role="implement")
        assert rec.recommended is not None
        assert rec.recommended["model"] == "gemini-3.8-flash-high"
        assert rec.recommended["vendor"] == "agy"
        assert "tier_capable" in rec.reason_codes
        assert "quota_healthy" in rec.reason_codes
        assert "provider_healthy" in rec.reason_codes

    # T3 cannot use agy (max_tier=2), so it selects the highest among T3: grok-4.6 (score 90.0)
    rec_t3 = recommend(policy, signals, tier="T3", role="implement")
    assert rec_t3.recommended is not None
    assert rec_t3.recommended["model"] == "grok-4.6"
    assert rec_t3.recommended["vendor"] == "grok"


def test_bench_breaks_score_tie(policy: Policy) -> None:
    """b. When scores are identical, the model with higher benchmark capability is selected."""
    # Claude and Codex both have score 90.0; grok and agy have lower score.
    signals = _default_signals(
        {
            "claude": {"score": 90.0},
            "codex": {"score": 90.0},
            "grok": {"score": 50.0},
            "agy": {"score": 50.0},
            "agy-3p": {"score": 50.0},
        }
    )

    # In T3: claude-opus-5 (bench 92, cost_rank 14) vs gpt-5.6-sol (bench 90, cost_rank 13).
    # Despite higher cost_rank, claude-opus-5 wins due to superior bench.
    rec = recommend(policy, signals, tier="T3", role="implement")
    assert rec.recommended is not None
    assert rec.recommended["model"] == "claude-opus-5"
    assert rec.recommended["vendor"] == "claude"


def test_cost_rank_breaks_bench_and_score_tie(policy: Policy) -> None:
    """c. When score and bench are identical, the model with lower cost_rank is selected."""
    # Create two synthetic models with identical bench and pool, differing only in cost_rank.
    synthetic_models = dict(policy.models)
    synthetic_models["model-cheap"] = Model(
        pool="claude",
        slots=["5h", "1w"],
        max_tier=2,
        bench=99,
        cost_rank=3,
        role="subagent",
        reviewer=False,
        dispatchable=True,
    )
    synthetic_models["model-expensive"] = Model(
        pool="claude",
        slots=["5h", "1w"],
        max_tier=2,
        bench=99,
        cost_rank=7,
        role="subagent",
        reviewer=False,
        dispatchable=True,
    )
    custom_policy = policy.model_copy(update={"models": synthetic_models})

    signals = _default_signals({"claude": {"score": 99.0}})
    rec = recommend(
        custom_policy,
        signals,
        tier="T2",
        role="implement",
        available_vendors=["claude"],
    )

    assert rec.recommended is not None
    # model-cheap has lower cost_rank (3 vs 7) and wins the tie-break
    assert rec.recommended["model"] == "model-cheap"
    alt_models = [a["model"] for a in rec.alternatives]
    assert "model-expensive" in alt_models


def test_cross_vendor_review_exclusion(policy: Policy) -> None:
    """d. Review role excludes the implementing vendor and emits cross_vendor_review reason."""
    # Codex has highest score (99.0). Its reviewer is gpt-5.6-sol.
    signals = _default_signals(
        {
            "codex": {"score": 99.0},
            "claude": {"score": 85.0},
            "grok": {"score": 80.0},
        }
    )

    rec = recommend(
        policy,
        signals,
        tier="T2",
        role="review",
        implemented_by_vendor="codex",
    )

    assert rec.recommended is not None
    assert rec.recommended["vendor"] != "codex"
    assert all(alt["vendor"] != "codex" for alt in rec.alternatives)
    assert "cross_vendor_review" in rec.reason_codes


def test_no_eligible_candidate_and_wait_seconds(policy: Policy) -> None:
    """e. When no candidate is eligible, recommended is None and wait_seconds is the minimum wait."""
    signals = {
        "claude": {
            "usable": False,
            "score": None,
            "level": "critical",
            "cooling": False,
            "seconds_until_reset": 7200,
            "stale": False,
        },
        "codex": {
            "usable": False,
            "score": None,
            "level": "low",
            "cooling": True,
            "seconds_until_reset": 3600,
            "stale": False,
        },
        "grok": {
            "usable": False,
            "score": None,
            "level": "low",
            "cooling": False,
            "seconds_until_reset": 1800,
            "stale": False,
        },
        "agy": {
            "usable": False,
            "score": None,
            "level": "critical",
            "cooling": False,
            "seconds_until_reset": 5400,
            "stale": False,
        },
        "agy-3p": {
            "usable": False,
            "score": None,
            "level": "unknown",
            "cooling": False,
            "seconds_until_reset": None,
            "stale": False,
        },
    }

    rec = recommend(policy, signals, tier="T2", role="implement")
    assert rec.recommended is None
    assert rec.alternatives == []
    assert "no_eligible_candidate" in rec.reason_codes
    assert "fallback_static" in rec.reason_codes
    # Candidates for T2 include grok (1800), codex (3600), agy (5400), claude (7200).
    # Minimum recovery wait duration is 1800 seconds.
    assert rec.wait_seconds == 1800


def test_fable_dispatchable_implement_vs_orchestrate(policy: Policy) -> None:
    """f. claude-fable-5-1 is excluded from implement tiers but eligible for orchestrate."""
    signals = _default_signals({"claude": {"score": 100.0}})

    # Implement tiers T0-T3 must never recommend or suggest claude-fable-5-1.
    for tier in ("T0", "T1", "T2", "T3"):
        rec = recommend(policy, signals, tier=tier, role="implement")
        assert rec.recommended is not None
        assert rec.recommended["model"] != "claude-fable-5-1"
        assert all(alt["model"] != "claude-fable-5-1" for alt in rec.alternatives)

    # Orchestrate role admits claude-fable-5-1 as a candidate.
    rec_orch = recommend(policy, signals, tier="T3", role="orchestrate")
    assert rec_orch.recommended is not None
    assert rec_orch.recommended["model"] == "claude-fable-5-1"
    assert rec_orch.recommended["vendor"] == "claude"


def test_available_vendors_filtering(policy: Policy) -> None:
    """g. Restricting available_vendors filters out non-whitelisted vendors and adds reason code."""
    # agy and grok have the highest score, but caller allows only claude and codex.
    signals = _default_signals(
        {
            "agy": {"score": 100.0},
            "grok": {"score": 95.0},
            "claude": {"score": 80.0},
            "codex": {"score": 75.0},
        }
    )

    rec = recommend(
        policy,
        signals,
        tier="T2",
        role="implement",
        available_vendors=["claude", "codex"],
    )

    assert rec.recommended is not None
    assert rec.recommended["vendor"] in {"claude", "codex"}
    assert rec.recommended["vendor"] not in {"agy", "grok"}
    for alt in rec.alternatives:
        assert alt["vendor"] in {"claude", "codex"}
        assert alt["vendor"] not in {"agy", "grok"}
    assert "vendor_filtered" in rec.reason_codes


def _project_pools_to_signals(
    pools_out: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Project the legacy routing view pool dictionary to sanitized engine signals."""
    signals: dict[str, dict[str, Any]] = {}
    for pool_id, pool_data in pools_out.items():
        binding_slot = pool_data.get("binding_slot")
        window = (
            pool_data.get("windows", {}).get(binding_slot)
            if binding_slot
            else None
        )
        sur = window.get("seconds_until_reset") if window else None
        cooling = pool_data.get("cooldown", {}).get("cooling", False)
        signals[pool_id] = {
            "usable": pool_data["usable"],
            "score": pool_data["score"],
            "level": pool_data["level"],
            "cooling": cooling,
            "seconds_until_reset": sur,
            "stale": pool_data["stale"],
        }
    return signals


def _win(
    key: str, used: float, reset_in_s: int, limit: int | None = None
) -> UsageWindow:
    return UsageWindow(
        key=key,
        label=key,
        used_percent=used,
        remaining_percent=100 - used,
        resets_at=NOW + timedelta(seconds=reset_in_s),
        limit_window_seconds=limit,
    )


def _snap(
    pid: ProviderId,
    windows: list[UsageWindow],
    *,
    age_s: int = 60,
    status: SnapshotStatus = SnapshotStatus.OK,
) -> AccountSnapshot:
    return AccountSnapshot(
        provider=pid,
        display_name=pid.value,
        status=status,
        windows=windows,
        fetched_at=NOW - timedelta(seconds=age_s),
    )


def test_parity_with_routing_view(policy: Policy) -> None:
    """h. Parity test asserting identical recommendations to build_routing_payload."""
    snapshots = [
        _snap(
            ProviderId.CODEX,
            [_win("5h", 2, 13_000, 18000), _win("1w", 0, 600_000, 604800)],
        ),
        _snap(
            ProviderId.SUPERGROK,
            [_win("weekly", 28, 350_000, 604800), _win("product-4", 2, 350_000)],
        ),
        _snap(
            ProviderId.ANTIGRAVITY,
            [
                _win("1w", 21.58, 230_000, 604800),
                _win("5h", 1.33, 16_000, 18000),
                _win("3p-1w", 0, 200_000, 604800),
                _win("3p-5h", 0, 17_000, 18000),
            ],
        ),
        _snap(
            ProviderId.CLAUDE,
            [
                _win("5h", 29.0, 11_000, 18000),
                _win("1w", 22.0, 340_000, 604800),
                _win("1w-fable", 36.0, 340_000, 604800),
            ],
        ),
    ]

    payload = build_routing_payload(snapshots, now=NOW)
    signals = _project_pools_to_signals(payload["pools"])

    # Parity check for implementation tiers T0-T3.
    for tier in ("T0", "T1", "T2", "T3"):
        rec = recommend(policy, signals, tier=tier, role="implement")
        legacy_rec = payload["tiers"][tier]["recommended"]
        assert rec.recommended is not None
        assert rec.recommended["model"] == legacy_rec

    # Parity check for reviewer role.
    rec_review = recommend(policy, signals, tier="T2", role="review")
    legacy_review_rec = payload["tiers"]["review"]["recommended"]
    assert rec_review.recommended is not None
    assert rec_review.recommended["model"] == legacy_review_rec

    # Intentional divergence check when no candidates are eligible:
    # Legacy routing_view falls back to ranked[0] when eligible candidates is 0,
    # whereas engine.recommend intentionally returns recommended=None so caller
    # static ladder fallbacks can take over cleanly.
    error_snapshots = [
        _snap(
            ProviderId.CODEX,
            [_win("5h", 99, 13_000)],
            status=SnapshotStatus.ERROR,
        ),
        _snap(
            ProviderId.SUPERGROK,
            [_win("weekly", 99, 350_000)],
            status=SnapshotStatus.ERROR,
        ),
        _snap(
            ProviderId.ANTIGRAVITY,
            [_win("1w", 99, 230_000)],
            status=SnapshotStatus.ERROR,
        ),
        _snap(
            ProviderId.CLAUDE,
            [_win("5h", 99, 11_000)],
            status=SnapshotStatus.ERROR,
        ),
    ]
    error_payload = build_routing_payload(error_snapshots, now=NOW)
    error_signals = _project_pools_to_signals(error_payload["pools"])

    rec_err = recommend(policy, error_signals, tier="T2", role="implement")
    # Legacy routing_view returned an unusable model fallback:
    assert error_payload["tiers"]["T2"]["recommended"] is not None
    # Engine intentionally returns None:
    assert rec_err.recommended is None
    assert "no_eligible_candidate" in rec_err.reason_codes
    assert "fallback_static" in rec_err.reason_codes


def test_missing_pool_handling(policy: Policy) -> None:
    """Missing pools default to unusable and unknown level without raising errors."""
    # Provide only codex signal; claude, grok, agy are completely omitted
    signals = {
        "codex": {
            "usable": True,
            "score": 80.0,
            "level": "ok",
            "cooling": False,
            "seconds_until_reset": 3600,
            "stale": False,
        }
    }
    rec = recommend(policy, signals, tier="T3", role="implement")
    assert rec.recommended is not None
    # Only codex is usable, so gpt-5.6-sol should be recommended
    assert rec.recommended["model"] == "gpt-5.6-sol"
    assert rec.recommended["vendor"] == "codex"


def test_stale_signals_reason_code(policy: Policy) -> None:
    """Stale signals on the recommended model add stale_signals to reason codes."""
    signals = _default_signals({"claude": {"score": 99.0, "stale": True}})
    rec = recommend(policy, signals, tier="T3", role="implement")
    assert rec.recommended is not None
    assert rec.recommended["model"] == "claude-opus-5"
    assert "stale_signals" in rec.reason_codes


def test_reason_codes_strict_order(policy: Policy) -> None:
    """Reason codes must follow the fixed specification order without duplicates."""
    signals = _default_signals({"claude": {"score": 99.0, "stale": True}})
    rec = recommend(
        policy,
        signals,
        tier="T2",
        role="review",
        implemented_by_vendor="codex",
        available_vendors=["claude", "codex"],
    )

    expected_order = [code for code in REASON_CODES if code in rec.reason_codes]
    assert rec.reason_codes == expected_order
    assert len(rec.reason_codes) == len(set(rec.reason_codes))


def test_response_minimal_surface(policy: Policy) -> None:
    """Recommendation strictly exposes only public vendor/model pairs and no sensitive internals."""
    signals = _default_signals()
    rec = recommend(policy, signals, tier="T2", role="implement")
    assert rec.recommended is not None
    assert set(rec.recommended.keys()) == {"vendor", "model"}
    for alt in rec.alternatives:
        assert set(alt.keys()) == {"vendor", "model"}
