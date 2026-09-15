"""Prove the declarative policy remains identical to the legacy constants.

The parity assertions protect this extraction from silently changing dispatch
behavior, while malformed copies exercise startup failures without touching the
version-controlled policy files or requiring any network access.
"""

from __future__ import annotations

import shutil
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

ROUTING_POLICY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROUTING_POLICY_ROOT))

from app.policy import Policy, load_policy  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# ``app.policy`` and the legacy ``app.services`` live in separate source roots
# during the extraction. Extending the already-imported package lets the parity
# test exercise both without changing either application's packaging contract.
import app as routing_policy_app  # noqa: E402

LEGACY_APP_PATH = Path(__file__).resolve().parents[2] / "app"
if str(LEGACY_APP_PATH) not in routing_policy_app.__path__:
    routing_policy_app.__path__.append(str(LEGACY_APP_PATH))

# The legacy package initializer imports the poller and database even though
# parity only needs the pure routing module. A package shim keeps this focused
# test independent of optional SQLite support in the Python runtime.
legacy_services = types.ModuleType("app.services")
legacy_services.__path__ = [str(LEGACY_APP_PATH / "services")]
sys.modules["app.services"] = legacy_services

from app.services.routing_view import (  # noqa: E402
    MODELS,
    POOLS,
    TIER_LEVELS,
    tier_candidates,
)

POLICY_DIR = ROUTING_POLICY_ROOT / "policy"


@pytest.fixture
def policy() -> Policy:
    return load_policy(POLICY_DIR)


@pytest.fixture
def mutable_policy_dir(tmp_path: Path) -> Path:
    target = tmp_path / "policy"
    shutil.copytree(POLICY_DIR, target)
    return target


def _change_document(
    policy_dir: Path,
    filename: str,
    change: Callable[[dict[str, Any]], None],
) -> None:
    path = policy_dir / filename
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    change(document)
    path.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _assert_error_has(error: pytest.ExceptionInfo[ValueError], *parts: str) -> None:
    message = str(error.value)
    for part in parts:
        assert part in message


def test_policy_metadata(policy: Policy) -> None:
    assert policy.schema_version == 1
    assert policy.policy_version == "2026-09-15.1"
    assert policy.review_cross_vendor is True
    assert policy.ranking == ["availability", "quota", "capability", "cost"]
    assert policy.orchestrator_excluded_vendors == ["agy"]


def test_policy_matches_legacy_routing_constants(policy: Policy) -> None:
    assert set(policy.pools) == set(POOLS)
    for pool_id, legacy_pool in POOLS.items():
        loaded_pool = policy.pools[pool_id]
        assert loaded_pool.provider == legacy_pool["provider"].value
        assert loaded_pool.display_name == legacy_pool["display_name"]
        assert loaded_pool.slots == legacy_pool["slots"]

    assert set(policy.models) == set(MODELS)
    for model_id, legacy_model in MODELS.items():
        loaded_model = policy.models[model_id]
        assert loaded_model.pool == legacy_model["pool"]
        assert loaded_model.slots == legacy_model["slots"]
        assert loaded_model.max_tier == legacy_model["max_tier"]
        assert loaded_model.bench == legacy_model["bench"]
        assert loaded_model.cost_rank == legacy_model["cost_rank"]
        assert loaded_model.role == legacy_model["role"]
        assert loaded_model.reviewer == legacy_model.get("reviewer", False)
        assert loaded_model.dispatchable == legacy_model.get("dispatchable", True)

    assert {tier_id: tier.level for tier_id, tier in policy.tiers.items()} == TIER_LEVELS
    for tier_id in ("T0", "T1", "T2", "T3", "review"):
        assert policy.tier_candidates(tier_id) == tier_candidates(tier_id)


def test_rejects_unknown_pool_reference(mutable_policy_dir: Path) -> None:
    def change(document: dict[str, Any]) -> None:
        document["models"]["gpt-reserve"]["pool"] = "missing-pool"

    _change_document(mutable_policy_dir, "models.yaml", change)

    with pytest.raises(ValueError) as error:
        load_policy(mutable_policy_dir)
    _assert_error_has(error, "models.yaml", "gpt-reserve", "pool")


def test_rejects_max_tier_outside_supported_range(mutable_policy_dir: Path) -> None:
    def change(document: dict[str, Any]) -> None:
        document["models"]["gpt-reserve"]["max_tier"] = 4

    _change_document(mutable_policy_dir, "models.yaml", change)

    with pytest.raises(ValueError) as error:
        load_policy(mutable_policy_dir)
    _assert_error_has(error, "models.yaml", "gpt-reserve", "max_tier")


def test_rejects_missing_required_model_field(mutable_policy_dir: Path) -> None:
    def change(document: dict[str, Any]) -> None:
        del document["models"]["gpt-reserve"]["bench"]

    _change_document(mutable_policy_dir, "models.yaml", change)

    with pytest.raises(ValueError) as error:
        load_policy(mutable_policy_dir)
    _assert_error_has(error, "models.yaml", "gpt-reserve", "bench")


def test_rejects_unsupported_schema_version(mutable_policy_dir: Path) -> None:
    def change(document: dict[str, Any]) -> None:
        document["schema_version"] = 2

    _change_document(mutable_policy_dir, "roles.yaml", change)

    with pytest.raises(ValueError) as error:
        load_policy(mutable_policy_dir)
    _assert_error_has(error, "roles.yaml", "schema_version")


def test_rejects_model_slot_missing_from_pool(mutable_policy_dir: Path) -> None:
    def change(document: dict[str, Any]) -> None:
        document["models"]["gpt-reserve"]["slots"].append("1w-fable")

    _change_document(mutable_policy_dir, "models.yaml", change)

    with pytest.raises(ValueError) as error:
        load_policy(mutable_policy_dir)
    _assert_error_has(error, "models.yaml", "gpt-reserve", "slots", "1w-fable")
