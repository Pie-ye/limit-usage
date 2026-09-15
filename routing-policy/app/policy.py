"""Load and validate the version-controlled routing policy.

The policy is split by concern so model inventory, tier boundaries, and routing
roles can be reviewed independently.  This module validates each source before
assembling one typed object, keeping configuration errors close to the filename
and key that caused them while preserving the legacy candidate ordering.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class _PolicyModel(BaseModel):
    """Reject coercion and unknown keys so policy mistakes fail at startup."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Pool(_PolicyModel):
    """Describe one vendor quota pool and its normalized slot mappings."""

    provider: str
    display_name: str
    slots: dict[str, str]


class Model(_PolicyModel):
    """Describe the static routing capabilities of one model."""

    pool: str
    slots: list[str]
    max_tier: int = Field(ge=0, le=3)
    bench: int
    cost_rank: int
    role: Literal["orchestrator", "subagent"]
    reviewer: bool = False
    dispatchable: bool = True


class Tier(_PolicyModel):
    """Map a dispatch tier to its level and orchestration complexity range."""

    level: int = Field(ge=0, le=3)
    complexity: list[int] = Field(min_length=2, max_length=2)


class Policy(_PolicyModel):
    """Provide the merged policy and the legacy-compatible candidate query."""

    schema_version: Literal[1]
    policy_version: str
    pools: dict[str, Pool]
    models: dict[str, Model]
    tiers: dict[str, Tier]
    review_cross_vendor: bool
    ranking: list[str]
    orchestrator_excluded_vendors: list[str]

    def tier_candidates(self, tier_id: str) -> list[str]:
        """Return eligible model ids in the legacy cost-rank order."""

        if tier_id == "review":
            candidates = [
                model_id
                for model_id, specification in self.models.items()
                if specification.reviewer
            ]
        else:
            level = self.tiers[tier_id].level
            candidates = [
                model_id
                for model_id, specification in self.models.items()
                if specification.max_tier >= level and specification.dispatchable
            ]
        return sorted(
            candidates,
            key=lambda model_id: self.models[model_id].cost_rank,
        )


class _ModelsDocument(_PolicyModel):
    schema_version: Literal[1]
    pools: dict[str, Pool]
    models: dict[str, Model]


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


class _OrchestratorPolicy(_PolicyModel):
    excluded_vendors: list[str]


class _RolesDocument(_PolicyModel):
    schema_version: Literal[1]
    policy: _RolePolicy
    ranking: list[str]
    orchestrator: _OrchestratorPolicy


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


def _validate_model_references(models_document: _ModelsDocument) -> None:
    for model_id, specification in models_document.models.items():
        pool = models_document.pools.get(specification.pool)
        if pool is None:
            raise ValueError(
                f"models.yaml: models.{model_id}.pool: "
                f"unknown pool {specification.pool!r}"
            )

        for slot in specification.slots:
            if slot not in pool.slots:
                raise ValueError(
                    f"models.yaml: models.{model_id}.slots: slot {slot!r} "
                    f"is not defined by pool {specification.pool!r}"
                )


def load_policy(policy_dir: Path | str) -> Policy:
    """Read the three YAML documents and return one validated policy object."""

    directory = Path(policy_dir)
    models_path = directory / "models.yaml"
    tiers_path = directory / "tiers.yaml"
    roles_path = directory / "roles.yaml"

    models_document = _validate_document(
        _ModelsDocument,
        _read_yaml(models_path),
        models_path,
    )
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
    _validate_model_references(models_document)

    return Policy(
        schema_version=models_document.schema_version,
        policy_version=tiers_document.policy_version,
        pools=models_document.pools,
        models=models_document.models,
        tiers=tiers_document.tiers,
        review_cross_vendor=roles_document.policy.review.cross_vendor,
        ranking=roles_document.ranking,
        orchestrator_excluded_vendors=(
            roles_document.orchestrator.excluded_vendors
        ),
    )
