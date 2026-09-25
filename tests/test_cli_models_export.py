from __future__ import annotations

import importlib.util
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "deploy" / "cli-models-export.py"
SPEC = importlib.util.spec_from_file_location("cli_models_export", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
cli_models_export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli_models_export)

AGY_SAMPLE = """\
Fetching available models...
gemini-3.8-flash-high\tGemini 3.8 Flash (High)
gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)
gemini-3.8-flash-low\tGemini 3.8 Flash (Low)
gemini-3.7-flash-high\tGemini 3.7 Flash (High)
gemini-3.7-flash-medium\tGemini 3.7 Flash (Medium)
gemini-3.7-flash-low\tGemini 3.7 Flash (Low)
gemini-3.6-flash-high\tGemini 3.6 Flash (High)
gemini-3.6-flash-medium\tGemini 3.6 Flash (Medium)
gemini-3.6-flash-low\tGemini 3.6 Flash (Low)
gemini-3.1-pro-high\tGemini 3.1 Pro (High)
gemini-3.1-pro-low\tGemini 3.1 Pro (Low)
claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)
claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)
gpt-oss-120b-medium\tGPT-OSS 120B (Medium)
"""

GROK_SAMPLE = """\
You are logged in with grok.com.

Default model: grok-4.7

Available models:
  * grok-4.7 (default)
  - grok-4.7-build-fast
  - grok-4.6
  - grok-4.5
"""

CODEX_SAMPLE = {
    "fetched_at": "2026-09-25T16:21:33.816096276Z",
    "etag": "x",
    "client_version": "1",
    "models": [
        {"slug": "gpt-reserve", "visibility": "hide"},
        {"slug": "gpt-5.6-sol", "visibility": "list"},
        {"slug": "gpt-5.6-terra", "visibility": "list"},
        {"slug": "gpt-5.6-luna", "visibility": "list"},
        {"slug": "gpt-5.5", "visibility": "list"},
        {"slug": "codex-auto-review", "visibility": "hide"},
    ],
}


def _write_codex_cache(path: Path) -> None:
    path.write_text(json.dumps(CODEX_SAMPLE), encoding="utf-8")


def _sample_run(cmd: list[str]) -> str:
    return {"agy": AGY_SAMPLE, "grok": GROK_SAMPLE}[cmd[0]]


def test_parse_agy_sample() -> None:
    models = cli_models_export.parse_agy(AGY_SAMPLE)

    assert len(models) == 14
    assert models[0] == "gemini-3.8-flash-high"
    assert models[-1] == "gpt-oss-120b-medium"
    assert "Fetching available models..." not in models


def test_parse_grok_sample_and_missing_heading() -> None:
    assert cli_models_export.parse_grok(GROK_SAMPLE) == [
        "grok-4.7",
        "grok-4.7-build-fast",
        "grok-4.6",
        "grok-4.5",
    ]
    assert cli_models_export.parse_grok("  - grok-4.7\n") == []


def test_parse_codex_cache_sample_and_invalid_structures() -> None:
    assert cli_models_export.parse_codex_cache(CODEX_SAMPLE) == [
        "gpt-reserve",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "codex-auto-review",
    ]
    assert cli_models_export.parse_codex_cache({}) == []
    assert cli_models_export.parse_codex_cache({"models": "x"}) == []


@pytest.mark.parametrize(
    ("parser", "value", "expected"),
    [
        (
            cli_models_export.parse_agy,
            "model-a\tA\nmodel-a\tAgain\n",
            ["model-a"],
        ),
        (
            cli_models_export.parse_grok,
            "Available models:\n  - model-a\n  * model-a (default)\n",
            ["model-a"],
        ),
        (
            cli_models_export.parse_codex_cache,
            {"models": [{"slug": "model-a"}, {"slug": "model-a"}]},
            ["model-a"],
        ),
    ],
)
def test_parsers_remove_duplicate_ids(parser, value, expected: list[str]) -> None:
    assert parser(value) == expected


def test_collect_all_vendors(tmp_path: Path) -> None:
    cache = tmp_path / "models_cache.json"
    _write_codex_cache(cache)
    now = datetime(2026, 9, 26, 16, 0, 0, 987654, tzinfo=timezone(timedelta(hours=8)))

    payload = cli_models_export.collect(
        codex_cache=cache,
        run=_sample_run,
        now=now,
    )

    assert payload["generated_at"] == "2026-09-26T08:00:00Z"
    assert list(payload["vendors"]) == ["codex", "agy", "grok"]
    assert all(vendor["ok"] is True for vendor in payload["vendors"].values())
    assert payload["vendors"]["codex"]["models"] == [
        "gpt-reserve",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "codex-auto-review",
    ]


def test_collect_isolates_vendor_failures(tmp_path: Path) -> None:
    cache = tmp_path / "models_cache.json"
    _write_codex_cache(cache)

    def failing_grok(cmd: list[str]) -> str:
        if cmd[0] == "grok":
            raise RuntimeError("grok timed out after 60s")
        return AGY_SAMPLE

    payload = cli_models_export.collect(
        codex_cache=cache,
        run=failing_grok,
        now=datetime(2026, 9, 26, 8, tzinfo=timezone.utc),
    )

    assert payload["vendors"]["codex"]["ok"] is True
    assert payload["vendors"]["agy"]["ok"] is True
    assert payload["vendors"]["grok"] == {
        "ok": False,
        "error": "grok timed out after 60s",
    }


def test_collect_reports_missing_codex_cache(tmp_path: Path) -> None:
    payload = cli_models_export.collect(
        codex_cache=tmp_path / "missing.json",
        run=_sample_run,
        now=datetime(2026, 9, 26, 8, tzinfo=timezone.utc),
    )

    assert payload["vendors"]["codex"]["ok"] is False
    assert payload["vendors"]["agy"]["ok"] is True
    assert payload["vendors"]["grok"]["ok"] is True


def test_collect_reports_empty_agy_output(tmp_path: Path) -> None:
    cache = tmp_path / "models_cache.json"
    _write_codex_cache(cache)

    def empty_agy(cmd: list[str]) -> str:
        return "" if cmd[0] == "agy" else GROK_SAMPLE

    payload = cli_models_export.collect(
        codex_cache=cache,
        run=empty_agy,
        now=datetime(2026, 9, 26, 8, tzinfo=timezone.utc),
    )

    assert payload["vendors"]["agy"] == {
        "ok": False,
        "error": "no models parsed",
    }
    assert payload["vendors"]["codex"]["ok"] is True
    assert payload["vendors"]["grok"]["ok"] is True


def test_write_atomic_round_trip_without_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "cli-models.json"
    payload = {"generated_at": "2026-09-26T08:00:00Z", "vendors": {}}

    cli_models_export.write_atomic(path, payload)

    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert list(path.parent.glob(".cli-models.*.tmp")) == []


def test_write_atomic_cleans_up_if_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cli-models.json"
    path.write_text("original\n", encoding="utf-8")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(cli_models_export.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        cli_models_export.write_atomic(path, {"updated": True})

    assert path.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.glob(".cli-models.*.tmp")) == []


def test_main_writes_output_without_calling_real_clis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "models_cache.json"
    out = tmp_path / "cli-models.json"
    _write_codex_cache(cache)

    def fake_run(cmd: list[str], **kwargs) -> SimpleNamespace:
        stdout = {"agy": AGY_SAMPLE, "grok": GROK_SAMPLE}[cmd[0]]
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 60
        assert kwargs["stdin"] is subprocess.DEVNULL
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(cli_models_export.shutil, "which", lambda *args, **kwargs: "/bin/cli")
    monkeypatch.setattr(cli_models_export.subprocess, "run", fake_run)

    result = cli_models_export.main(
        [str(out), "--codex-cache", str(cache)]
    )

    assert result == 0
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert all(vendor["ok"] is True for vendor in payload["vendors"].values())
