from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

TIER_IDS: tuple[str, ...] = ("T0", "T1", "T2", "T3")


@dataclass(frozen=True)
class Derived:
    model: str
    vendor: str
    pool: str
    slots: tuple[str, ...]
    bench: int
    blended_price: float
    effort: str | None
    dispatchable: bool
    min_tier: str | None
    max_tier: str
    tiers: tuple[str, ...]
    reviewer: bool
    orchestrator: bool
    role: str


@dataclass(frozen=True)
class Catalog:
    catalog_version: str
    pools: Mapping[str, Mapping[str, Any]]
    models: Mapping[str, Derived]
    tier_ids: tuple[str, ...]
    blend: Mapping[str, float]
    thresholds: Mapping[str, Mapping[str, Any]]


def _read_yaml(file_path: Path, filename: str) -> dict[str, Any]:
    if not file_path.is_file():
        raise ValueError(f"{filename}: <root>: file does not exist: {file_path}")
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"{filename}: <root>: failed to read file: {e}")
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError(f"{filename}: <root>: YAML parsing error: {e}")
    if not isinstance(data, dict):
        raise ValueError(f"{filename}: <root>: content must be a mapping")
    return data


def _validate_tiering(data: dict[str, Any]) -> None:
    allowed_root_keys = {"schema_version", "blend", "tiers", "orchestrator_excluded_vendors"}
    for k in data:
        if k not in allowed_root_keys:
            raise ValueError(f"tiering.yaml: {k}: unknown key")
    for req in ("schema_version", "blend", "tiers", "orchestrator_excluded_vendors"):
        if req not in data:
            raise ValueError(f"tiering.yaml: {req}: missing required field")

    schema_version = data["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        raise ValueError(f"tiering.yaml: schema_version: schema_version must be 1")

    blend = data["blend"]
    if not isinstance(blend, dict):
        raise ValueError(f"tiering.yaml: blend: must be a mapping")
    for k in blend:
        if k not in {"input", "output"}:
            raise ValueError(f"tiering.yaml: blend.{k}: unknown key")
    for req in ("input", "output"):
        if req not in blend:
            raise ValueError(f"tiering.yaml: blend.{req}: missing required field")
        val = blend[req]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
            raise ValueError(f"tiering.yaml: blend.{req}: must be a number > 0")

    tiers = data["tiers"]
    if not isinstance(tiers, dict):
        raise ValueError(f"tiering.yaml: tiers: must be a mapping")
    for k in tiers:
        if k not in TIER_IDS:
            raise ValueError(f"tiering.yaml: tiers.{k}: unknown tier")
    for t in TIER_IDS:
        if t not in tiers:
            raise ValueError(f"tiering.yaml: tiers.{t}: missing required tier")

    prev_bench = -1
    saw_null_price = False
    prev_price: float | None = None

    for t in TIER_IDS:
        t_data = tiers[t]
        if not isinstance(t_data, dict):
            raise ValueError(f"tiering.yaml: tiers.{t}: must be a mapping")
        for k in t_data:
            if k not in {"min_bench", "max_blended_price"}:
                raise ValueError(f"tiering.yaml: tiers.{t}.{k}: unknown key")
        for req in ("min_bench", "max_blended_price"):
            if req not in t_data:
                raise ValueError(f"tiering.yaml: tiers.{t}.{req}: missing required field")

        min_bench = t_data["min_bench"]
        if isinstance(min_bench, bool) or not isinstance(min_bench, int) or min_bench < 0 or min_bench > 100:
            raise ValueError(f"tiering.yaml: tiers.{t}.min_bench: must be an int between 0 and 100")

        if t == "T0" and min_bench != 0:
            raise ValueError(f"tiering.yaml: tiers.T0.min_bench: must be 0")

        if min_bench < prev_bench:
            raise ValueError(f"tiering.yaml: tiers.{t}.min_bench: must be non-decreasing")
        prev_bench = min_bench

        max_price = t_data["max_blended_price"]
        if max_price is not None:
            if isinstance(max_price, bool) or not isinstance(max_price, (int, float)) or max_price < 0:
                raise ValueError(f"tiering.yaml: tiers.{t}.max_blended_price: must be a non-negative number or null")
            if saw_null_price:
                raise ValueError(f"tiering.yaml: tiers.{t}.max_blended_price: cannot specify a price limit after null")
            if prev_price is not None and max_price < prev_price:
                raise ValueError(f"tiering.yaml: tiers.{t}.max_blended_price: must be non-decreasing")
            prev_price = float(max_price)
        else:
            saw_null_price = True

    oev = data["orchestrator_excluded_vendors"]
    if not isinstance(oev, list) or any(not isinstance(x, str) for x in oev):
        raise ValueError(f"tiering.yaml: orchestrator_excluded_vendors: must be a list of strings")


def _validate_models(data: dict[str, Any]) -> None:
    allowed_root_keys = {"schema_version", "catalog_version", "pools", "models"}
    for k in data:
        if k not in allowed_root_keys:
            raise ValueError(f"models.yaml: {k}: unknown key")
    for req in ("schema_version", "catalog_version", "pools", "models"):
        if req not in data:
            raise ValueError(f"models.yaml: {req}: missing required field")

    schema_version = data["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 2:
        raise ValueError(f"models.yaml: schema_version: schema_version must be 2")

    catalog_version = data["catalog_version"]
    if not isinstance(catalog_version, str) or not catalog_version.strip():
        raise ValueError(f"models.yaml: catalog_version: must be a non-empty string")

    pools = data["pools"]
    if not isinstance(pools, dict):
        raise ValueError(f"models.yaml: pools: must be a mapping")

    allowed_pool_keys = {"provider", "display_name", "slots"}
    for pool_id, pool in pools.items():
        if not isinstance(pool, dict):
            raise ValueError(f"models.yaml: pools.{pool_id}: must be a mapping")
        for k in pool:
            if k not in allowed_pool_keys:
                raise ValueError(f"models.yaml: pools.{pool_id}.{k}: unknown key")
        for req in ("provider", "display_name", "slots"):
            if req not in pool:
                raise ValueError(f"models.yaml: pools.{pool_id}.{req}: missing required field")
        if not isinstance(pool["provider"], str):
            raise ValueError(f"models.yaml: pools.{pool_id}.provider: must be a string")
        if not isinstance(pool["display_name"], str):
            raise ValueError(f"models.yaml: pools.{pool_id}.display_name: must be a string")
        slots = pool["slots"]
        if not isinstance(slots, dict) or not slots or not all(isinstance(k, str) and isinstance(v, str) for k, v in slots.items()):
            raise ValueError(f"models.yaml: pools.{pool_id}.slots: must be a mapping of str to str")

    models = data["models"]
    if not isinstance(models, dict):
        raise ValueError(f"models.yaml: models: must be a mapping")

    allowed_model_keys = {"pool", "slots", "bench", "bench_note", "price", "effort", "dispatchable"}
    required_model_keys = ("pool", "slots", "bench", "price")

    for model_id, model in models.items():
        if not isinstance(model, dict):
            raise ValueError(f"models.yaml: models.{model_id}: must be a mapping")
        for k in model:
            if k not in allowed_model_keys:
                raise ValueError(f"models.yaml: models.{model_id}.{k}: unknown key")
        for req in required_model_keys:
            if req not in model:
                raise ValueError(f"models.yaml: models.{model_id}.{req}: missing required field")

        pool_name = model["pool"]
        if not isinstance(pool_name, str) or pool_name not in pools:
            raise ValueError(f"models.yaml: models.{model_id}.pool: unknown pool '{pool_name}'")

        slots = model["slots"]
        if not isinstance(slots, list) or len(slots) == 0:
            raise ValueError(f"models.yaml: models.{model_id}.slots: must be a non-empty list")
        for slot in slots:
            if not isinstance(slot, str) or slot not in pools[pool_name]["slots"]:
                raise ValueError(f"models.yaml: models.{model_id}.slots: slot '{slot}' not found in pool '{pool_name}'")

        bench = model["bench"]
        if isinstance(bench, bool) or not isinstance(bench, int) or bench < 0 or bench > 100:
            raise ValueError(f"models.yaml: models.{model_id}.bench: must be an int between 0 and 100")

        if "bench_note" in model:
            if not isinstance(model["bench_note"], str):
                raise ValueError(f"models.yaml: models.{model_id}.bench_note: must be a string")

        if "effort" in model:
            effort = model["effort"]
            if effort is not None and not isinstance(effort, str):
                raise ValueError(f"models.yaml: models.{model_id}.effort: must be a string or null")

        if "dispatchable" in model:
            dispatchable = model["dispatchable"]
            if not isinstance(dispatchable, bool):
                raise ValueError(f"models.yaml: models.{model_id}.dispatchable: must be a bool")

        price = model["price"]
        if not isinstance(price, dict):
            raise ValueError(f"models.yaml: models.{model_id}.price: must be a mapping")
        allowed_price_keys = {"input", "output", "as_of", "source", "proxy_of"}
        for pk in price:
            if pk not in allowed_price_keys:
                raise ValueError(f"models.yaml: models.{model_id}.price.{pk}: unknown key")
        for req in ("input", "output", "as_of", "source"):
            if req not in price:
                raise ValueError(f"models.yaml: models.{model_id}.price.{req}: missing required field")

        inp = price["input"]
        if isinstance(inp, bool) or not isinstance(inp, (int, float)) or inp < 0:
            raise ValueError(f"models.yaml: models.{model_id}.price.input: must be a number >= 0")

        outp = price["output"]
        if isinstance(outp, bool) or not isinstance(outp, (int, float)) or outp < 0:
            raise ValueError(f"models.yaml: models.{model_id}.price.output: must be a number >= 0")

        as_of = price["as_of"]
        if not isinstance(as_of, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", as_of):
            raise ValueError(f"models.yaml: models.{model_id}.price.as_of: must be a string in YYYY-MM-DD format")
        try:
            datetime.strptime(as_of, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"models.yaml: models.{model_id}.price.as_of: must be a valid date in YYYY-MM-DD format")

        source = price["source"]
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"models.yaml: models.{model_id}.price.source: must be a non-empty string")

    for model_id, model in models.items():
        price = model.get("price")
        if isinstance(price, dict) and "proxy_of" in price:
            proxy_of = price["proxy_of"]
            if not isinstance(proxy_of, str) or proxy_of not in models or proxy_of == model_id:
                raise ValueError(f"models.yaml: models.{model_id}.price.proxy_of: unknown model '{proxy_of}'")


def load_catalog(directory: str | Path | None = None) -> Catalog:
    if directory is None:
        env_dir = os.environ.get("CATALOG_DIR")
        if env_dir:
            dir_path = Path(env_dir)
        else:
            dir_path = Path(__file__).resolve().parent
    else:
        dir_path = Path(directory)

    models_data = _read_yaml(dir_path / "models.yaml", "models.yaml")
    tiering_data = _read_yaml(dir_path / "tiering.yaml", "tiering.yaml")

    _validate_tiering(tiering_data)
    _validate_models(models_data)

    blend_clean = {
        "input": float(tiering_data["blend"]["input"]),
        "output": float(tiering_data["blend"]["output"]),
    }
    blend_in = blend_clean["input"]
    blend_out = blend_clean["output"]
    blend_total = blend_in + blend_out

    thresholds: dict[str, dict[str, Any]] = {}
    for t in TIER_IDS:
        mb = int(tiering_data["tiers"][t]["min_bench"])
        mp = tiering_data["tiers"][t]["max_blended_price"]
        thresholds[t] = {
            "min_bench": mb,
            "max_blended_price": float(mp) if mp is not None else None,
        }

    excluded_vendors = set(tiering_data["orchestrator_excluded_vendors"])

    derived_models: dict[str, Derived] = {}
    for model_id, m_raw in models_data["models"].items():
        pool = m_raw["pool"]
        vendor = pool.split("-", 1)[0]
        slots = tuple(m_raw["slots"])
        bench = int(m_raw["bench"])
        price_in = float(m_raw["price"]["input"])
        price_out = float(m_raw["price"]["output"])
        blended_price = round((blend_in * price_in + blend_out * price_out) / blend_total, 4)

        effort = m_raw.get("effort", None)
        dispatchable = bool(m_raw.get("dispatchable", True))

        # max_tier = highest tier whose min_bench <= bench
        max_tier = "T0"
        for t in TIER_IDS:
            if bench >= thresholds[t]["min_bench"]:
                max_tier = t

        # min_tier = lowest tier whose max_blended_price is None or blended_price <= max_blended_price
        min_tier: str | None = None
        for t in TIER_IDS:
            limit = thresholds[t]["max_blended_price"]
            if limit is None or blended_price <= limit:
                min_tier = t
                break

        # tiers: tuple between min_tier and max_tier (inclusive)
        if min_tier is None:
            tiers: tuple[str, ...] = ()
        else:
            min_idx = TIER_IDS.index(min_tier)
            max_idx = TIER_IDS.index(max_tier)
            if min_idx > max_idx:
                tiers = ()
            else:
                tiers = TIER_IDS[min_idx : max_idx + 1]

        reviewer = dispatchable and ("T3" in tiers)
        orchestrator = (max_tier == "T3") and (vendor not in excluded_vendors)
        role = "orchestrator" if orchestrator else "subagent"

        derived_models[model_id] = Derived(
            model=model_id,
            vendor=vendor,
            pool=pool,
            slots=slots,
            bench=bench,
            blended_price=blended_price,
            effort=effort,
            dispatchable=dispatchable,
            min_tier=min_tier,
            max_tier=max_tier,
            tiers=tiers,
            reviewer=reviewer,
            orchestrator=orchestrator,
            role=role,
        )

    return Catalog(
        catalog_version=str(models_data["catalog_version"]),
        pools=models_data["pools"],
        models=derived_models,
        tier_ids=TIER_IDS,
        blend=blend_clean,
        thresholds=thresholds,
    )


def tier_candidates(catalog: Catalog, tier: str) -> list[str]:
    if tier == "review":
        candidates = [m for m, d in catalog.models.items() if d.reviewer]
    elif tier in catalog.tier_ids:
        candidates = [m for m, d in catalog.models.items() if d.dispatchable and tier in d.tiers]
    else:
        raise ValueError(f"unknown tier: {tier}")

    candidates.sort(key=lambda m: (catalog.models[m].blended_price, m))
    return candidates
