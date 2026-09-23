from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import time

import httpx
import pytest
import respx

from app.providers.claude import (
    DEFAULT_OAUTH_ACTIVE_INTERVAL_SECONDS,
    DEFAULT_OAUTH_MIN_INTERVAL_SECONDS,
    USAGE_URL,
    ClaudeProvider,
)
from app.services.claude_activity import ClaudeActivityProbe


def write_transcript(root, rel: str, *, age_seconds: float = 0.0) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"assistant"}\n', encoding="utf-8")
    when = time.time() - age_seconds
    os.utime(path, (when, when))


def test_missing_directory_is_not_active(tmp_path):
    probe = ClaudeActivityProbe(tmp_path / "nope")
    assert probe.age_seconds() is None
    assert probe.is_active() is False


def test_empty_tree_is_not_active(tmp_path):
    (tmp_path / "projects").mkdir()
    probe = ClaudeActivityProbe(tmp_path / "projects")
    assert probe.is_active() is False


def test_recent_transcript_is_active(tmp_path):
    write_transcript(tmp_path, "proj/session.jsonl", age_seconds=5)
    probe = ClaudeActivityProbe(tmp_path, cache_seconds=0)
    assert probe.is_active() is True
    assert probe.age_seconds() < 60


def test_old_transcript_is_not_active(tmp_path):
    write_transcript(tmp_path, "proj/session.jsonl", age_seconds=4000)
    probe = ClaudeActivityProbe(tmp_path, cache_seconds=0)
    assert probe.is_active() is False


def test_subagent_transcripts_count(tmp_path):
    """Subagents live one directory deeper; they are the sessions that never
    render a status line, so missing them would defeat the point."""
    write_transcript(tmp_path, "proj/parent.jsonl", age_seconds=4000)
    write_transcript(tmp_path, "proj/parent/subagents/agent-abc.jsonl", age_seconds=3)
    probe = ClaudeActivityProbe(tmp_path, cache_seconds=0)
    assert probe.is_active() is True


def test_non_jsonl_files_are_ignored(tmp_path):
    write_transcript(tmp_path, "proj/old.jsonl", age_seconds=4000)
    other = tmp_path / "proj" / "fresh.log"
    other.write_text("x", encoding="utf-8")
    probe = ClaudeActivityProbe(tmp_path, cache_seconds=0)
    assert probe.is_active() is False


def test_future_mtime_is_ignored(tmp_path):
    """A restored backup or a skewed clock must not pin the provider into
    fast-poll mode forever."""
    write_transcript(tmp_path, "proj/skewed.jsonl", age_seconds=-86400)
    probe = ClaudeActivityProbe(tmp_path, cache_seconds=0)
    assert probe.is_active() is False


def test_scan_result_is_cached(tmp_path):
    write_transcript(tmp_path, "proj/a.jsonl", age_seconds=4000)
    probe = ClaudeActivityProbe(tmp_path, cache_seconds=60)
    assert probe.is_active() is False
    write_transcript(tmp_path, "proj/b.jsonl", age_seconds=1)
    assert probe.is_active() is False  # still the cached scan
    probe.cache_seconds = 0
    assert probe.is_active() is True


class StubProbe:
    def __init__(self, active: bool | Exception) -> None:
        self.active = active

    def is_active(self) -> bool:
        if isinstance(self.active, Exception):
            raise self.active
        return self.active


def make_provider(tmp_path, probe) -> ClaudeProvider:
    return ClaudeProvider(tmp_path / "creds.json", activity_probe=probe)


def test_interval_is_wide_when_idle(tmp_path):
    provider = make_provider(tmp_path, StubProbe(False))
    assert provider._oauth_interval() == DEFAULT_OAUTH_MIN_INTERVAL_SECONDS


def test_interval_tightens_when_active(tmp_path):
    provider = make_provider(tmp_path, StubProbe(True))
    assert provider._oauth_interval() == DEFAULT_OAUTH_ACTIVE_INTERVAL_SECONDS


def test_no_probe_keeps_the_old_behaviour(tmp_path):
    provider = ClaudeProvider(tmp_path / "creds.json")
    assert provider._oauth_interval() == DEFAULT_OAUTH_MIN_INTERVAL_SECONDS


def test_failing_probe_falls_back_to_the_safe_interval(tmp_path):
    """A broken probe must not push the caller towards a rate-limited endpoint."""
    provider = make_provider(tmp_path, StubProbe(RuntimeError("boom")))
    assert provider._oauth_interval() == DEFAULT_OAUTH_MIN_INTERVAL_SECONDS


def test_active_interval_never_exceeds_the_idle_one(tmp_path):
    provider = ClaudeProvider(
        tmp_path / "creds.json",
        oauth_min_interval_seconds=120,
        oauth_active_interval_seconds=600,  # misconfigured: "fast" is slower
        activity_probe=StubProbe(True),
    )
    assert provider._oauth_interval() == 120


def _write_creds(path):
    path.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok", "subscriptionType": "max"}}),
        encoding="utf-8",
    )
    return path


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest.mark.asyncio
@respx.mock
async def test_becoming_active_shortens_an_interval_chosen_while_idle(tmp_path, monkeypatch):
    """An OAuth call made while idle must not pin the next one 30 min out once
    Claude Code starts working again."""
    clock = Clock()
    monkeypatch.setattr("app.providers.claude.utcnow", clock)
    route = respx.get(USAGE_URL).mock(
        return_value=httpx.Response(
            200, json={"limits": [{"kind": "session", "percent": 10, "resets_at": "2026-09-24T00:00:00Z"}]}
        )
    )
    probe = StubProbe(False)
    provider = ClaudeProvider(_write_creds(tmp_path / "creds.json"), activity_probe=probe)
    await provider.fetch()
    assert route.call_count == 1

    probe.active = True
    clock.now += timedelta(seconds=DEFAULT_OAUTH_ACTIVE_INTERVAL_SECONDS - 1)
    await provider.fetch()
    assert route.call_count == 1

    clock.now += timedelta(seconds=2)
    await provider.fetch()
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_activity_never_shortens_a_rate_limit_backoff(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("app.providers.claude.utcnow", clock)
    route = respx.get(USAGE_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3600"})
    )
    provider = ClaudeProvider(
        _write_creds(tmp_path / "creds.json"), activity_probe=StubProbe(True)
    )
    await provider.fetch()
    clock.now += timedelta(seconds=3599)
    await provider.fetch()
    assert route.call_count == 1

    clock.now += timedelta(seconds=2)
    await provider.fetch()
    assert route.call_count == 2
