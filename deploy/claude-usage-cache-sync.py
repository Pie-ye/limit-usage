#!/usr/bin/env python3
"""Copy Claude Code's usage cache out of ~/.claude.json.

Claude Code writes ~/.claude.json by creating a new file and renaming it over
the old one, so a single-file bind mount into the container would stay pinned
to the pre-rewrite inode and go stale forever (the same trap as
~/.claude/.credentials.json). Instead this script reads the `cachedUsageUtilization`
key out of the config file and copies it into a file inside a directory the
container mounts, writing atomically (temp file + os.replace) so the container
never observes a half-written file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def sync(config_path: Path, out_path: Path) -> bool:
    config = json.loads(config_path.read_text())
    cached = config.get("cachedUsageUtilization")
    if not isinstance(cached, dict):
        return False

    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = None
        if isinstance(existing, dict) and existing.get("fetchedAtMs") == cached.get(
            "fetchedAtMs"
        ):
            return False

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + f".{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(cached))
    os.replace(tmp_path, out_path)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="~/.claude.json",
        help="Path to Claude Code's config/cache file (default: ~/.claude.json)",
    )
    parser.add_argument(
        "--out",
        default="~/.claude-monitor/state/claude-code-usage.json",
        help=(
            "Path to write the copied usage cache to "
            "(default: ~/.claude-monitor/state/claude-code-usage.json)"
        ),
    )
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser()
    out_path = Path(args.out).expanduser()

    try:
        sync(config_path, out_path)
    except Exception as exc:  # noqa: BLE001 - surfaced to stderr, not raised
        print(f"claude-usage-cache-sync: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
