"""Tests for the pure functional routing policy recommendation engine."""

from __future__ import annotations

import shutil
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

ROUTING_POLICY_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROUTING_POLICY_ROOT.parent
sys.path.insert(0, str(ROUTING_POLICY_ROOT))

from app.engine import REASON_CODES, recommend  # noqa: E402
from app.policy import Model, Policy, load_policy  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))

import app as routing_policy_app  # noqa: E402

LEGACY_APP_PATH = REPO_ROOT / "app"
if str(LEGACY_APP_PATH) not in routing_policy_app.__path__:
    routing_policy_app.__path__.append(str(LEGACY_APP_PATH))

# Both services use the package name ``app``. Extend the routing-policy package
# and shim the legacy services package so this test can exercise both pure APIs.
legacy_services = types.ModuleType("app.services")
legacy_services.__path__ = [str(LEGACY_APP_PATH / "services")]
sys.modules["app.services"] = legacy_services

from app.models import AccountSnapshot, ProviderId, SnapshotStatus, UsageWindow  # noqa: E402
from app.services.routing_view import build_routing_payload  # noqa: E402
from catalog.tiering import Catalog, load_catalog  # noqa: E402

POLICY_DIR = ROUTING_POLICY_ROOT / "policy"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _catalog_model(
    pool: str,
    slots: list[str],
    bench: int,
    price: float,
    *,
    effort: str | None = None,
    dispatchable: bool = True,
) -> dict[str, Any]:
    model: dict[str, Any] = {
        "pool": pool,
        "slots": slots,
        "bench": bench,
        "price": {
            "input": price,
            "output": price,
            "as_of": "2026-09-26",
            "source": "https://example.test/pricing",
        },
    }
    if effort is not None:
        model["effort"] = effort
    if not dispatchable:
        model["dispatchable"] = False
    return model


def _build_synthetic_policy(tmp_path: Path) -> tuple[Policy, Catalog]:
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    shutil.copy(POLICY_DIR / "tiers.yaml", policy_dir / "tiers.yaml")
    shutil.copy(POLICY_DIR / "roles.yaml", policy_dir / "roles.yaml")

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    models_document = {
        "schema_version": 2,
        "catalog_version": "test-catalog.1",
        "pools": {
            "claude": {
                "provider": "claude",
                "display_name": "Test Claude",
                "slots": {"5h": "5h", "1w": "1w", "1w-fable": "1w-fable"},
            },
            "codex": {
                "provider": "codex",
                "display_name": "Test Codex",
                "slots": {"5h": "5h", "1w": "1w"},
            },
            "grok": {
                "provider": "supergrok",
                "display_name": "Test Grok",
                "slots": {"1w": "weekly"},
            },
            "agy": {
                "provider": "antigravity",
                "display_name": "Test Agy",
                "slots": {"5h": "5h", "1w": "1w"},
            },
            "agy-3p": {
                "provider": "antigravity",
                "display_name": "Test Agy Third Party",
                "slots": {"5h": "3p-5h", "1w": "3p-1w"},
            },
        },
        "models": {
            "model-agy-low": _catalog_model("agy", ["5h", "1w"], 55, 1.0),
            "model-agy-high": _catalog_model(
                "agy", ["5h", "1w"], 81, 1.0, effort="medium"
            ),
            "model-agy-planner": _catalog_model(
                "agy", ["5h", "1w"], 99, 1.0, dispatchable=False
            ),
            "model-codex-low": _catalog_model("codex", ["5h", "1w"], 50, 2.0),
            "model-codex-mid": _catalog_model(
                "codex", ["5h", "1w"], 82, 2.0, effort="high"
            ),
            "model-codex-top": _catalog_model(
                "codex", ["5h", "1w"], 90, 4.0, effort="max"
            ),
            "model-grok-mid": _catalog_model("grok", ["1w"], 77, 2.0),
            "model-grok-top": _catalog_model(
                "grok", ["1w"], 86, 2.0, effort="high"
            ),
            "model-claude-low": _catalog_model(
                "claude", ["5h", "1w"], 60, 1.0
            ),
            "model-claude-mid": _catalog_model(
                "claude", ["5h", "1w"], 80, 2.0
            ),
            "model-claude-top": _catalog_model(
                "claude", ["5h", "1w"], 92, 5.0
            ),
            "model-claude-planner": _catalog_model(
                "claude",
                ["5h", "1w", "1w-fable"],
                95,
                10.0,
                effort="max",
                dispatchable=False,
            ),
        },
    }
    tiering_document = {
        "schema_version": 1,
        "blend": {"input": 1, "output": 3},
        "tiers": {
            "T0": {"min_bench": 0, "max_blended_price": 5},
            "T1": {"min_bench": 60, "max_blended_price": 10},
            "T2": {"min_bench": 74, "max_blended_price": 25},
            "T3": {"min_bench": 86, "max_blended_price": None},
        },
        "orchestrator_excluded_vendors": ["agy"],
    }
    (catalog_dir / "models.yaml").write_text(
        yaml.safe_dump(models_document, sort_keys=False),
        encoding="utf-8",
    )
    (catalog_dir / "tiering.yaml").write_text(
        yaml.safe_dump(tiering_document, sort_keys=False),
        encoding="utf-8",
    )

    catalog = load_catalog(catalog_dir)
    return load_policy(policy_dir, catalog_dir=catalog_dir), catalog


@pytest.fixture
def policy_catalog(tmp_path: Path) -> tuple[Policy, Catalog]:
    return _build_synthetic_policy(tmp_path)


@pytest.fixture
def policy(policy_catalog: tuple[Policy, Catalog]) -> Policy:
    return policy_catalog[0]


def _default_signals(
    overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    pools = {
        pool: {
            "usable": True,
            "score": score,
            "level": "ok",
            "cooling": False,
            "seconds_until_reset": 7200,
            "stale": False,
        }
        for pool, score in {
            "claude": 90.0,
            "codex": 85.0,
            "grok": 80.0,
            "agy": 75.0,
            "agy-3p": 70.0,
        }.items()
    }
    if overrides:
        for pool, values in overrides.items():
            if pool in pools:
                pools[pool].update(values)
            else:
                pools[pool] = values
    return pools


def test_tier_selection_highest_score(policy: Policy) -> None:
    signals = _default_signals(
        {
            "agy": {"score": 95.0},
            "grok": {"score": 90.0},
            "codex": {"score": 80.0},
            "claude": {"score": 70.0},
            "agy-3p": {"score": 60.0},
        }
    )

    for tier in ("T0", "T1", "T2"):
        rec = recommend(policy, signals, tier=tier, role="implement")
        assert rec.recommended is not None
        assert rec.recommended["model"] == "model-agy-high"
        assert rec.recommended["vendor"] == "agy"
        assert "tier_capable" in rec.reason_codes
        assert "quota_healthy" in rec.reason_codes
        assert "provider_healthy" in rec.reason_codes

    rec_t3 = recommend(policy, signals, tier="T3", role="implement")
    assert rec_t3.recommended is not None
    assert rec_t3.recommended["model"] == "model-grok-top"
    assert rec_t3.recommended["vendor"] == "grok"


def test_bench_breaks_score_tie(policy: Policy) -> None:
    signals = _default_signals(
        {
            "claude": {"score": 90.0},
            "codex": {"score": 90.0},
            "grok": {"score": 50.0},
            "agy": {"score": 50.0},
        }
    )

    rec = recommend(policy, signals, tier="T3", role="implement")
    assert rec.recommended is not None
    assert rec.recommended["model"] == "model-claude-top"
    assert rec.recommended["vendor"] == "claude"


def test_blended_price_breaks_bench_and_score_tie(policy: Policy) -> None:
    synthetic_models = dict(policy.models)
    synthetic_models["model-cheap"] = Model(
        pool="claude",
        slots=["5h", "1w"],
        bench=99,
        blended_price=3.0,
        effort="medium",
        min_tier=0,
        max_tier=2,
        tiers=["T0", "T1", "T2"],
        role="subagent",
        reviewer=False,
        orchestrator=False,
        dispatchable=True,
    )
    synthetic_models["model-expensive"] = Model(
        pool="claude",
        slots=["5h", "1w"],
        bench=99,
        blended_price=7.0,
        effort="high",
        min_tier=0,
        max_tier=2,
        tiers=["T0", "T1", "T2"],
        role="subagent",
        reviewer=False,
        orchestrator=False,
        dispatchable=True,
    )
    custom_policy = policy.model_copy(update={"models": synthetic_models})

    rec = recommend(
        custom_policy,
        _default_signals({"claude": {"score": 99.0}}),
        tier="T2",
        role="implement",
        available_vendors=["claude"],
    )

    assert rec.recommended is not None
    assert rec.recommended["model"] == "model-cheap"
    assert "model-expensive" in [alternative["model"] for alternative in rec.alternatives]


def test_cross_vendor_review_exclusion(policy: Policy) -> None:
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
    }

    rec = recommend(policy, signals, tier="T2", role="implement")
    assert rec.recommended is None
    assert rec.alternatives == []
    assert "no_eligible_candidate" in rec.reason_codes
    assert "fallback_static" in rec.reason_codes
    assert rec.wait_seconds == 1800


def test_planner_dispatchable_implement_vs_orchestrate(policy: Policy) -> None:
    signals = _default_signals({"claude": {"score": 100.0}})

    for tier in ("T0", "T1", "T2", "T3"):
        rec = recommend(policy, signals, tier=tier, role="implement")
        assert rec.recommended is not None
        assert rec.recommended["model"] != "model-claude-planner"
        assert all(
            alt["model"] != "model-claude-planner" for alt in rec.alternatives
        )

    rec_orchestrate = recommend(
        policy,
        signals,
        tier="T3",
        role="orchestrate",
    )
    assert rec_orchestrate.recommended is not None
    assert rec_orchestrate.recommended["model"] == "model-claude-planner"
    assert rec_orchestrate.recommended["vendor"] == "claude"


def test_orchestrate_uses_derived_orchestrator_flag(policy: Policy) -> None:
    signals = _default_signals({"agy": {"score": 100.0}})
    rec = recommend(policy, signals, tier="T3", role="orchestrate")

    returned = [rec.recommended, *rec.alternatives]
    targets = [target for target in returned if target is not None]
    assert targets
    assert all(policy.models[str(target["model"])].orchestrator for target in targets)
    assert all(target["vendor"] != "agy" for target in targets)
    assert "model-agy-planner" not in {
        target["model"] for target in targets
    }


def test_available_vendors_filtering(policy: Policy) -> None:
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
    assert all(alt["vendor"] in {"claude", "codex"} for alt in rec.alternatives)
    assert "vendor_filtered" in rec.reason_codes


def test_model_usability_skips_otherwise_recommended_model(policy: Policy) -> None:
    signals = _default_signals(
        {
            "codex": {"score": 99.0},
            "claude": {"score": 85.0},
            "grok": {"score": 80.0},
        }
    )
    normal = recommend(policy, signals, tier="T3", role="implement")
    filtered = recommend(
        policy,
        signals,
        tier="T3",
        role="implement",
        model_usable={"model-codex-top": False},
    )

    assert normal.recommended is not None
    assert normal.recommended["model"] == "model-codex-top"
    assert filtered.recommended is not None
    assert filtered.recommended["model"] == "model-claude-top"
    assert all(
        alternative["model"] != "model-codex-top"
        for alternative in filtered.alternatives
    )


def _project_pools_to_signals(
    pools_out: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    signals: dict[str, dict[str, Any]] = {}
    for pool_id, pool_data in pools_out.items():
        binding_slot = pool_data.get("binding_slot")
        window = (
            pool_data.get("windows", {}).get(binding_slot)
            if binding_slot
            else None
        )
        signals[pool_id] = {
            "usable": pool_data["usable"],
            "score": pool_data["score"],
            "level": pool_data["level"],
            "cooling": pool_data.get("cooldown", {}).get("cooling", False),
            "seconds_until_reset": (
                window.get("seconds_until_reset") if window else None
            ),
            "stale": pool_data["stale"],
        }
    return signals


def _win(
    key: str,
    used: float,
    reset_in_s: int,
    limit: int | None = None,
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
    provider: ProviderId,
    windows: list[UsageWindow],
    *,
    age_s: int = 60,
    status: SnapshotStatus = SnapshotStatus.OK,
) -> AccountSnapshot:
    return AccountSnapshot(
        provider=provider,
        display_name=provider.value,
        status=status,
        windows=windows,
        fetched_at=NOW - timedelta(seconds=age_s),
    )


def test_parity_with_routing_view(
    policy_catalog: tuple[Policy, Catalog],
) -> None:
    policy, catalog = policy_catalog
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

    payload = build_routing_payload(snapshots, now=NOW, catalog=catalog)
    signals = _project_pools_to_signals(payload["pools"])

    for tier in ("T0", "T1", "T2", "T3"):
        rec = recommend(policy, signals, tier=tier, role="implement")
        assert rec.recommended is not None
        assert rec.recommended["model"] == payload["tiers"][tier]["recommended"]

    rec_review = recommend(policy, signals, tier="T2", role="review")
    assert rec_review.recommended is not None
    assert rec_review.recommended["model"] == payload["tiers"]["review"]["recommended"]

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
    error_payload = build_routing_payload(
        error_snapshots,
        now=NOW,
        catalog=catalog,
    )
    error_signals = _project_pools_to_signals(error_payload["pools"])

    rec_error = recommend(policy, error_signals, tier="T2", role="implement")
    assert error_payload["tiers"]["T2"]["recommended"] is not None
    assert rec_error.recommended is None
    assert "no_eligible_candidate" in rec_error.reason_codes
    assert "fallback_static" in rec_error.reason_codes


def test_missing_pool_handling(policy: Policy) -> None:
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
    assert rec.recommended["model"] == "model-codex-top"
    assert rec.recommended["vendor"] == "codex"


def test_stale_signals_reason_code(policy: Policy) -> None:
    signals = _default_signals({"claude": {"score": 99.0, "stale": True}})
    rec = recommend(policy, signals, tier="T3", role="implement")
    assert rec.recommended is not None
    assert rec.recommended["model"] == "model-claude-top"
    assert "stale_signals" in rec.reason_codes


def test_signals_stale_flag_adds_stale_signals_reason_code(policy: Policy) -> None:
    signals = _default_signals({"claude": {"score": 99.0, "stale": False}})
    rec = recommend(
        policy,
        signals,
        tier="T3",
        role="implement",
        signals_stale=True,
    )
    assert rec.recommended is not None
    assert rec.recommended["model"] == "model-claude-top"
    assert "stale_signals" in rec.reason_codes


def test_reason_codes_strict_order(policy: Policy) -> None:
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


def test_response_includes_effort_with_minimal_surface(policy: Policy) -> None:
    rec = recommend(policy, _default_signals(), tier="T2", role="implement")
    assert rec.recommended is not None
    assert set(rec.recommended) == {"vendor", "model", "effort"}
    assert "effort" in rec.recommended
    for alternative in rec.alternatives:
        assert set(alternative) == {"vendor", "model", "effort"}
        assert "effort" in alternative
