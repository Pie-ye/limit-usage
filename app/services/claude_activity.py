"""How recently Claude Code did any work on this machine.

Background: the two local quota files stopped updating on 2026-09-05 because
Claude Code only refreshes them from an interactive TUI, so the card runs
entirely on the OAuth endpoint. That endpoint is rate limited *per account* and
shared with every other Claude Code session the account has open — a 429 comes
back with ``Retry-After: 3600``, parking the card for an hour — so it is polled
on a deliberately wide interval (30 min by default). That interval is the whole
of the lag you see.

Polling faster all the time would trade a 30-minute lag for an hourly outage.
But the number only moves while somebody is actually spending quota, and that
*is* observable locally and instantly: Claude Code appends to
``~/.claude/projects/**/*.jsonl`` once per API response, including from headless,
ACP and subagent sessions that never render a status line. Measured lag from
response to bytes on disk is about two seconds.

So this module answers one cheap question — "when did anything last happen?" —
by taking the newest mtime under the projects tree. It deliberately does **not**
read or parse those files:

* Quota percentages cannot be derived from them. Measured against 13k historical
  readings, tokens-per-percent varied 34x within a single model, and one 5-hour
  window went 50% -> 100% with *zero* local JSONL activity, because the account's
  quota is also spent from other machines and surfaces. Any percentage inferred
  here would be silently wrong in exactly the multi-machine case this deployment
  has.
* Parsing them is expensive (194 files / 97 MiB here, and growing without bound).

An mtime scan is O(files) stat calls with no reads, and the result is cached for
a few seconds so a burst of polls costs one scan.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Treat the account as "someone is working" for this long after the last write.
# Generous relative to a turn, so the gaps while a model thinks do not flap the
# interval back and forth.
ACTIVE_WINDOW_SECONDS = 900

# Re-scan at most this often; a poll cycle is 60 s, so this only collapses bursts.
SCAN_CACHE_SECONDS = 15

# Ignore absurd mtimes from a clock skew or a restored backup rather than letting
# one bad file pin the provider into fast-poll mode forever.
MAX_FUTURE_SKEW_SECONDS = 300


class ClaudeActivityProbe:
    def __init__(
        self,
        projects_dir: Path | None,
        *,
        active_window_seconds: int = ACTIVE_WINDOW_SECONDS,
        cache_seconds: float = SCAN_CACHE_SECONDS,
    ) -> None:
        self.projects_dir = projects_dir
        self.active_window_seconds = active_window_seconds
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached_at: float = 0.0
        self._cached_mtime: float | None = None

    def _scan(self) -> float | None:
        """Newest .jsonl mtime under the projects tree, or None."""
        if self.projects_dir is None or not self.projects_dir.is_dir():
            return None
        newest: float | None = None
        now = time.time()
        for root, _dirs, files in os.walk(self.projects_dir):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                try:
                    mtime = os.stat(os.path.join(root, name)).st_mtime
                except OSError:
                    continue
                if mtime > now + MAX_FUTURE_SKEW_SECONDS:
                    continue
                if newest is None or mtime > newest:
                    newest = mtime
        return newest

    def last_activity_epoch(self) -> float | None:
        with self._lock:
            now = time.monotonic()
            if self._cached_at and (now - self._cached_at) < self.cache_seconds:
                return self._cached_mtime
            try:
                self._cached_mtime = self._scan()
            except Exception as exc:  # never let a probe take the card down
                logger.warning("Claude activity scan failed: %s", exc)
                self._cached_mtime = None
            self._cached_at = now
            return self._cached_mtime

    def age_seconds(self) -> float | None:
        mtime = self.last_activity_epoch()
        if mtime is None:
            return None
        return max(0.0, time.time() - mtime)

    def is_active(self) -> bool:
        """True when the account is plausibly spending quota right now.

        Unknown (no directory, empty tree, scan failed) is reported as *not*
        active, so a misconfigured path falls back to the slow, safe interval
        rather than hammering a rate-limited endpoint.
        """
        age = self.age_seconds()
        return age is not None and age <= self.active_window_seconds
