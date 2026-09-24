#!/usr/bin/env python3
"""Pass Claude Code's stream-json stdout through untouched, recording rate limits.

Headless Claude Code (``--output-format stream-json``, as spawned by
claude-agent-acp for codeg) never runs the statusLine hook, so nothing on disk
records the account's official windows. It does, however, print a
``rate_limit_event`` whenever a window's rounded percentage or reset moves, and
that event carries ``rate_limit_info.unifiedWindows`` read straight from the
``anthropic-ratelimit-unified-*`` response headers — no extra API call.

``deploy/claude-rate-tap`` pipes the real binary's stdout through this script.
Every byte is forwarded to our stdout as soon as it is read; only a side copy is
split into lines and inspected. Anything that goes wrong while inspecting or
writing the capture is swallowed: the Claude session must never notice the tap.

The capture uses the statusline capture format the provider already parses, so
it is just a third local source for the Claude card.
"""

from __future__ import annotations

import errno
import json
import math
import os
import sys
import time
from pathlib import Path

DEFAULT_CAPTURE = Path.home() / ".claude-monitor" / "stream" / "latest.json"
EVENT_PREFIX = b'{"type":"rate_limit_event"'
READ_SIZE = 65536
# A line longer than this cannot be a rate_limit_event; stop buffering it.
MAX_LINE = 1 << 20

# unifiedWindows key -> where it lands in the statusline capture format.
STANDARD_WINDOWS = {"five_hour": "five_hour", "seven_day": "seven_day"}
# The 7d_oi ("overage included") bucket is the per-model weekly limit, which is
# Fable on this account; the OAuth usage endpoint labels the same bucket
# weekly_scoped/Fable.
SCOPED_WINDOWS = {"seven_day_overage_included": "Fable"}


def _window(entry: object) -> tuple[float, int] | None:
    if not isinstance(entry, dict):
        return None
    utilization = entry.get("utilization")
    resets_at = entry.get("resetsAt")
    if isinstance(utilization, bool) or not isinstance(utilization, (int, float)):
        return None
    if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)):
        return None
    if not math.isfinite(utilization) or not math.isfinite(resets_at):
        return None
    return round(float(utilization) * 100, 1), int(resets_at)


def _still_open(entry: object, now: float) -> bool:
    if not isinstance(entry, dict):
        return False
    limit = entry.get("limit", entry)
    resets_at = limit.get("resets_at") if isinstance(limit, dict) else None
    return isinstance(resets_at, (int, float)) and resets_at > now


def merge_capture(existing: object, unified: dict, now: float) -> dict | None:
    """Fold one event's windows into the previous capture.

    A response only carries the buckets it touched (a Haiku call has no 7d_oi
    headers), so windows missing from this event keep their last value until
    their own reset passes.
    """
    previous = existing.get("rate_limits") if isinstance(existing, dict) else None
    previous = previous if isinstance(previous, dict) else {}

    rate_limits: dict = {}
    for field in STANDARD_WINDOWS.values():
        if _still_open(previous.get(field), now):
            rate_limits[field] = previous[field]
    scoped: dict[str, dict] = {}
    for entry in previous.get("model_scoped") or []:
        if _still_open(entry, now) and isinstance(entry.get("displayName"), str):
            scoped[entry["displayName"]] = entry

    seen = False
    for key, field in STANDARD_WINDOWS.items():
        window = _window(unified.get(key))
        if window is not None:
            rate_limits[field] = {"used_percentage": window[0], "resets_at": window[1]}
            seen = True
    for key, name in SCOPED_WINDOWS.items():
        window = _window(unified.get(key))
        if window is not None:
            scoped[name] = {
                "displayName": name,
                "limit": {"utilization": window[0], "resets_at": window[1]},
            }
            seen = True

    if not seen:
        return None
    if scoped:
        rate_limits["model_scoped"] = list(scoped.values())
    return {"captured_at_epoch": int(now), "source": "stream-json", "rate_limits": rate_limits}


def record(line: bytes, capture: Path) -> None:
    event = json.loads(line)
    info = event.get("rate_limit_info") if isinstance(event, dict) else None
    unified = info.get("unifiedWindows") if isinstance(info, dict) else None
    if not isinstance(unified, dict):
        return
    try:
        existing = json.loads(capture.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = None
    payload = merge_capture(existing, unified, time.time())
    if payload is None:
        return
    capture.parent.mkdir(parents=True, exist_ok=True)
    tmp = capture.with_name(f"{capture.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, capture)
    finally:
        tmp.unlink(missing_ok=True)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def main() -> int:
    capture = Path(os.environ.get("CLAUDE_RATE_TAP_CAPTURE") or DEFAULT_CAPTURE)
    pending = b""
    skipping = False
    while True:
        try:
            chunk = os.read(0, READ_SIZE)
        except InterruptedError:
            continue
        if not chunk:
            return 0
        try:
            _write_all(1, chunk)
        except OSError as exc:
            # Reader gone: nothing left to forward to. Exiting closes our stdin,
            # so the writer sees the same broken pipe it would have without us.
            if exc.errno in (errno.EPIPE, errno.EBADF):
                return 0
            raise

        pending += chunk
        *lines, pending = pending.split(b"\n")
        for line in lines:
            if skipping:  # tail of an oversized line we already dropped
                skipping = False
                continue
            if line.startswith(EVENT_PREFIX):
                try:
                    record(line, capture)
                except Exception:  # never let a bad event or full disk touch the stream
                    pass
        if len(pending) > MAX_LINE:
            pending = b""
            skipping = True


if __name__ == "__main__":
    sys.exit(main())
