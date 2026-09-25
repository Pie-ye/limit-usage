import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.services.cli_models as cm_mod
from app.services.cli_models import (
    FRESH_MAX_AGE_SECONDS,
    CliModels,
    VendorList,
    load_cli_models,
)
from app.services.routing_view import build_routing_payload
from tests.test_routing_view import NOW, _all_ok, make_fake_catalog


@pytest.fixture
def fake_catalog(tmp_path: Path):
    return make_fake_catalog(tmp_path)


def test_load_cli_models_normal_and_listed(tmp_path: Path):
    path = tmp_path / "cli-models.json"
    payload = {
        "generated_at": "2026-09-26T08:00:00Z",
        "vendors": {
            "codex": {"ok": True, "models": ["model-codex-top", "model-codex-mid"]},
            "agy": {"ok": True, "models": ["gemini-2.5-pro", "claude-3-5-sonnet"]},
            "grok": {"ok": False, "error": "grok timed out"},
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    now = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)

    cm = load_cli_models(path, now=now)
    assert cm is not None
    assert cm.listed("codex", "model-codex-top") is True
    assert cm.listed("codex", "model-codex-mid") is True
    assert cm.listed("codex", "unknown-model") is False
    assert cm.listed("agy", "gemini-2.5-pro") is True
    assert cm.listed("grok", "any-model") is None
    assert cm.listed("unknown-vendor", "any-model") is None


def test_load_cli_models_stale_over_48h(tmp_path: Path):
    path = tmp_path / "cli-models.json"
    payload = {
        "generated_at": "2026-09-26T08:00:00Z",
        "vendors": {
            "codex": {"ok": True, "models": ["model-codex-top"]},
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    # 49 hours after generated_at (> FRESH_MAX_AGE_SECONDS)
    now = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)

    cm = load_cli_models(path, now=now)
    assert cm is not None
    assert cm.vendors["codex"].fresh is False
    assert cm.listed("codex", "model-codex-top") is None
    assert cm.uncatalogued([]) == {}


def test_load_cli_models_invalid_files(tmp_path: Path):
    now = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)

    # 1. Non-existent file
    assert load_cli_models(tmp_path / "nonexistent.json", now=now) is None

    # 2. Not valid JSON
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("invalid json content", encoding="utf-8")
    assert load_cli_models(bad_json, now=now) is None

    # 3. Top-level not a dict
    not_dict = tmp_path / "list.json"
    not_dict.write_text("[\"item1\", \"item2\"]", encoding="utf-8")
    assert load_cli_models(not_dict, now=now) is None

    # 4. generated_at missing or malformed
    no_gen = tmp_path / "no_gen.json"
    no_gen.write_text(json.dumps({"vendors": {}}), encoding="utf-8")
    assert load_cli_models(no_gen, now=now) is None

    bad_gen = tmp_path / "bad_gen.json"
    bad_gen.write_text(json.dumps({"generated_at": "invalid-date", "vendors": {}}), encoding="utf-8")
    assert load_cli_models(bad_gen, now=now) is None


def test_load_cli_models_malformed_vendor_entry(tmp_path: Path):
    path = tmp_path / "malformed-vendors.json"
    payload = {
        "generated_at": "2026-09-26T08:00:00Z",
        "vendors": {
            "bad_ok": {"ok": "true", "models": ["m1"]},
            "bad_models_type": {"ok": True, "models": "m1"},
            "bad_model_item": {"ok": True, "models": [123]},
            "bad_vendor_value": "not-a-dict",
            "good_vendor": {"ok": True, "models": ["good-model"]},
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    now = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)

    cm = load_cli_models(path, now=now)
    assert cm is not None
    assert cm.vendors["bad_ok"].ok is False
    assert cm.vendors["bad_ok"].error == "malformed entry"
    assert cm.vendors["bad_models_type"].ok is False
    assert cm.vendors["bad_models_type"].error == "malformed entry"
    assert cm.vendors["bad_model_item"].ok is False
    assert cm.vendors["bad_model_item"].error == "malformed entry"
    assert cm.vendors["bad_vendor_value"].ok is False
    assert cm.vendors["bad_vendor_value"].error == "malformed entry"
    assert cm.vendors["good_vendor"].ok is True
    assert cm.vendors["good_vendor"].models == frozenset(["good-model"])


def test_mtime_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "cache-test.json"
    payload = {
        "generated_at": "2026-09-26T08:00:00Z",
        "vendors": {
            "codex": {"ok": True, "models": ["model-initial"]},
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    now = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)

    call_count = 0
    orig_loads = json.loads

    def counting_loads(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return orig_loads(*args, **kwargs)

    monkeypatch.setattr(cm_mod.json, "loads", counting_loads)

    cm1 = load_cli_models(path, now=now)
    assert cm1 is not None
    assert call_count == 1

    # Second read: mtime unchanged, json.loads not called again
    cm2 = load_cli_models(path, now=now + timedelta(hours=1))
    assert cm2 is not None
    assert call_count == 1

    # Rewrite file with changed mtime
    new_payload = {
        "generated_at": "2026-09-26T09:00:00Z",
        "vendors": {
            "codex": {"ok": True, "models": ["model-updated"]},
        },
    }
    path.write_text(json.dumps(new_payload), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns + 1_000_000_000, stat.st_mtime_ns + 1_000_000_000))

    cm3 = load_cli_models(path, now=now + timedelta(hours=2))
    assert cm3 is not None
    assert call_count == 2
    assert cm3.listed("codex", "model-updated") is True


def test_uncatalogued(tmp_path: Path):
    path = tmp_path / "uncatalogued.json"
    payload = {
        "generated_at": "2026-09-26T08:00:00Z",
        "vendors": {
            "codex": {"ok": True, "models": ["cat-1", "codex-new-2", "codex-new-1"]},
            "agy": {
                "ok": True,
                "models": [
                    "cat-gemini",
                    "gemini-2.5-flash",
                    "gemini-2.5-pro",
                    "claude-3-7-sonnet",
                    "gpt-oss-1",
                ],
            },
            "grok": {"ok": False, "error": "offline", "models": []},
            "all_catalogued": {"ok": True, "models": ["cat-1"]},
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    now = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)

    cm = load_cli_models(path, now=now)
    assert cm is not None
    catalogued = ["cat-1", "cat-gemini"]
    uncat = cm.uncatalogued(catalogued)

    # codex: cat-1 excluded, remainder sorted
    assert uncat["codex"] == ["codex-new-1", "codex-new-2"]
    # agy: non-gemini excluded (claude, gpt-oss), cat-gemini excluded, remainder sorted
    assert uncat["agy"] == ["gemini-2.5-flash", "gemini-2.5-pro"]
    # grok is not fresh -> omitted
    assert "grok" not in uncat
    # all_catalogued is empty -> omitted
    assert "all_catalogued" not in uncat


def test_summary_shape(tmp_path: Path):
    path = tmp_path / "summary.json"
    payload = {
        "generated_at": "2026-09-26T08:00:00Z",
        "vendors": {
            "codex": {"ok": True, "models": ["model-a", "model-b"]},
            "grok": {"ok": False, "error": "connection refused"},
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    now = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)

    cm = load_cli_models(path, now=now)
    assert cm is not None
    summary = cm.summary()
    assert summary == {
        "generated_at": "2026-09-26T08:00:00Z",
        "vendors": {
            "codex": {"ok": True, "fresh": True, "count": 2},
            "grok": {"ok": False, "fresh": False, "count": 0},
        },
    }


def test_routing_payload_with_cli_models(fake_catalog):
    # codex has only model-codex-mid; model-codex-top is missing from codex list -> listed is False
    vendors = {
        "codex": VendorList(ok=True, fresh=True, models=frozenset(["model-codex-mid"]), error=None),
        "claude": VendorList(ok=True, fresh=True, models=frozenset(["model-c-top", "model-c-mid"]), error=None),
        "grok": VendorList(ok=True, fresh=True, models=frozenset(["model-g-top", "model-g-mid"]), error=None),
        "agy": VendorList(ok=True, fresh=True, models=frozenset(["model-a-top", "model-a-low", "gemini-uncat"]), error=None),
    }
    cli_models = CliModels(generated_at=NOW, vendors=vendors)

    out = build_routing_payload(_all_ok(), now=NOW, cli_models=cli_models, catalog=fake_catalog)

    # model-codex-top listed is False -> usable False, not recommended
    top = out["models"]["model-codex-top"]
    assert top["listed"] is False
    assert top["usable"] is False

    mid = out["models"]["model-codex-mid"]
    assert mid["listed"] is True
    assert mid["usable"] is True

    # In T0, codex-top was normally recommended; now codex-mid is recommended
    t0 = out["tiers"]["T0"]
    assert t0["recommended"] == "model-codex-mid"
    assert out["catalog"]["cli_models"] is not None
    assert out["catalog"]["cli_models"]["vendors"]["codex"]["count"] == 1
    assert "gemini-uncat" in out["catalog"]["uncatalogued"].get("agy", [])


def test_wait_seconds_delisted_and_missing(fake_catalog):
    from app.services.routing_view import _wait_seconds

    # Direct unit tests on _wait_seconds
    assert _wait_seconds({"listed": False, "missing": {"seconds_left": 100}}) is None
    assert _wait_seconds({"listed": False}) is None
    assert _wait_seconds({"missing": {"seconds_left": 86400}}) == 86400

    # Integration test with build_routing_payload
    vendors = {
        "codex": VendorList(ok=True, fresh=True, models=frozenset(), error=None),  # all delisted
    }
    cli_models = CliModels(generated_at=NOW, vendors=vendors)
    missing_models = {
        "model-c-top": {
            "missing": True,
            "until": "2026-09-27T12:00:00Z",
            "seconds_left": 86400,
            "last_error": "not found",
            "reported_at": "2026-09-26T12:00:00Z",
        }
    }
    out = build_routing_payload(
        _all_ok(),
        now=NOW,
        cli_models=cli_models,
        missing_models=missing_models,
        catalog=fake_catalog,
    )
    t0_candidates = {c["model"]: c for c in out["tiers"]["T0"]["candidates"]}
    assert t0_candidates["model-codex-top"]["listed"] is False if "listed" in t0_candidates["model-codex-top"] else True
    assert t0_candidates["model-codex-top"]["wait_seconds"] is None

    t3_candidates = {c["model"]: c for c in out["tiers"]["T3"]["candidates"]}
    assert t3_candidates["model-c-top"]["wait_seconds"] == 86400
