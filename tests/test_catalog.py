from __future__ import annotations

from pathlib import Path
import pytest
import yaml

from catalog.tiering import Catalog, Derived, load_catalog, tier_candidates

GOLDEN_DATA = [
    {
        "model": "gemini-3.8-flash-low",
        "bench": 55,
        "blended": 3.0,
        "min": "T0",
        "max": "T0",
        "tiers": ("T0",),
        "reviewer": False,
        "orchestrator": False,
        "effort": None,
        "dispatchable": True,
        "vendor": "agy",
    },
    {
        "model": "gemini-3.8-flash-medium",
        "bench": 70,
        "blended": 3.0,
        "min": "T0",
        "max": "T1",
        "tiers": ("T0", "T1"),
        "reviewer": False,
        "orchestrator": False,
        "effort": None,
        "dispatchable": True,
        "vendor": "agy",
    },
    {
        "model": "gemini-3.8-flash-high",
        "bench": 81,
        "blended": 3.0,
        "min": "T0",
        "max": "T2",
        "tiers": ("T0", "T1", "T2"),
        "reviewer": False,
        "orchestrator": False,
        "effort": None,
        "dispatchable": True,
        "vendor": "agy",
    },
    {
        "model": "gemini-3.1-pro-low",
        "bench": 65,
        "blended": 9.5,
        "min": "T1",
        "max": "T1",
        "tiers": ("T1",),
        "reviewer": False,
        "orchestrator": False,
        "effort": None,
        "dispatchable": True,
        "vendor": "agy",
    },
    {
        "model": "gemini-3.1-pro-high",
        "bench": 74,
        "blended": 9.5,
        "min": "T1",
        "max": "T2",
        "tiers": ("T1", "T2"),
        "reviewer": False,
        "orchestrator": False,
        "effort": None,
        "dispatchable": True,
        "vendor": "agy",
    },
    {
        "model": "gpt-reserve",
        "bench": 50,
        "blended": 0.95,
        "min": "T0",
        "max": "T0",
        "tiers": ("T0",),
        "reviewer": False,
        "orchestrator": False,
        "effort": None,
        "dispatchable": True,
        "vendor": "codex",
    },
    {
        "model": "gpt-5.6-luna",
        "bench": 78,
        "blended": 0.95,
        "min": "T0",
        "max": "T2",
        "tiers": ("T0", "T1", "T2"),
        "reviewer": False,
        "orchestrator": False,
        "effort": "max",
        "dispatchable": True,
        "vendor": "codex",
    },
    {
        "model": "gpt-5.5",
        "bench": 72,
        "blended": 23.75,
        "min": "T2",
        "max": "T1",
        "tiers": (),
        "reviewer": False,
        "orchestrator": False,
        "effort": None,
        "dispatchable": True,
        "vendor": "codex",
    },
    {
        "model": "gpt-5.6-terra",
        "bench": 82,
        "blended": 9.5,
        "min": "T1",
        "max": "T2",
        "tiers": ("T1", "T2"),
        "reviewer": False,
        "orchestrator": False,
        "effort": "high",
        "dispatchable": True,
        "vendor": "codex",
    },
    {
        "model": "gpt-5.6-sol",
        "bench": 90,
        "blended": 16.0,
        "min": "T2",
        "max": "T3",
        "tiers": ("T2", "T3"),
        "reviewer": True,
        "orchestrator": True,
        "effort": "max",
        "dispatchable": True,
        "vendor": "codex",
    },
    {
        "model": "grok-4.5",
        "bench": 77,
        "blended": 5.0,
        "min": "T0",
        "max": "T2",
        "tiers": ("T0", "T1", "T2"),
        "reviewer": False,
        "orchestrator": False,
        "effort": "medium",
        "dispatchable": True,
        "vendor": "grok",
    },
    {
        "model": "grok-4.6",
        "bench": 86,
        "blended": 5.0,
        "min": "T0",
        "max": "T3",
        "tiers": ("T0", "T1", "T2", "T3"),
        "reviewer": True,
        "orchestrator": True,
        "effort": "high",
        "dispatchable": True,
        "vendor": "grok",
    },
    {
        "model": "claude-haiku-4-5-20251001",
        "bench": 60,
        "blended": 4.0,
        "min": "T0",
        "max": "T1",
        "tiers": ("T0", "T1"),
        "reviewer": False,
        "orchestrator": False,
        "effort": None,
        "dispatchable": True,
        "vendor": "claude",
    },
    {
        "model": "claude-sonnet-5",
        "bench": 80,
        "blended": 8.0,
        "min": "T1",
        "max": "T2",
        "tiers": ("T1", "T2"),
        "reviewer": False,
        "orchestrator": False,
        "effort": None,
        "dispatchable": True,
        "vendor": "claude",
    },
    {
        "model": "claude-opus-5",
        "bench": 92,
        "blended": 20.0,
        "min": "T2",
        "max": "T3",
        "tiers": ("T2", "T3"),
        "reviewer": True,
        "orchestrator": True,
        "effort": None,
        "dispatchable": True,
        "vendor": "claude",
    },
    {
        "model": "claude-fable-5-1",
        "bench": 95,
        "blended": 40.0,
        "min": "T3",
        "max": "T3",
        "tiers": ("T3",),
        "reviewer": False,
        "orchestrator": True,
        "effort": None,
        "dispatchable": False,
        "vendor": "claude",
    },
]


def test_catalog_golden_table() -> None:
    cat = load_catalog()
    expected_order = [row["model"] for row in GOLDEN_DATA]
    assert list(cat.models.keys()) == expected_order
    assert cat.tier_ids == ("T0", "T1", "T2", "T3")
    assert cat.blend == {"input": 1.0, "output": 3.0}
    assert cat.thresholds == {
        "T0": {"min_bench": 0, "max_blended_price": 5.0},
        "T1": {"min_bench": 60, "max_blended_price": 10.0},
        "T2": {"min_bench": 74, "max_blended_price": 25.0},
        "T3": {"min_bench": 86, "max_blended_price": None},
    }

    for row in GOLDEN_DATA:
        m = cat.models[row["model"]]
        assert m.bench == row["bench"], f"{row['model']} bench mismatch"
        assert m.blended_price == pytest.approx(row["blended"], abs=1e-4), f"{row['model']} blended_price mismatch"
        assert m.min_tier == row["min"], f"{row['model']} min_tier mismatch"
        assert m.max_tier == row["max"], f"{row['model']} max_tier mismatch"
        assert m.tiers == row["tiers"], f"{row['model']} tiers mismatch"
        assert m.reviewer == row["reviewer"], f"{row['model']} reviewer mismatch"
        assert m.orchestrator == row["orchestrator"], f"{row['model']} orchestrator mismatch"
        assert m.role == ("orchestrator" if row["orchestrator"] else "subagent"), f"{row['model']} role mismatch"
        assert m.effort == row["effort"], f"{row['model']} effort mismatch"
        assert m.dispatchable == row["dispatchable"], f"{row['model']} dispatchable mismatch"
        assert m.vendor == row["vendor"], f"{row['model']} vendor mismatch"


def test_tier_candidates_real_catalog() -> None:
    cat = load_catalog()

    assert tier_candidates(cat, "T0") == [
        "gpt-5.6-luna",
        "gpt-reserve",
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-low",
        "gemini-3.8-flash-medium",
        "claude-haiku-4-5-20251001",
        "grok-4.5",
        "grok-4.6",
    ]

    assert tier_candidates(cat, "T3") == [
        "grok-4.6",
        "gpt-5.6-sol",
        "claude-opus-5",
    ]

    assert tier_candidates(cat, "review") == [
        "grok-4.6",
        "gpt-5.6-sol",
        "claude-opus-5",
    ]

    for t in ("T0", "T1", "T2", "T3"):
        assert "gpt-5.5" not in tier_candidates(cat, t)

    for t in ("T0", "T1", "T2", "T3", "review"):
        assert "claude-fable-5-1" not in tier_candidates(cat, t)

    with pytest.raises(ValueError, match="unknown tier: T9"):
        tier_candidates(cat, "T9")


# Fixtures / helpers for derivation and validation tests using synthetic catalog
BASE_TIERING_YAML = """\
schema_version: 1
blend: {input: 1, output: 3}
tiers:
  T0: {min_bench: 0, max_blended_price: 5}
  T1: {min_bench: 60, max_blended_price: 10}
  T2: {min_bench: 74, max_blended_price: 25}
  T3: {min_bench: 86, max_blended_price: null}
orchestrator_excluded_vendors: [agy]
"""

BASE_MODELS_YAML = """\
schema_version: 2
catalog_version: "2026-09-26.1"
pools:
  test-pool:
    provider: test
    display_name: "Test Provider"
    slots: {"5h": "5h", "1w": "1w"}
  agy:
    provider: antigravity
    display_name: "Antigravity · Gemini"
    slots: {"5h": "5h"}
  agy-3p:
    provider: antigravity
    display_name: "Antigravity · Claude"
    slots: {"5h": "5h"}
models:
  model-a:
    pool: test-pool
    slots: ["5h"]
    bench: 70
    price: {input: 1.0, output: 2.0, as_of: "2026-09-26", source: "test-src"}
"""


def _write_catalog(directory: Path, models_content: str, tiering_content: str = BASE_TIERING_YAML) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "models.yaml").write_text(models_content, encoding="utf-8")
    (directory / "tiering.yaml").write_text(tiering_content, encoding="utf-8")


def test_derivation_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. 價格正好等於門檻時算「符合」（例：混合價 5.0 → min_tier T0）
    # blend input:1, output:3 -> input=5.0, output=5.0 gives blended_price = 5.0
    # 2. bench 正好等於門檻時算「符合」（bench 60 -> T1, bench 74 -> T2, bench 86 -> T3）
    # 3. agy pool 的 T3 模型 orchestrator 為 False
    # 4. agy-3p pool 的 vendor 為 agy
    # 5. dispatchable: false 的 T3 模型 reviewer 為 False
    models_yaml = """\
schema_version: 2
catalog_version: "2026-09-26.1"
pools:
  test-pool:
    provider: test
    display_name: "Test"
    slots: {"5h": "5h"}
  agy:
    provider: antigravity
    display_name: "Antigravity Gemini"
    slots: {"5h": "5h"}
  agy-3p:
    provider: antigravity
    display_name: "Antigravity 3P"
    slots: {"5h": "5h"}
models:
  model-a:
    pool: test-pool
    slots: ["5h"]
    bench: 50
    price: {input: 5.0, output: 5.0, as_of: "2026-09-26", source: "test"}
  model-b1:
    pool: test-pool
    slots: ["5h"]
    bench: 60
    price: {input: 1.0, output: 1.0, as_of: "2026-09-26", source: "test"}
  model-b2:
    pool: test-pool
    slots: ["5h"]
    bench: 74
    price: {input: 1.0, output: 1.0, as_of: "2026-09-26", source: "test"}
  model-b3:
    pool: test-pool
    slots: ["5h"]
    bench: 86
    price: {input: 1.0, output: 1.0, as_of: "2026-09-26", source: "test"}
  model-c:
    pool: agy
    slots: ["5h"]
    bench: 90
    price: {input: 1.0, output: 1.0, as_of: "2026-09-26", source: "test"}
  model-d:
    pool: agy-3p
    slots: ["5h"]
    bench: 70
    price: {input: 1.0, output: 1.0, as_of: "2026-09-26", source: "test"}
  model-e:
    pool: test-pool
    slots: ["5h"]
    bench: 90
    price: {input: 1.0, output: 1.0, as_of: "2026-09-26", source: "test"}
    dispatchable: false
"""
    _write_catalog(tmp_path, models_yaml)

    # 6. CATALOG_DIR 環境變數生效 (monkeypatch.setenv)
    monkeypatch.setenv("CATALOG_DIR", str(tmp_path))
    cat = load_catalog()  # call without arguments to test env var

    # 1. 價格剛好 5.0 -> min_tier T0
    assert cat.models["model-a"].blended_price == pytest.approx(5.0)
    assert cat.models["model-a"].min_tier == "T0"

    # 2. bench 正好等於門檻
    assert cat.models["model-b1"].max_tier == "T1"
    assert cat.models["model-b2"].max_tier == "T2"
    assert cat.models["model-b3"].max_tier == "T3"

    # 3. agy pool 的 T3 模型 orchestrator 為 False
    assert cat.models["model-c"].max_tier == "T3"
    assert cat.models["model-c"].orchestrator is False

    # 4. agy-3p pool 的 vendor 為 agy
    assert cat.models["model-d"].vendor == "agy"

    # 5. dispatchable: false 的 T3 模型 reviewer 為 False
    assert cat.models["model-e"].max_tier == "T3"
    assert "T3" in cat.models["model-e"].tiers
    assert cat.models["model-e"].dispatchable is False
    assert cat.models["model-e"].reviewer is False


# 驗證失敗 tests (17 條)
def test_validation_unknown_top_level_key(tmp_path: Path) -> None:
    bad_yaml = BASE_MODELS_YAML + "unknown_top_key: 123\n"
    _write_catalog(tmp_path, bad_yaml)
    with pytest.raises(ValueError, match=r"models\.yaml: unknown_top_key: unknown key"):
        load_catalog(tmp_path)


def test_validation_model_unknown_key(tmp_path: Path) -> None:
    bad_yaml = BASE_MODELS_YAML.replace("bench: 70", "bench: 70\n    unknown_model_field: 42")
    _write_catalog(tmp_path, bad_yaml)
    with pytest.raises(ValueError, match=r"models\.yaml: models\.model-a\.unknown_model_field: unknown key"):
        load_catalog(tmp_path)


def test_validation_price_unknown_key(tmp_path: Path) -> None:
    bad_yaml = BASE_MODELS_YAML.replace('source: "test-src"', 'source: "test-src", unknown_price_field: 99')
    _write_catalog(tmp_path, bad_yaml)
    with pytest.raises(ValueError, match=r"models\.yaml: models\.model-a\.price\.unknown_price_field: unknown key"):
        load_catalog(tmp_path)


def test_validation_missing_bench(tmp_path: Path) -> None:
    bad_yaml = BASE_MODELS_YAML.replace("    bench: 70\n", "")
    _write_catalog(tmp_path, bad_yaml)
    with pytest.raises(ValueError, match=r"models\.yaml: models\.model-a\.bench: missing required field"):
        load_catalog(tmp_path)


def test_validation_bench_out_of_range(tmp_path: Path) -> None:
    bad_yaml = BASE_MODELS_YAML.replace("bench: 70", "bench: 101")
    _write_catalog(tmp_path, bad_yaml)
    with pytest.raises(ValueError, match=r"models\.yaml: models\.model-a\.bench: must be an int between 0 and 100"):
        load_catalog(tmp_path)


def test_validation_bench_is_boolean(tmp_path: Path) -> None:
    bad_yaml = BASE_MODELS_YAML.replace("bench: 70", "bench: true")
    _write_catalog(tmp_path, bad_yaml)
    with pytest.raises(ValueError, match=r"models\.yaml: models\.model-a\.bench: must be an int between 0 and 100"):
        load_catalog(tmp_path)


def test_validation_price_input_negative(tmp_path: Path) -> None:
    bad_yaml = BASE_MODELS_YAML.replace("input: 1.0", "input: -1")
    _write_catalog(tmp_path, bad_yaml)
    with pytest.raises(ValueError, match=r"models\.yaml: models\.model-a\.price\.input: must be a number >= 0"):
        load_catalog(tmp_path)


def test_validation_unknown_pool(tmp_path: Path) -> None:
    bad_yaml = BASE_MODELS_YAML.replace("pool: test-pool", "pool: nonexistent-pool")
    _write_catalog(tmp_path, bad_yaml)
    with pytest.raises(ValueError, match=r"models\.yaml: models\.model-a\.pool: unknown pool 'nonexistent-pool'"):
        load_catalog(tmp_path)


def test_validation_slot_not_in_pool(tmp_path: Path) -> None:
    bad_yaml = BASE_MODELS_YAML.replace('slots: ["5h"]', 'slots: ["nonexistent-slot"]')
    _write_catalog(tmp_path, bad_yaml)
    with pytest.raises(ValueError, match=r"models\.yaml: models\.model-a\.slots: slot 'nonexistent-slot' not found in pool 'test-pool'"):
        load_catalog(tmp_path)


def test_validation_proxy_of_unknown_model(tmp_path: Path) -> None:
    bad_yaml = BASE_MODELS_YAML.replace('source: "test-src"', 'source: "test-src", proxy_of: "nonexistent-model"')
    _write_catalog(tmp_path, bad_yaml)
    with pytest.raises(ValueError, match=r"models\.yaml: models\.model-a\.price\.proxy_of: unknown model 'nonexistent-model'"):
        load_catalog(tmp_path)


def test_validation_as_of_format(tmp_path: Path) -> None:
    bad_yaml = BASE_MODELS_YAML.replace('as_of: "2026-09-26"', 'as_of: "2026/09/26"')
    _write_catalog(tmp_path, bad_yaml)
    with pytest.raises(ValueError, match=r"models\.yaml: models\.model-a\.price\.as_of: must be a string in YYYY-MM-DD format"):
        load_catalog(tmp_path)


def test_validation_tier_t0_min_bench_not_zero(tmp_path: Path) -> None:
    bad_tiering = BASE_TIERING_YAML.replace("T0: {min_bench: 0", "T0: {min_bench: 5")
    _write_catalog(tmp_path, BASE_MODELS_YAML, bad_tiering)
    with pytest.raises(ValueError, match=r"tiering\.yaml: tiers\.T0\.min_bench: must be 0"):
        load_catalog(tmp_path)


def test_validation_min_bench_decreasing(tmp_path: Path) -> None:
    # T1 min_bench 60, change T2 min_bench to 50
    bad_tiering = BASE_TIERING_YAML.replace("T2: {min_bench: 74", "T2: {min_bench: 50")
    _write_catalog(tmp_path, BASE_MODELS_YAML, bad_tiering)
    with pytest.raises(ValueError, match=r"tiering\.yaml: tiers\.T2\.min_bench: must be non-decreasing"):
        load_catalog(tmp_path)


def test_validation_max_blended_price_decreasing(tmp_path: Path) -> None:
    # T0 is 5, change T1 to 3
    bad_tiering = BASE_TIERING_YAML.replace("max_blended_price: 10", "max_blended_price: 3")
    _write_catalog(tmp_path, BASE_MODELS_YAML, bad_tiering)
    with pytest.raises(ValueError, match=r"tiering\.yaml: tiers\.T1\.max_blended_price: must be non-decreasing"):
        load_catalog(tmp_path)


def test_validation_number_after_null_price(tmp_path: Path) -> None:
    # Change T2 to null, while T3 is 50
    bad_tiering = BASE_TIERING_YAML.replace(
        "T2: {min_bench: 74, max_blended_price: 25}",
        "T2: {min_bench: 74, max_blended_price: null}",
    ).replace(
        "T3: {min_bench: 86, max_blended_price: null}",
        "T3: {min_bench: 86, max_blended_price: 50}",
    )
    _write_catalog(tmp_path, BASE_MODELS_YAML, bad_tiering)
    with pytest.raises(ValueError, match=r"tiering\.yaml: tiers\.T3\.max_blended_price: cannot specify a price limit after null"):
        load_catalog(tmp_path)


def test_validation_blend_output_zero(tmp_path: Path) -> None:
    bad_tiering = BASE_TIERING_YAML.replace("output: 3", "output: 0")
    _write_catalog(tmp_path, BASE_MODELS_YAML, bad_tiering)
    with pytest.raises(ValueError, match=r"tiering\.yaml: blend\.output: must be a number > 0"):
        load_catalog(tmp_path)


def test_validation_models_schema_version_one(tmp_path: Path) -> None:
    bad_yaml = BASE_MODELS_YAML.replace("schema_version: 2", "schema_version: 1")
    _write_catalog(tmp_path, bad_yaml)
    with pytest.raises(ValueError, match=r"models\.yaml: schema_version: schema_version must be 2"):
        load_catalog(tmp_path)
