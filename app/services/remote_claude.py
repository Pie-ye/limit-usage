"""Claude usage readings pushed in from other machines.

Claude's quota is per *account*, but every source this app can read is per
*machine*: the statusline capture only updates while a TUI renders on that host,
and ``~/.claude.json``'s usage cache only refreshes when a TUI there starts or
opens a dialog. Work done on a second machine therefore burns account-wide quota
that this host's files cannot see, and the card keeps serving the stale — and
crucially *optimistic* — number until ``CLAUDE_OFFICIAL_MAX_AGE_SECONDS`` finally
expires it. For a dispatcher picking models by remaining %, optimistic-and-stale
is the worst failure mode available: it routes traffic *towards* the pool that is
already exhausted, and only finds out via a 429.

So every machine pushes the two files it already has to ``POST /api/ingest/claude``
and this registry keeps the newest payload per (host, kind). Readings are stored
raw and parsed at read time by ``ClaudeProvider``, which already owns both
parsers — the wire format is therefore literally the two files, with no second
schema to drift.

Freshness comes from the timestamp *inside* the payload (``captured_at_epoch`` /
``fetchedAtMs``), never from arrival time, so a push delayed by a slow cron or a
flaky tunnel cannot masquerade as fresh, and no clock-skew allowance is needed
beyond what the local files already get.

State is in-memory, like ``FeedbackRegistry``: a restart falls back to local
files only, which is the safe direction.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.models import utcnow

# The two kinds map 1:1 onto ClaudeProvider's existing local parsers.
KIND_STATUSLINE = "statusline"
KIND_USAGE_CACHE = "usage_cache"
KINDS = (KIND_STATUSLINE, KIND_USAGE_CACHE)

# A tag for display and per-host replacement, not an identity claim — the
# Cloudflare Access service token / tailnet ACL is what actually authorises a
# push. Bounded so a typo'd hostname in a loop cannot grow the dict forever.
MAX_HOSTS = 32
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


@dataclass(frozen=True)
class RemoteReading:
    """One file, as it was on ``host`` when that host last pushed."""

    host: str
    kind: str
    payload: dict[str, Any]
    received_at: datetime

    @property
    def source_name(self) -> str:
        """Matches the local source naming so the card's ``source`` reads evenly."""
        return f"{self.host}/{self.kind}"


class RemoteClaudeRegistry:
    def __init__(self, max_hosts: int = MAX_HOSTS) -> None:
        self._lock = threading.Lock()
        self._readings: dict[tuple[str, str], RemoteReading] = {}
        self.max_hosts = max_hosts

    @staticmethod
    def validate_host(host: str) -> str:
        h = (host or "").strip()
        if not HOST_RE.match(h):
            raise ValueError(
                "host must be 1-63 chars of letters, digits, dot, dash or underscore"
            )
        return h

    def report(
        self,
        host: str,
        readings: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Replace ``host``'s stored readings. Unknown kinds are rejected loudly
        rather than ignored, so a renamed field in a push agent surfaces as a 422
        instead of a card that silently stops updating."""
        host = self.validate_host(host)
        received_at = now or utcnow()
        accepted: list[str] = []
        for kind, payload in readings.items():
            if kind not in KINDS:
                raise ValueError(f"unknown reading kind: {kind}")
            if payload is None:
                continue
            if not isinstance(payload, dict):
                raise ValueError(f"{kind} payload must be a JSON object")
            accepted.append(kind)

        if not accepted:
            raise ValueError(f"no readings supplied (expected one of {', '.join(KINDS)})")

        with self._lock:
            known = {h for h, _ in self._readings}
            if host not in known and len(known) >= self.max_hosts:
                raise ValueError(
                    f"too many hosts ({self.max_hosts}); drop stale ones via "
                    "DELETE /api/ingest/claude"
                )
            for kind in accepted:
                self._readings[(host, kind)] = RemoteReading(
                    host=host,
                    kind=kind,
                    payload=readings[kind],
                    received_at=received_at,
                )
        return {"host": host, "accepted": accepted, "received_at": received_at.isoformat()}

    def entries(self) -> list[RemoteReading]:
        with self._lock:
            return list(self._readings.values())

    def clear(self, host: str | None = None) -> int:
        with self._lock:
            if host is None:
                count = len(self._readings)
                self._readings.clear()
                return count
            keys = [key for key in self._readings if key[0] == host]
            for key in keys:
                del self._readings[key]
            return len(keys)

    def snapshot(self) -> dict[str, Any]:
        """Per-host view for debugging a push agent that has gone quiet."""
        hosts: dict[str, Any] = {}
        for reading in self.entries():
            hosts.setdefault(reading.host, {})[reading.kind] = {
                "received_at": reading.received_at.isoformat(),
            }
        return {"hosts": hosts, "host_count": len(hosts)}
