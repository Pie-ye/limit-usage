"""Load and assemble the version-controlled routing policy.

Models and their derived capabilities come from the shared catalog. This module
only validates tier boundaries and role rules before assembling one typed policy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from catalog.tiering import load_catalog


class _PolicyModel(BaseModel):
    """Reject coercion and unknown keys so policy mistakes fail at startup."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Pool(_PolicyModel):
    """Describe one vendor quota pool and its normalized slot mappings."""

    provider: str
    display_name: str
    slots: dict[str, str]


class Model(_PolicyModel):
    """Describe the catalog-derived routing capabilities of one model."""

    pool: str
    slots: list[str]
    bench: int
    blended_price: float
    effort: str | None = None
    min_tier: int | None = Field(default=None, ge=0, le=3)
    max_tier: int = Field(ge=0, le=3)
    tiers: list[str]
    role: Literal["orchestrator", "subagent"]
    reviewer: bool = False
    orchestrator: bool = False
    dispatchable: bool = True


class Tier(_PolicyModel):
    """Map a dispatch tier to its level and orchestration complexity range."""

    level: int = Field(ge=0, le=3)
    complexity: list[int] = Field(min_length=2, max_length=2)


class Policy(_PolicyModel):
    """Provide the assembled routing policy and candidate query."""

    schema_version: Literal[1]
    policy_version: str
    catalog_version: str
    pools: dict[str, Pool]
    models: dict[str, Model]
    tiers: dict[str, Tier]
    review_cross_vendor: bool
    # Descriptive documentation of candidate ranking intent (availability -> quota -> capability -> cost) in v1.
    # Sorting order is hardcoded in engine.py for behavioral parity with routing_view;
    # modifying this list does not alter candidate selection without engine.py changes.
    ranking: list[str] = Field(
        description=(
            "Descriptive documentation of candidate ranking intent in v1. "
            "Engine candidate sorting is hardcoded to guarantee behavioral parity "
            "with legacy routing_view; changing the evaluation sequence requires modifying engine.py."
        )
    )

    def tier_candidates(self, tier_id: str) -> list[str]:
        """Return eligible model ids ordered by blended price and model id."""

        if tier_id == "review":
            candidates = [
                model_id
                for model_id, specification in self.models.items()
                if specification.reviewer
            ]
        else:
            candidates = [
                model_id
                for model_id, specification in self.models.items()
                if specification.dispatchable and tier_id in specification.tiers
            ]
        return sorted(
            candidates,
            key=lambda model_id: (
                self.models[model_id].blended_price,
                model_id,
            ),
        )


class _ReviewTier(_PolicyModel):
    role: Literal["review"]


class _TiersDocument(_PolicyModel):
    schema_version: Literal[1]
    policy_version: str
    tiers: dict[str, Tier]
    review: _ReviewTier


class _ReviewPolicy(_PolicyModel):
    cross_vendor: bool


class _RolePolicy(_PolicyModel):
    review: _ReviewPolicy


class _RolesDocument(_PolicyModel):
    schema_version: Literal[1]
    policy: _RolePolicy
    ranking: list[str]


Document = TypeVar("Document", bound=_PolicyModel)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as source:
            document = yaml.safe_load(source)
    except OSError as error:
        raise ValueError(f"{path.name}: <document>: cannot read file: {error}") from error
    except yaml.YAMLError as error:
        raise ValueError(f"{path.name}: <document>: invalid YAML: {error}") from error

    if not isinstance(document, dict):
        raise ValueError(f"{path.name}: <document>: expected a mapping")
    return document


def _validate_document(
    model_type: type[Document],
    document: dict[str, Any],
    path: Path,
) -> Document:
    try:
        return model_type.model_validate(document)
    except ValidationError as error:
        details = []
        for failure in error.errors(include_url=False):
            key = ".".join(str(part) for part in failure["loc"]) or "<document>"
            details.append(f"{path.name}: {key}: {failure['msg']}")
        raise ValueError("; ".join(details)) from error


def load_policy(
    policy_dir: Path | str,
    catalog_dir: Path | str | None = None,
) -> Policy:
    """Read policy rules and the shared catalog into one validated object."""

    directory = Path(policy_dir)
    tiers_path = directory / "tiers.yaml"
    roles_path = directory / "roles.yaml"

    tiers_document = _validate_document(
        _TiersDocument,
        _read_yaml(tiers_path),
        tiers_path,
    )
    roles_document = _validate_document(
        _RolesDocument,
        _read_yaml(roles_path),
        roles_path,
    )
    catalog = load_catalog(catalog_dir)

    if set(tiers_document.tiers) != set(catalog.tier_ids):
        raise ValueError(
            "tiers.yaml: tiers: must define exactly T0, T1, T2, T3"
        )

    pools = {
        pool_id: Pool.model_validate(specification)
        for pool_id, specification in catalog.pools.items()
    }
    models = {
        model_id: Model(
            pool=derived.pool,
            slots=list(derived.slots),
            bench=derived.bench,
            blended_price=derived.blended_price,
            effort=derived.effort,
            min_tier=(
                int(derived.min_tier[1:])
                if derived.min_tier is not None
                else None
            ),
            max_tier=int(derived.max_tier[1:]),
            tiers=list(derived.tiers),
            role=derived.role,
            reviewer=derived.reviewer,
            orchestrator=derived.orchestrator,
            dispatchable=derived.dispatchable,
        )
        for model_id, derived in catalog.models.items()
    }

    return Policy(
        schema_version=tiers_document.schema_version,
        policy_version=tiers_document.policy_version,
        catalog_version=catalog.catalog_version,
        pools=pools,
        models=models,
        tiers=tiers_document.tiers,
        review_cross_vendor=roles_document.policy.review.cross_vendor,
        ranking=roles_document.ranking,
    )
