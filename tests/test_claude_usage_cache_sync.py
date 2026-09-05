from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "claude-usage-cache-sync.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("claude_usage_cache_sync", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync_module = _load_module()


def _write_config(path: Path, cached_usage_utilization: dict | None) -> None:
    payload: dict = {}
    if cached_usage_utilization is not None:
        payload["cachedUsageUtilization"] = cached_usage_utilization
    path.write_text(json.dumps(payload))


def test_sync_extracts_cached_usage(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    out_path = tmp_path / "out.json"
    cached = {"fetchedAtMs": 1000, "five_hour": {"utilization": 12}}
    _write_config(config_path, cached)

    result = sync_module.sync(config_path, out_path)

    assert result is True
    assert json.loads(out_path.read_text()) == cached


def test_sync_skips_when_fetched_at_unchanged(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    out_path = tmp_path / "out.json"
    cached = {"fetchedAtMs": 1000, "five_hour": {"utilization": 12}}
    _write_config(config_path, cached)

    assert sync_module.sync(config_path, out_path) is True
    mtime_before = out_path.stat().st_mtime_ns

    result = sync_module.sync(config_path, out_path)

    assert result is False
    assert out_path.stat().st_mtime_ns == mtime_before


def test_sync_rewrites_when_fetched_at_changes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    out_path = tmp_path / "out.json"
    _write_config(config_path, {"fetchedAtMs": 1000, "five_hour": {"utilization": 12}})
    assert sync_module.sync(config_path, out_path) is True

    updated = {"fetchedAtMs": 2000, "five_hour": {"utilization": 42}}
    _write_config(config_path, updated)

    result = sync_module.sync(config_path, out_path)

    assert result is True
    assert json.loads(out_path.read_text()) == updated


def test_sync_without_cache_key_writes_nothing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    out_path = tmp_path / "out.json"
    _write_config(config_path, None)

    result = sync_module.sync(config_path, out_path)

    assert result is False
    assert not out_path.exists()


def test_sync_creates_parent_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    out_path = tmp_path / "nested" / "dir" / "out.json"
    cached = {"fetchedAtMs": 1000, "five_hour": {"utilization": 12}}
    _write_config(config_path, cached)

    result = sync_module.sync(config_path, out_path)

    assert result is True
    assert json.loads(out_path.read_text()) == cached


def test_sync_cleans_up_tmp_file_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    out_path = tmp_path / "out.json"
    cached = {"fetchedAtMs": 1000, "five_hour": {"utilization": 12}}
    _write_config(config_path, cached)

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(sync_module.os, "replace", _boom)

    with pytest.raises(OSError):
        sync_module.sync(config_path, out_path)

    tmp_path_candidate = out_path.with_name(out_path.name + f".{sync_module.os.getpid()}.tmp")
    assert not tmp_path_candidate.exists()
    assert not out_path.exists()


def test_main_reports_bad_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = tmp_path / "config.json"
    out_path = tmp_path / "out.json"
    config_path.write_text("not json")

    rc = sync_module.main(["--config", str(config_path), "--out", str(out_path)])

    assert rc == 1
    captured = capsys.readouterr()
    assert "claude-usage-cache-sync:" in captured.err
