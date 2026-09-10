from datetime import datetime, timedelta, timezone

from app.models import AccountSnapshot, ProviderId, SnapshotStatus, UsageWindow
from app.services.routing_view import (
    MODELS,
    PACE_PENALTY,
    POOLS,
    TIERS,
    build_routing_payload,
)

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


def test_tables_are_consistent():
    for model, spec in MODELS.items():
        assert spec["pool"] in POOLS, model
        for slot in spec["slots"]:
            assert slot in POOLS[spec["pool"]]["slots"], (model, slot)
        assert 0 <= spec["max_tier"] <= 3, model
    assert len({spec["cost_rank"] for spec in MODELS.values()}) == len(MODELS)
    for tier, spec in TIERS.items():
        for m in spec["candidates"]:
            assert m in MODELS, (tier, m)
    # tier candidates are capability-filtered, not pinned
    assert "gemini-3.8-flash-high" in TIERS["T2"]["candidates"]
    assert "gemini-3.8-flash-high" not in TIERS["T3"]["candidates"]
    assert "gemini-3.8-flash-low" not in TIERS["T1"]["candidates"]
    assert set(TIERS["T3"]["candidates"]) == {"gpt-5.6-sol", "grok-4.6", "claude-opus-5"}
    assert set(TIERS["review"]["candidates"]) == {"gpt-5.6-sol", "grok-4.6", "claude-opus-5"}
    assert set(TIERS["T1"]["candidates"]) < set(TIERS["T0"]["candidates"])
    for tier in TIERS.values():
        assert "claude-fable-5-1" not in tier["candidates"]
    for m, spec in MODELS.items():
        assert spec["role"] in {"orchestrator", "subagent"}, m
        if spec.get("reviewer"):
            assert spec["max_tier"] == 3, m


def test_pools_windows_reset_and_binding():
    out = build_routing_payload(_all_ok(), now=NOW)

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


def test_model_binding_ignores_fable_cap_for_sonnet():
    out = build_routing_payload(_all_ok(), now=NOW)
    sonnet = out["models"]["claude-sonnet-5"]
    fable = out["models"]["claude-fable-5-1"]
    assert sonnet["binding_slot"] == "5h"
    assert sonnet["remaining_percent"] == 71.0
    assert sonnet["seconds_until_reset"] == 11_000
    assert sonnet["vendor"] == "claude"
    assert fable["binding_slot"] == "1w-fable"
    assert fable["remaining_percent"] == 64.0
    assert out["models"]["grok-4.6"]["pool"] == "grok"
    assert out["models"]["gemini-3.8-flash-high"]["vendor"] == "agy"
    assert out["models"]["gpt-5.6-sol"]["role"] == "orchestrator"


def test_tiers_rank_by_quota_score_then_cost():
    out = build_routing_payload(_all_ok(), now=NOW)
    # agy-3p 100 is not a model pool; codex 98 (5h) is the best real pool,
    # and within the codex pool the cheapest model wins the tie.
    t0 = out["tiers"]["T0"]
    assert t0["recommended"] == "gpt-reserve"
    assert [c["model"] for c in t0["candidates"]][:2] == ["gpt-reserve", "gpt-5.6-luna"]
    assert t0["usable_candidates"] == len(TIERS["T0"]["candidates"])
    assert "highest quota score" in t0["reason"]
    assert all(c["usable"] for c in t0["candidates"])
    assert out["tiers"]["T1"]["recommended"] == "gpt-5.6-luna"  # gpt-reserve is T0-only
    # T3 only admits max_tier 3 models; codex sol (98) beats grok (72) and opus (71)
    assert [c["model"] for c in out["tiers"]["T3"]["candidates"]] == ["gpt-5.6-sol", "grok-4.6", "claude-opus-5"]
    assert out["tiers"]["review"]["recommended"] == "gpt-5.6-sol"
    assert out["orchestrators"][0] == "gpt-5.6-sol"
    assert set(out["orchestrators"]) == {"gpt-5.6-sol", "grok-4.6", "claude-opus-5", "claude-fable-5-1"}
    assert out["models"]["claude-fable-5-1"]["dispatchable"] is False
    assert out["models"]["gpt-reserve"]["role"] == "subagent"


def test_tier_skips_critical_models():
    snaps = _all_ok(claude_5h_used=95.0)  # sonnet/opus binding = 5h → 5 % → critical
    out = build_routing_payload(snaps, now=NOW)
    assert out["models"]["claude-sonnet-5"]["level"] == "critical"
    assert out["models"]["claude-sonnet-5"]["usable"] is False
    t2 = out["tiers"]["T2"]
    assert t2["recommended"] == "gpt-5.6-luna"  # cheapest usable codex model
    assert t2["vendor"] == "codex"
    # unusable models sort last regardless of cost
    assert [c["model"] for c in t2["candidates"]][-2:] == ["claude-sonnet-5", "claude-opus-5"]
    assert t2["usable_candidates"] == len(TIERS["T2"]["candidates"]) - 2
    assert out["tiers"]["T3"]["recommended"] == "gpt-5.6-sol"
    assert out["tiers"]["review"]["recommended"] == "gpt-5.6-sol"


def test_tier_prefers_gemini_when_it_has_the_most_quota():
    snaps = _all_ok()
    snaps = [
        _snap(ProviderId.CODEX, [_win("5h", 60, 13_000, 18000), _win("1w", 50, 600_000, 604800)])
        if s.provider == ProviderId.CODEX else s
        for s in snaps
    ]
    out = build_routing_payload(snaps, now=NOW)
    assert out["tiers"]["T0"]["recommended"] == "gemini-3.8-flash-low"  # cheapest agy (78.42)
    assert out["tiers"]["T1"]["recommended"] == "gemini-3.8-flash-medium"  # flash-low is T0-only
    assert out["tiers"]["T2"]["recommended"] == "gemini-3.8-flash-high"  # T2-capable by benchmark
    assert out["tiers"]["T3"]["recommended"] == "grok-4.6"  # 72 beats opus 71 and codex 40


def test_tier_with_no_usable_candidate():
    snaps = [s for s in _all_ok() if s.provider not in {ProviderId.CODEX, ProviderId.CLAUDE, ProviderId.SUPERGROK}]
    out = build_routing_payload(snaps, now=NOW)
    t3 = out["tiers"]["T3"]
    assert t3["usable_candidates"] == 0
    assert t3["recommended"] in {"gpt-5.6-sol", "grok-4.6", "claude-opus-5"}
    assert "no usable candidate" in t3["reason"]
    assert all(not c["usable"] for c in t3["candidates"])


def test_tier_falls_back_when_primary_not_ok_status():
    snaps = _all_ok()
    snaps = [
        s if s.provider != ProviderId.CODEX else _snap(ProviderId.CODEX, s.windows, status=SnapshotStatus.AUTH_ERROR)
        for s in snaps
    ]
    out = build_routing_payload(snaps, now=NOW)
    assert out["pools"]["codex"]["usable"] is False
    assert out["tiers"]["T3"]["recommended"] == "grok-4.6"
    assert out["tiers"]["T3"]["candidates"][-1]["model"] == "gpt-5.6-sol"


def test_missing_provider_is_unknown_and_unusable():
    snaps = [s for s in _all_ok() if s.provider != ProviderId.SUPERGROK]
    out = build_routing_payload(snaps, now=NOW)
    grok = out["pools"]["grok"]
    assert grok["status"] == "missing"
    assert grok["usable"] is False
    assert grok["level"] == "unknown"
    assert out["models"]["grok-4.6"]["usable"] is False
    t1 = out["tiers"]["T1"]
    assert t1["recommended"] == "gpt-5.6-luna"
    # unusable candidates sort last
    assert {c["model"] for c in t1["candidates"][-2:]} == {"grok-4.5", "grok-4.6"}


def test_stale_flag_uses_threshold():
    snaps = _all_ok()
    snaps = [s if s.provider != ProviderId.CLAUDE else _snap(ProviderId.CLAUDE, s.windows, age_s=1200) for s in snaps]
    out = build_routing_payload(snaps, now=NOW, stale_after_seconds=900)
    assert out["pools"]["claude"]["stale"] is True
    assert out["models"]["claude-sonnet-5"]["stale"] is True
    # stale alone does not make it unusable
    assert out["models"]["claude-sonnet-5"]["usable"] is True
    out2 = build_routing_payload(snaps, now=NOW, stale_after_seconds=3600)
    assert out2["pools"]["claude"]["stale"] is False


def test_burn_rate_and_pace_penalty():
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
    out = build_routing_payload(snaps, hist, now=NOW)
    w = out["pools"]["claude"]["windows"]["5h"]
    assert w["burn_per_hour"] == 10.0
    assert w["hours_until_empty"] == 4.0
    assert w["idle"] is False
    assert w["will_last_until_reset"] is False
    assert w["projected_used_at_reset"] == 100.0
    sonnet = out["models"]["claude-sonnet-5"]
    assert sonnet["binding_slot"] == "5h"
    assert sonnet["pace_penalty"] is True
    assert sonnet["score"] == round(40.0 * PACE_PENALTY, 1)
    assert sonnet["level"] == "ok"  # penalty affects score, not level
    # penalised sonnet (20) drops behind codex (98) and grok (72)
    assert [c["model"] for c in out["tiers"]["T2"]["candidates"]][:2] == ["gpt-5.6-luna", "gpt-5.6-terra"]


def test_burn_rate_idle_lasts_until_reset():
    reset_in = 5 * 3600
    snaps = _all_ok()
    hist = {
        ProviderId.CODEX.value: _history(ProviderId.CODEX, "5h", [(60, 2.0), (0, 2.0)], 13_000)
    }
    out = build_routing_payload(snaps, hist, now=NOW)
    w = out["pools"]["codex"]["windows"]["5h"]
    assert w["burn_per_hour"] == 0.0
    assert w["hours_until_empty"] is None
    assert w["idle"] is True
    assert w["will_last_until_reset"] is True
    assert w["projected_used_at_reset"] == 2.0
    assert out["models"]["gpt-5.6-luna"]["pace_penalty"] is False
