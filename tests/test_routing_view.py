from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models import AccountSnapshot, ProviderId, SnapshotStatus, UsageWindow
from app.services.routing_view import (
    PACE_PENALTY,
    build_routing_payload,
)
from catalog.tiering import Catalog, load_catalog

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _snap(pid: ProviderId, windows: list[UsageWindow], *, age_s: int = 60, status=SnapshotStatus.OK):
    return AccountSnapshot(
        provider=pid,
        display_name=pid.value,
        status=status,
        windows=windows,
        fetched_at=NOW - timedelta(seconds=age_s),
    )


def _win(key: str, used: float, reset_in_s: int, limit: int | None = None) -> UsageWindow:
    return UsageWindow(
        key=key,
        label=key,
        used_percent=used,
        remaining_percent=100 - used,
        resets_at=NOW + timedelta(seconds=reset_in_s),
        limit_window_seconds=limit,
    )


def _history(pid: ProviderId, key: str, series: list[tuple[int, float]], reset_in_s: int):
    """series: [(minutes_ago, used_percent), ...] oldest first."""
    rows = []
    for minutes_ago, used in series:
        snap = AccountSnapshot(
            provider=pid,
            display_name=pid.value,
            status=SnapshotStatus.OK,
            windows=[_win(key, used, reset_in_s)],
            fetched_at=NOW - timedelta(minutes=minutes_ago),
        )
        rows.append({
            "provider": pid.value,
            "fetched_at": snap.fetched_at.isoformat(),
            "snapshot": snap.model_dump(mode="json"),
        })
    return rows


def _all_ok(claude_5h_used=29.0, fable_used=36.0):
    return [
        _snap(ProviderId.CODEX, [_win("5h", 2, 13_000, 18000), _win("1w", 0, 600_000, 604800)]),
        _snap(ProviderId.SUPERGROK, [_win("weekly", 28, 350_000, 604800), _win("product-4", 2, 350_000)]),
        _snap(ProviderId.DEEPSEEK, [UsageWindow(key="balance-cny", label="CNY", amount="40.68", currency="CNY")]),
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
                _win("5h", claude_5h_used, 11_000, 18000),
                _win("1w", 22, 340_000, 604800),
                _win("1w-fable", fable_used, 340_000, 604800),
            ],
        ),
    ]


def make_fake_catalog(tmp_path: Path) -> Catalog:
    tiering_yaml = """schema_version: 1
blend: {input: 1, output: 3}
tiers:
  T0: {min_bench: 0, max_blended_price: 5}
  T1: {min_bench: 60, max_blended_price: 10}
  T2: {min_bench: 74, max_blended_price: 25}
  T3: {min_bench: 86, max_blended_price: null}
orchestrator_excluded_vendors: [agy]
"""
    models_yaml = """schema_version: 2
catalog_version: "fake-catalog-1.0"
pools:
  claude:
    provider: claude
    display_name: "Claude Code (claude)"
    slots: {"5h": "5h", "1w": "1w", "1w-fable": "1w-fable"}
  codex:
    provider: codex
    display_name: "Codex (codex)"
    slots: {"5h": "5h", "1w": "1w"}
  grok:
    provider: supergrok
    display_name: "SuperGrok (grok)"
    slots: {"1w": "weekly"}
  agy:
    provider: antigravity
    display_name: "Antigravity · Gemini (agy)"
    slots: {"5h": "5h", "1w": "1w"}
  agy-3p:
    provider: antigravity
    display_name: "Antigravity · Claude/GPT (agy)"
    slots: {"5h": "3p-5h", "1w": "3p-1w"}
models:
  # codex
  model-codex-top:
    pool: codex
    slots: ["5h", "1w"]
    bench: 90
    price: {input: 4.0, output: 4.0, as_of: "2026-09-26", source: "test"}
  model-strong:
    pool: codex
    slots: ["5h", "1w"]
    bench: 90
    price: {input: 30.0, output: 30.0, as_of: "2026-09-26", source: "test"}
  model-codex-mid:
    pool: codex
    slots: ["5h", "1w"]
    bench: 80
    price: {input: 4.0, output: 4.0, as_of: "2026-09-26", source: "test"}
  model-codex-low:
    pool: codex
    slots: ["5h", "1w"]
    bench: 65
    price: {input: 4.0, output: 4.0, as_of: "2026-09-26", source: "test"}
  model-cheap:
    pool: codex
    slots: ["5h", "1w"]
    bench: 50
    price: {input: 1.0, output: 1.0, as_of: "2026-09-26", source: "test"}
  model-empty:
    pool: codex
    slots: ["5h", "1w"]
    bench: 50
    price: {input: 30.0, output: 30.0, as_of: "2026-09-26", source: "test"}
  model-tie-cheap:
    pool: codex
    slots: ["5h", "1w"]
    bench: 70
    price: {input: 2.0, output: 2.0, as_of: "2026-09-26", source: "test"}
  model-tie-pricey:
    pool: codex
    slots: ["5h", "1w"]
    bench: 70
    price: {input: 3.0, output: 3.0, as_of: "2026-09-26", source: "test"}

  # claude
  model-c-top:
    pool: claude
    slots: ["5h", "1w"]
    bench: 88
    price: {input: 15.0, output: 15.0, as_of: "2026-09-26", source: "test"}
  model-c-mid:
    pool: claude
    slots: ["5h", "1w"]
    bench: 78
    price: {input: 15.0, output: 15.0, as_of: "2026-09-26", source: "test"}
  model-fable:
    pool: claude
    slots: ["5h", "1w", "1w-fable"]
    bench: 95
    price: {input: 30.0, output: 30.0, as_of: "2026-09-26", source: "test"}
    dispatchable: false

  # grok
  model-g-top:
    pool: grok
    slots: ["1w"]
    bench: 86
    price: {input: 8.0, output: 8.0, as_of: "2026-09-26", source: "test"}
  model-g-mid:
    pool: grok
    slots: ["1w"]
    bench: 77
    price: {input: 8.0, output: 8.0, as_of: "2026-09-26", source: "test"}

  # agy
  model-a-top:
    pool: agy
    slots: ["5h", "1w"]
    bench: 81
    price: {input: 1.0, output: 1.0, as_of: "2026-09-26", source: "test"}
  model-a-low:
    pool: agy
    slots: ["5h", "1w"]
    bench: 55
    price: {input: 1.0, output: 1.0, as_of: "2026-09-26", source: "test"}
"""
    (tmp_path / "tiering.yaml").write_text(tiering_yaml, encoding="utf-8")
    (tmp_path / "models.yaml").write_text(models_yaml, encoding="utf-8")
    return load_catalog(tmp_path)


@pytest.fixture
def fake_catalog(tmp_path: Path) -> Catalog:
    return make_fake_catalog(tmp_path)


def test_payload_uses_derived_tiers(fake_catalog):
    out = build_routing_payload(_all_ok(), now=NOW, catalog=fake_catalog)
    # cheap but weak model only in T0
    assert "model-cheap" in [c["model"] for c in out["tiers"]["T0"]["candidates"]]
    for t in ("T1", "T2", "T3"):
        assert "model-cheap" not in [c["model"] for c in out["tiers"][t]["candidates"]]

    # expensive but strong model only in T3
    for t in ("T0", "T1", "T2"):
        assert "model-strong" not in [c["model"] for c in out["tiers"][t]["candidates"]]
    assert "model-strong" in [c["model"] for c in out["tiers"]["T3"]["candidates"]]

    # empty range model in no tiers
    for t in ("T0", "T1", "T2", "T3", "review"):
        assert "model-empty" not in [c["model"] for c in out["tiers"][t]["candidates"]]

    # dispatchable: false in models and orchestrators, but not in any tier candidates
    assert "model-fable" in out["models"]
    assert out["models"]["model-fable"]["dispatchable"] is False
    assert "model-fable" in out["orchestrators"]
    for t in ("T0", "T1", "T2", "T3", "review"):
        assert "model-fable" not in [c["model"] for c in out["tiers"][t]["candidates"]]


def test_pools_windows_reset_and_binding(fake_catalog):
    out = build_routing_payload(_all_ok(), now=NOW, catalog=fake_catalog)

    claude = out["pools"]["claude"]
    assert claude["status"] == "ok"
    assert claude["stale"] is False
    assert claude["data_age_seconds"] == 60
    assert claude["windows"]["5h"]["remaining_percent"] == 71.0
    assert claude["windows"]["5h"]["seconds_until_reset"] == 11_000
    assert claude["windows"]["1w-fable"]["remaining_percent"] == 64.0
    # No history → no burn estimate, but the window is still reported.
    assert claude["windows"]["5h"]["burn_per_hour"] is None
    assert claude["windows"]["5h"]["will_last_until_reset"] is None
    # Pool binding = min across all slots (fable 64 %)
    assert claude["binding_slot"] == "1w-fable"
    assert claude["score"] == 64.0
    assert claude["level"] == "ok"

    # grok maps the SuperGrok "weekly" key to the normalised 1w slot
    assert out["pools"]["grok"]["windows"]["1w"]["key"] == "weekly"
    assert out["pools"]["grok"]["windows"]["1w"]["remaining_percent"] == 72.0
    assert "5h" not in out["pools"]["grok"]["windows"]

    # Antigravity splits into gemini and 3p pools
    assert out["pools"]["agy"]["windows"]["1w"]["remaining_percent"] == 78.42
    assert out["pools"]["agy-3p"]["windows"]["1w"]["remaining_percent"] == 100.0


def test_model_binding_ignores_fable_cap_for_subagents(fake_catalog):
    out = build_routing_payload(_all_ok(), now=NOW, catalog=fake_catalog)
    mid = out["models"]["model-c-mid"]
    fable = out["models"]["model-fable"]
    assert mid["binding_slot"] == "5h"
    assert mid["remaining_percent"] == 71.0
    assert mid["seconds_until_reset"] == 11_000
    assert mid["vendor"] == "claude"
    assert fable["binding_slot"] == "1w-fable"
    assert fable["remaining_percent"] == 64.0
    assert out["models"]["model-g-top"]["pool"] == "grok"
    assert out["models"]["model-a-top"]["vendor"] == "agy"
    assert out["models"]["model-codex-top"]["role"] == "orchestrator"


def test_tiers_rank_by_quota_score_then_cost(fake_catalog):
    out = build_routing_payload(_all_ok(), now=NOW, catalog=fake_catalog)

    # codex 98 (5h) is the best pool, and within it strongest benchmark wins the tie.
    t0 = out["tiers"]["T0"]
    assert t0["recommended"] == "model-codex-top"
    assert [c["model"] for c in t0["candidates"]][:3] == ["model-codex-top", "model-codex-mid", "model-tie-cheap"]
    assert t0["usable_candidates"] == len(t0["candidates"])
    assert "highest quota score" in t0["reason"]
    assert all(c["usable"] for c in t0["candidates"])
    assert out["tiers"]["T1"]["recommended"] == "model-codex-top"
    assert out["tiers"]["T2"]["recommended"] == "model-codex-top"
    # T3 candidates
    assert [c["model"] for c in out["tiers"]["T3"]["candidates"]] == ["model-codex-top", "model-strong", "model-g-top", "model-c-top"]
    assert out["tiers"]["review"]["recommended"] == "model-codex-top"
    assert out["orchestrators"][0] == "model-codex-top"
    assert set(out["orchestrators"]) == {"model-codex-top", "model-strong", "model-g-top", "model-c-top", "model-fable"}
    assert out["models"]["model-fable"]["dispatchable"] is False
    assert out["models"]["model-cheap"]["role"] == "subagent"


def test_tier_skips_critical_models(fake_catalog):
    snaps = _all_ok(claude_5h_used=95.0)  # claude binding = 5h → 5 % → critical
    out = build_routing_payload(snaps, now=NOW, catalog=fake_catalog)
    assert out["models"]["model-c-top"]["level"] == "critical"
    assert out["models"]["model-c-top"]["usable"] is False
    t2 = out["tiers"]["T2"]
    assert t2["recommended"] == "model-codex-top"
    assert t2["vendor"] == "codex"
    # unusable models sort last regardless of cost
    assert [c["model"] for c in t2["candidates"]][-2:] == ["model-c-top", "model-c-mid"]
    assert out["tiers"]["T3"]["recommended"] == "model-codex-top"
    assert out["tiers"]["review"]["recommended"] == "model-codex-top"


def test_tier_prefers_agy_when_it_has_the_most_quota(fake_catalog):
    snaps = _all_ok()
    snaps = [
        _snap(ProviderId.CODEX, [_win("5h", 60, 13_000, 18000), _win("1w", 50, 600_000, 604800)])
        if s.provider == ProviderId.CODEX else s
        for s in snaps
    ]
    out = build_routing_payload(snaps, now=NOW, catalog=fake_catalog)
    # agy (78.42) has the most quota; within it model-a-top has the best bench
    assert out["tiers"]["T0"]["recommended"] == "model-a-top"
    assert out["tiers"]["T1"]["recommended"] == "model-a-top"
    assert out["tiers"]["T2"]["recommended"] == "model-a-top"
    assert out["tiers"]["T3"]["recommended"] == "model-g-top"  # grok 72 beats claude 71 and codex 40


def test_tier_with_no_usable_candidate(fake_catalog):
    snaps = [s for s in _all_ok() if s.provider not in {ProviderId.CODEX, ProviderId.CLAUDE, ProviderId.SUPERGROK}]
    out = build_routing_payload(snaps, now=NOW, catalog=fake_catalog)
    t3 = out["tiers"]["T3"]
    assert t3["usable_candidates"] == 0
    assert t3["recommended"] in {"model-codex-top", "model-strong", "model-g-top", "model-c-top"}
    assert "no usable candidate" in t3["reason"]
    assert all(not c["usable"] for c in t3["candidates"])


def test_tier_falls_back_when_primary_not_ok_status(fake_catalog):
    snaps = _all_ok()
    snaps = [
        s if s.provider != ProviderId.CODEX else _snap(ProviderId.CODEX, s.windows, status=SnapshotStatus.AUTH_ERROR)
        for s in snaps
    ]
    out = build_routing_payload(snaps, now=NOW, catalog=fake_catalog)
    assert out["pools"]["codex"]["usable"] is False
    assert out["tiers"]["T3"]["recommended"] == "model-g-top"
    assert out["tiers"]["T3"]["candidates"][-1]["model"] in {"model-codex-top", "model-strong"}


def test_missing_provider_is_unknown_and_unusable(fake_catalog):
    snaps = [s for s in _all_ok() if s.provider != ProviderId.SUPERGROK]
    out = build_routing_payload(snaps, now=NOW, catalog=fake_catalog)
    grok = out["pools"]["grok"]
    assert grok["status"] == "missing"
    assert grok["usable"] is False
    assert grok["level"] == "unknown"
    assert out["models"]["model-g-top"]["usable"] is False
    t1 = out["tiers"]["T1"]
    assert t1["recommended"] == "model-codex-top"
    # unusable candidates sort last
    assert {c["model"] for c in t1["candidates"][-2:]} == {"model-g-top", "model-g-mid"}


def test_stale_flag_uses_threshold(fake_catalog):
    snaps = _all_ok()
    snaps = [s if s.provider != ProviderId.CLAUDE else _snap(ProviderId.CLAUDE, s.windows, age_s=1200) for s in snaps]
    out = build_routing_payload(snaps, now=NOW, stale_after_seconds=900, catalog=fake_catalog)
    assert out["pools"]["claude"]["stale"] is True
    assert out["models"]["model-c-top"]["stale"] is True
    # stale alone does not make it unusable
    assert out["models"]["model-c-top"]["usable"] is True
    out2 = build_routing_payload(snaps, now=NOW, stale_after_seconds=3600, catalog=fake_catalog)
    assert out2["pools"]["claude"]["stale"] is False


def test_burn_rate_and_pace_penalty(fake_catalog):
    # Claude 5h: 60 % used, 10 %/h burn over the last hour, resets in 5 h
    # → projected 60 + 50 = 110 > 100, will not last → score halved.
    reset_in = 5 * 3600
    snaps = _all_ok()
    snaps = [
        s
        if s.provider != ProviderId.CLAUDE
        else _snap(ProviderId.CLAUDE, [_win("5h", 60, reset_in, 18000), _win("1w", 22, 340_000, 604800)])
        for s in snaps
    ]
    hist = {
        ProviderId.CLAUDE.value: _history(
            ProviderId.CLAUDE, "5h", [(60, 50.0), (30, 55.0), (0, 60.0)], reset_in
        )
    }
    out = build_routing_payload(snaps, hist, now=NOW, catalog=fake_catalog)
    w = out["pools"]["claude"]["windows"]["5h"]
    assert w["burn_per_hour"] == 10.0
    assert w["hours_until_empty"] == 4.0
    assert w["idle"] is False
    assert w["will_last_until_reset"] is False
    assert w["projected_used_at_reset"] == 100.0
    mid = out["models"]["model-c-mid"]
    assert mid["binding_slot"] == "5h"
    assert mid["pace_penalty"] is True
    assert mid["score"] == round(40.0 * PACE_PENALTY, 1)
    assert mid["level"] == "ok"
    assert [c["model"] for c in out["tiers"]["T2"]["candidates"]][:2] == ["model-codex-top", "model-codex-mid"]


def test_burn_rate_idle_lasts_until_reset(fake_catalog):
    reset_in = 5 * 3600
    snaps = _all_ok()
    hist = {
        ProviderId.CODEX.value: _history(ProviderId.CODEX, "5h", [(60, 2.0), (0, 2.0)], 13_000)
    }
    out = build_routing_payload(snaps, hist, now=NOW, catalog=fake_catalog)
    w = out["pools"]["codex"]["windows"]["5h"]
    assert w["burn_per_hour"] == 0.0
    assert w["hours_until_empty"] is None
    assert w["idle"] is True
    assert w["will_last_until_reset"] is True
    assert w["projected_used_at_reset"] == 2.0
    assert out["models"]["model-codex-mid"]["pace_penalty"] is False


def test_bench_breaks_quota_ties_before_cost(fake_catalog):
    out = build_routing_payload(_all_ok(), now=NOW, catalog=fake_catalog)
    codex = [c for c in out["tiers"]["T0"]["candidates"] if c["vendor"] == "codex"]
    assert [c["score"] for c in codex] == [codex[0]["score"]] * len(codex)
    bench_list = [c["bench"] for c in codex]
    assert bench_list == sorted(bench_list, reverse=True)


def test_price_breaks_quota_and_bench_ties(fake_catalog):
    out = build_routing_payload(_all_ok(), now=NOW, catalog=fake_catalog)
    t1_models = [c["model"] for c in out["tiers"]["T1"]["candidates"]]
    # model-tie-cheap (price 2.0) and model-tie-pricey (price 3.0) have same quota and bench (70)
    assert t1_models.index("model-tie-cheap") < t1_models.index("model-tie-pricey")


def test_derived_fields_and_catalog_shape(fake_catalog):
    out = build_routing_payload(_all_ok(), now=NOW, catalog=fake_catalog)
    for mid, row in out["models"].items():
        assert "cost_rank" not in row
        assert "min_tier" in row and (isinstance(row["min_tier"], int) or row["min_tier"] is None)
        assert "tiers" in row and isinstance(row["tiers"], list)
        assert "blended_price" in row and isinstance(row["blended_price"], float)
        assert "effort" in row and (isinstance(row["effort"], str) or row["effort"] is None)
        assert "reviewer" in row and isinstance(row["reviewer"], bool)
        assert "orchestrator" in row and isinstance(row["orchestrator"], bool)
        assert row["listed"] is None
        assert row["missing"] is None
        assert isinstance(row["max_tier"], int)
    for c in out["tiers"]["T0"]["candidates"]:
        assert "cost_rank" not in c
        assert "min_tier" in c and (isinstance(c["min_tier"], int) or c["min_tier"] is None)
        assert "blended_price" in c and isinstance(c["blended_price"], float)
        assert "effort" in c and (isinstance(c["effort"], str) or c["effort"] is None)
        assert isinstance(c["max_tier"], int)
    cat = out["catalog"]
    assert cat["catalog_version"] == "fake-catalog-1.0"
    assert cat["blend"] == {"input": 1.0, "output": 3.0}
    assert "T0" in cat["thresholds"]
    assert cat["uncatalogued"] == {}
    assert cat["cli_models"] is None


def test_smoke_real_catalog():
    payload = build_routing_payload([], now=NOW)
    assert set(c["model"] for c in payload["tiers"]["T3"]["candidates"]) == {
        "gpt-5.6-sol",
        "grok-4.6",
        "claude-opus-5",
    }
    all_tier_candidates = set()
    for t in ("T0", "T1", "T2", "T3"):
        all_tier_candidates.update(c["model"] for c in payload["tiers"][t]["candidates"])
    assert "gpt-5.5" not in all_tier_candidates
