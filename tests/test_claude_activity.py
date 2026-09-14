from __future__ import annotations

import os
import time

from app.providers.claude import (
    DEFAULT_OAUTH_ACTIVE_INTERVAL_SECONDS,
    DEFAULT_OAUTH_MIN_INTERVAL_SECONDS,
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
