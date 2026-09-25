"""Tests for loading routing policy rules with the shared model catalog."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.policy import Policy, load_policy

ROUTING_POLICY_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROUTING_POLICY_ROOT.parent
POLICY_DIR = ROUTING_POLICY_ROOT / "policy"


@pytest.fixture
def policy() -> Policy:
    return load_policy(POLICY_DIR)


@pytest.fixture
def synthetic_policy_and_catalog(tmp_path: Path) -> tuple[Path, Path]:
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    shutil.copy(POLICY_DIR / "tiers.yaml", policy_dir / "tiers.yaml")
    shutil.copy(POLICY_DIR / "roles.yaml", policy_dir / "roles.yaml")

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    shutil.copy(REPO_ROOT / "catalog" / "tiering.yaml", catalog_dir / "tiering.yaml")
    models = {
        "schema_version": 2,
        "catalog_version": "test-catalog.1",
        "pools": {
            "codex": {
                "provider": "codex",
                "display_name": "Test Codex",
                "slots": {"5h": "5h", "1w": "1w"},
            }
        },
        "models": {
            "model-test": {
                "pool": "codex",
                "slots": ["5h", "1w"],
                "bench": 90,
                "price": {
                    "input": 1.0,
                    "output": 2.0,
                    "as_of": "2026-09-26",
                    "source": "https://example.test/pricing",
                },
                "effort": "high",
            }
        },
    }
    (catalog_dir / "models.yaml").write_text(
        yaml.safe_dump(models, sort_keys=False),
        encoding="utf-8",
    )
    return policy_dir, catalog_dir


def _change_document(
    directory: Path,
    filename: str,
    change: Callable[[dict[str, Any]], None],
) -> None:
    path = directory / filename
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


def test_policy_metadata_and_real_catalog(policy: Policy) -> None:
    assert policy.schema_version == 1
    assert policy.policy_version == "2026-09-26.1"
    assert policy.catalog_version == "2026-09-26.1"
    assert policy.review_cross_vendor is True
    assert policy.ranking == ["availability", "quota", "capability", "cost"]
    assert policy.tier_candidates("T3") == [
        "grok-4.6",
        "gpt-5.6-sol",
        "claude-opus-5",
    ]
    assert all(
        "gpt-5.5" not in policy.tier_candidates(tier)
        for tier in ("T0", "T1", "T2", "T3")
    )
    assert policy.models["claude-fable-5-1"].dispatchable is False


def test_rejects_unknown_pool_reference(
    synthetic_policy_and_catalog: tuple[Path, Path],
) -> None:
    policy_dir, catalog_dir = synthetic_policy_and_catalog

    def change(document: dict[str, Any]) -> None:
        document["models"]["model-test"]["pool"] = "missing-pool"

    _change_document(catalog_dir, "models.yaml", change)

    with pytest.raises(ValueError) as error:
        load_policy(policy_dir, catalog_dir=catalog_dir)
    _assert_error_has(error, "models.yaml", "models.model-test.pool")


def test_rejects_bench_outside_supported_range(
    synthetic_policy_and_catalog: tuple[Path, Path],
) -> None:
    policy_dir, catalog_dir = synthetic_policy_and_catalog

    def change(document: dict[str, Any]) -> None:
        document["models"]["model-test"]["bench"] = 101

    _change_document(catalog_dir, "models.yaml", change)

    with pytest.raises(ValueError) as error:
        load_policy(policy_dir, catalog_dir=catalog_dir)
    _assert_error_has(error, "models.yaml", "models.model-test.bench")


def test_rejects_model_slot_missing_from_pool(
    synthetic_policy_and_catalog: tuple[Path, Path],
) -> None:
    policy_dir, catalog_dir = synthetic_policy_and_catalog

    def change(document: dict[str, Any]) -> None:
        document["models"]["model-test"]["slots"].append("missing-slot")

    _change_document(catalog_dir, "models.yaml", change)

    with pytest.raises(ValueError) as error:
        load_policy(policy_dir, catalog_dir=catalog_dir)
    _assert_error_has(
        error,
        "models.yaml",
        "models.model-test.slots",
        "missing-slot",
    )


def test_rejects_missing_required_model_field(
    synthetic_policy_and_catalog: tuple[Path, Path],
) -> None:
    policy_dir, catalog_dir = synthetic_policy_and_catalog

    def change(document: dict[str, Any]) -> None:
        del document["models"]["model-test"]["bench"]

    _change_document(catalog_dir, "models.yaml", change)

    with pytest.raises(ValueError) as error:
        load_policy(policy_dir, catalog_dir=catalog_dir)
    _assert_error_has(error, "models.yaml", "models.model-test.bench")


def test_rejects_unsupported_roles_schema_version(
    synthetic_policy_and_catalog: tuple[Path, Path],
) -> None:
    policy_dir, catalog_dir = synthetic_policy_and_catalog

    def change(document: dict[str, Any]) -> None:
        document["schema_version"] = 2

    _change_document(policy_dir, "roles.yaml", change)

    with pytest.raises(ValueError) as error:
        load_policy(policy_dir, catalog_dir=catalog_dir)
    _assert_error_has(error, "roles.yaml", "schema_version")


def test_rejects_policy_tier_set_that_differs_from_catalog(
    synthetic_policy_and_catalog: tuple[Path, Path],
) -> None:
    policy_dir, catalog_dir = synthetic_policy_and_catalog

    def change(document: dict[str, Any]) -> None:
        del document["tiers"]["T3"]

    _change_document(policy_dir, "tiers.yaml", change)

    with pytest.raises(ValueError) as error:
        load_policy(policy_dir, catalog_dir=catalog_dir)
    assert str(error.value) == (
        "tiers.yaml: tiers: must define exactly T0, T1, T2, T3"
    )
