#!/usr/bin/env python3
"""Push this machine's Claude usage files to a limit-usage instance elsewhere.

Claude's quota is per *account*, but every file carrying it is per *machine*.
Work done on this host burns quota the limit-usage host cannot see, so its card
— and the ``/api/routing`` scores derived from it — keep reporting a stale,
optimistic number and keep routing work towards a pool that is already spent.
This script closes that gap by sending the two files limit-usage already knows
how to parse to ``POST /api/ingest/claude``.

Deliberately stdlib-only and single-file, so it can be dropped onto any host
that runs Claude Code — including one where you cannot install packages — and
driven by a systemd timer, cron, or Windows Task Scheduler.

Two sources, either or both:

* the claude-monitor statusline capture (official ``rate_limits``, but only
  written while an interactive TUI renders a status line), and
* ``cachedUsageUtilization`` read straight out of ``~/.claude.json`` — read
  directly rather than via the companion sync script so a remote host needs
  only this one file.

Every run pushes whatever it finds, even if unchanged: the server's registry is
in-memory, so skipping no-op pushes would leave this host's reading missing
until its numbers happened to move. The payload is a couple of KB.

Usage:
    claude-usage-push.py --url http://pipc.tail147fab.ts.net:50048
    claude-usage-push.py --url https://usage.piea.uk --dry-run

Cloudflare Access service tokens are read from CF_ACCESS_CLIENT_ID /
CF_ACCESS_CLIENT_SECRET when set.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://127.0.0.1:50048"
DEFAULT_STATUSLINE = "~/.claude-monitor/statusline/latest.json"
DEFAULT_CLAUDE_CONFIG = "~/.claude.json"


def read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, f"missing: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"unreadable {path}: {exc}"
    if not isinstance(data, dict):
        return None, f"not a JSON object: {path}"
    return data, None


def read_usage_cache(config_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Pull cachedUsageUtilization out of Claude Code's config file.

    Claude Code rewrites this file by rename, so reading it fresh each run is
    both correct and cheap — there is no handle to go stale.
    """
    config, err = read_json(config_path)
    if err is not None:
        return None, err
    cached = (config or {}).get("cachedUsageUtilization")
    if not isinstance(cached, dict):
        return None, f"no cachedUsageUtilization in {config_path}"
    return cached, None


def build_payload(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    notes: list[str] = []
    payload: dict[str, Any] = {"host": args.host}

    statusline, err = read_json(Path(args.statusline).expanduser())
    if err is not None:
        notes.append(f"statusline {err}")
    else:
        payload["statusline"] = statusline

    usage_cache, err = read_usage_cache(Path(args.claude_config).expanduser())
    if err is not None:
        notes.append(f"usage_cache {err}")
    else:
        payload["usage_cache"] = usage_cache

    return payload, notes


def post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "claude-usage-push/1.0",
    }
    client_id = os.environ.get("CF_ACCESS_CLIENT_ID", "").strip()
    client_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        headers["CF-Access-Client-Id"] = client_id
        headers["CF-Access-Client-Secret"] = client_secret

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("LIMIT_USAGE_URL", DEFAULT_URL),
        help=f"limit-usage base URL (env LIMIT_USAGE_URL, default {DEFAULT_URL})",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("LIMIT_USAGE_PUSH_HOST") or socket.gethostname(),
        help="name this machine reports as (default: system hostname)",
    )
    parser.add_argument("--statusline", default=DEFAULT_STATUSLINE)
    parser.add_argument("--claude-config", default=DEFAULT_CLAUDE_CONFIG)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be sent, contact nothing",
    )
    args = parser.parse_args(argv)

    payload, notes = build_payload(args)
    for note in notes:
        print(f"claude-usage-push: {note}", file=sys.stderr)

    if "statusline" not in payload and "usage_cache" not in payload:
        # Not an error: a host that has not run Claude Code yet has nothing to
        # say, and a timer should not spam failures over it.
        print("claude-usage-push: no readings on this host, nothing to push", file=sys.stderr)
        return 0

    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    url = args.url.rstrip("/") + "/api/ingest/claude"
    try:
        result = post(url, payload, args.timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        print(f"claude-usage-push: HTTP {exc.code} from {url}: {detail}", file=sys.stderr)
        if exc.code in (302, 401, 403):
            print(
                "claude-usage-push: looks like Cloudflare Access rejected this "
                "request — set CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET",
                file=sys.stderr,
            )
        return 1
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"claude-usage-push: cannot reach {url}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
