#!/usr/bin/env python3
"""Export vendor CLI model lists from the host for limit-usage.

The limit-usage container has none of the codex, agy, or grok CLIs and cannot
see the host's ~/.codex/ cache. Run this script on the host to collect their
currently available model IDs into the container-mounted data directory. The
output is written atomically so the container never observes partial JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path


def parse_agy(text: str) -> list[str]:
    """Extract model IDs from the tab-separated output of ``agy models``."""
    models: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if "\t" not in line:
            continue
        model = line.split("\t", 1)[0].strip()
        if model and model not in seen:
            seen.add(model)
            models.append(model)
    return models


def parse_grok(text: str) -> list[str]:
    """Extract model IDs listed after the ``Available models:`` heading."""
    models: list[str] = []
    seen: set[str] = set()
    in_models = False
    for line in text.splitlines():
        if not in_models:
            if line.strip() == "Available models:":
                in_models = True
            continue
        match = re.match(r"^\s*[*-]\s+(\S+)", line)
        if match is None:
            continue
        model = match.group(1)
        if model not in seen:
            seen.add(model)
            models.append(model)
    return models


def parse_codex_cache(data: dict) -> list[str]:
    """Extract all string model slugs from a Codex model cache."""
    entries = data.get("models")
    if not isinstance(entries, list):
        return []

    models: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model = entry.get("slug")
        if isinstance(model, str) and model not in seen:
            seen.add(model)
            models.append(model)
    return models


def _error_message(exc: Exception) -> str:
    return " ".join(str(exc).splitlines())[:200]


def _result(loader: Callable[[], list[str]]) -> dict:
    try:
        models = loader()
    except Exception as exc:  # noqa: BLE001 - each vendor must fail independently
        return {"ok": False, "error": _error_message(exc)}
    if not models:
        return {"ok": False, "error": "no models parsed"}
    return {"ok": True, "models": models}


def collect(
    *,
    codex_cache: Path,
    run: Callable[[list[str]], str],
    now: datetime,
) -> dict:
    """Collect model lists while isolating failures between vendors."""
    generated_at = (
        now.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    def load_codex() -> list[str]:
        data = json.loads(codex_cache.read_text(encoding="utf-8"))
        return parse_codex_cache(data)

    return {
        "generated_at": generated_at,
        "vendors": {
            "codex": _result(load_codex),
            "agy": _result(lambda: parse_agy(run(["agy", "models"]))),
            "grok": _result(lambda: parse_grok(run(["grok", "models"]))),
        },
    }


def write_atomic(path: Path, payload: dict) -> None:
    """Write JSON to *path* atomically using a temporary sibling file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=path.parent,
            prefix=".cli-models.",
            suffix=".tmp",
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(json.dumps(payload, ensure_ascii=False, indent=2))
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    except BaseException:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "out",
        nargs="?",
        default="~/Container/limit-usage/data/cli-models.json",
    )
    parser.add_argument(
        "--codex-cache",
        default="~/.codex/models_cache.json",
    )
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args(argv)

    out_path = Path(args.out).expanduser()
    codex_cache = Path(args.codex_cache).expanduser()
    timeout = args.timeout
    env = os.environ.copy()
    local_bin = str(Path("~/.local/bin").expanduser())
    env["PATH"] = os.pathsep.join(part for part in (env.get("PATH", ""), local_bin) if part)

    def run(cmd: list[str]) -> str:
        if shutil.which(cmd[0], path=env["PATH"]) is None:
            raise RuntimeError(f"{cmd[0]} not found")
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"{cmd[0]} timed out after {timeout}s"
            ) from exc
        if completed.returncode != 0:
            stderr_lines = (completed.stderr or "").splitlines()
            stderr_last_line = stderr_lines[-1] if stderr_lines else ""
            raise RuntimeError(
                f"{cmd[0]} exited {completed.returncode}: {stderr_last_line}"
            )
        return completed.stdout

    payload = collect(
        codex_cache=codex_cache,
        run=run,
        now=datetime.now(timezone.utc),
    )
    try:
        write_atomic(out_path, payload)
    except Exception as exc:  # noqa: BLE001 - command reports write failures via exit code
        print(f"cli-models: write FAILED: {_error_message(exc)}", file=sys.stderr)
        return 1

    for vendor, result in payload["vendors"].items():
        if result["ok"]:
            print(
                f"cli-models: {vendor} ok ({len(result['models'])} models)",
                file=sys.stderr,
            )
        else:
            print(
                f"cli-models: {vendor} FAILED: {result['error']}",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
