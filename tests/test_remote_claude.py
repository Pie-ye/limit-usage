from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.providers.claude import ClaudeProvider
from app.services.remote_claude import RemoteClaudeRegistry


def statusline_payload(*, captured_at: datetime, five_hour: float, seven_day: float) -> dict:
    return {
        "captured_at_epoch": captured_at.timestamp(),
        "rate_limits": {
            "five_hour": {"used_percentage": five_hour},
            "seven_day": {"used_percentage": seven_day},
        },
    }


def make_provider(registry: RemoteClaudeRegistry, tmp_path) -> ClaudeProvider:
    # No local files: this exercises the remote path on its own.
    return ClaudeProvider(
        tmp_path / "missing-credentials.json",
        statusline_capture_path=tmp_path / "missing-statusline.json",
        usage_cache_path=tmp_path / "missing-cache.json",
        remote_readings=registry.entries,
    )


def test_report_rejects_unknown_kind():
    registry = RemoteClaudeRegistry()
    with pytest.raises(ValueError, match="unknown reading kind"):
        registry.report("oracle-edge", {"jsonl": {}})


def test_report_rejects_bad_host():
    registry = RemoteClaudeRegistry()
    with pytest.raises(ValueError, match="host must be"):
        registry.report("bad host/name", {"statusline": {}})


def test_report_requires_a_reading():
    registry = RemoteClaudeRegistry()
    with pytest.raises(ValueError, match="no readings supplied"):
        registry.report("oracle-edge", {"statusline": None, "usage_cache": None})


def test_host_cap_is_enforced():
    registry = RemoteClaudeRegistry(max_hosts=2)
    now = datetime.now(timezone.utc)
    for host in ("a", "b"):
        registry.report(host, {"statusline": statusline_payload(captured_at=now, five_hour=1, seven_day=1)})
    with pytest.raises(ValueError, match="too many hosts"):
        registry.report("c", {"statusline": statusline_payload(captured_at=now, five_hour=1, seven_day=1)})
    # An already-known host is still accepted at the cap.
    registry.report("a", {"statusline": statusline_payload(captured_at=now, five_hour=2, seven_day=2)})


def test_repeated_push_replaces_rather_than_accumulates():
    registry = RemoteClaudeRegistry()
    now = datetime.now(timezone.utc)
    for pct in (10, 20, 30):
        registry.report(
            "oracle-edge",
            {"statusline": statusline_payload(captured_at=now, five_hour=pct, seven_day=pct)},
        )
    entries = registry.entries()
    assert len(entries) == 1
    assert entries[0].payload["rate_limits"]["five_hour"]["used_percentage"] == 30


def test_remote_reading_feeds_the_provider(tmp_path):
    registry = RemoteClaudeRegistry()
    now = datetime.now(timezone.utc)
    registry.report(
        "oracle-edge",
        {"statusline": statusline_payload(captured_at=now, five_hour=42.0, seven_day=17.0)},
    )
    provider = make_provider(registry, tmp_path)
    parsed = provider._read_remote_sources(now=now)
    assert len(parsed) == 1
    name, observed_at, windows, reason = parsed[0]
    assert reason is None
    assert name == "remote:oracle-edge/statusline"
    assert observed_at == now.replace(microsecond=now.microsecond)
    assert {w.key for w in windows} >= {"5h", "1w"}


def test_stale_remote_reading_is_rejected_by_the_age_gate(tmp_path):
    registry = RemoteClaudeRegistry()
    now = datetime.now(timezone.utc)
    old = now - timedelta(seconds=7 * 3600)  # older than the 6 h default
    registry.report(
        "oracle-edge",
        {"statusline": statusline_payload(captured_at=old, five_hour=42.0, seven_day=17.0)},
    )
    provider = make_provider(registry, tmp_path)
    _name, _observed_at, windows, reason = provider._read_remote_sources(now=now)[0]
    assert windows == []
    assert reason is not None and "old" in reason


def test_newest_observation_wins_across_hosts(tmp_path):
    """The whole point: whichever machine observed most recently is believed,
    regardless of which one pushed last."""
    from app.providers.claude import merge_official_windows

    registry = RemoteClaudeRegistry()
    now = datetime.now(timezone.utc)
    stale = now - timedelta(minutes=30)

    # The fresher reading is pushed FIRST, so ordering by arrival would lose.
    registry.report(
        "company",
        {"statusline": statusline_payload(captured_at=now, five_hour=90.0, seven_day=80.0)},
    )
    registry.report(
        "oracle-edge",
        {"statusline": statusline_payload(captured_at=stale, five_hour=10.0, seven_day=5.0)},
    )

    provider = make_provider(registry, tmp_path)
    sources = [
        (name, observed_at, windows)
        for name, observed_at, windows, reason in provider._read_remote_sources(now=now)
        if reason is None
    ]
    windows, contributing, newest = merge_official_windows(sources, now=now)
    by_key = {w.key: w for w in windows}
    assert by_key["5h"].used_percent == pytest.approx(90.0)
    assert newest == now
    assert contributing == ["remote:company/statusline"]


def test_malformed_remote_payload_does_not_break_the_card(tmp_path):
    registry = RemoteClaudeRegistry()
    now = datetime.now(timezone.utc)
    registry.report("oracle-edge", {"statusline": {"totally": "wrong"}})
    provider = make_provider(registry, tmp_path)
    _name, _observed_at, windows, reason = provider._read_remote_sources(now=now)[0]
    assert windows == []
    assert reason is not None


def test_clear_removes_one_host_or_all():
    registry = RemoteClaudeRegistry()
    now = datetime.now(timezone.utc)
    for host in ("a", "b"):
        registry.report(host, {"statusline": statusline_payload(captured_at=now, five_hour=1, seven_day=1)})
    assert registry.clear("a") == 1
    assert registry.snapshot()["host_count"] == 1
    assert registry.clear() == 1
    assert registry.entries() == []
