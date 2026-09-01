"""Tests for concerts service."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.services import concerts


def test_read_concerts_ok(tmp_path: Path) -> None:
    cache = tmp_path / "homepage-concerts.json"
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    cache.write_text(
        json.dumps(
            {
                "ok": True,
                "headline": "IVE 9/11 演出",
                "upcoming_count": 2,
                "on_sale_count": 0,
                "announced_count": 1,
                "updated_at": now_iso,
                "events": [
                    {
                        "artist": "IVE",
                        "title": "SHOW WHAT I AM",
                        "url": "https://tixcraft.com/activity/detail/26_ive",
                        "stages": ["upcoming"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    out = concerts.read_concerts(cache)
    assert out["ok"] is True
    assert out["status"] == "ok"
    assert out["stale"] is False
    assert out["freshness_display"] == "正常"
    assert out["headline"] == "IVE 9/11 演出"
    assert out["upcoming_count"] == 2
    assert out["events"][0]["artist"] == "IVE"
    assert out["expires_at"] is not None
    assert out["stale_after_seconds"] == 93600


def test_read_concerts_rejects_explicit_failed_cache(tmp_path: Path) -> None:
    cache = tmp_path / "homepage-concerts.json"
    cache.write_text(
        json.dumps(
            {
                "ok": False,
                "artists": [],
                "events": [],
                "updated_at": "2026-09-01T01:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    out = concerts.read_concerts(cache)
    assert out["ok"] is False
    assert out["status"] == "error"
    assert out["stale"] is True
    assert out["message"] == "演唱會快取標示為失敗"


def test_read_concerts_keeps_genuine_empty_cache_fresh(tmp_path: Path) -> None:
    cache = tmp_path / "homepage-concerts.json"
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    cache.write_text(
        json.dumps(
            {
                "ok": True,
                "artists": [],
                "events": [],
                "headline": "目前沒有追蹤中的藝人",
                "updated_at": now_iso,
            }
        ),
        encoding="utf-8",
    )
    out = concerts.read_concerts(cache)
    assert out["ok"] is True
    assert out["status"] == "ok"
    assert out["stale"] is False
    assert out["headline"] == "目前沒有追蹤中的藝人"
    assert out["events"] == []


def test_read_concerts_keeps_legacy_cache_without_ok(tmp_path: Path) -> None:
    cache = tmp_path / "homepage-concerts.json"
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    cache.write_text(
        json.dumps(
            {
                "headline": "IVE 9/11 演出",
                "artists": ["IVE"],
                "events": [],
                "updated_at": now_iso,
            }
        ),
        encoding="utf-8",
    )
    out = concerts.read_concerts(cache)
    assert out["ok"] is True
    assert out["status"] == "ok"
    assert out["stale"] is False
    assert out["artists"] == ["IVE"]


def test_read_concerts_stale(tmp_path: Path) -> None:
    cache = tmp_path / "homepage-concerts.json"
    old_iso = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat().replace("+00:00", "Z")
    cache.write_text(
        json.dumps(
            {
                "ok": True,
                "headline": "IVE 9/11 演出",
                "upcoming_count": 2,
                "updated_at": old_iso,
                "events": [
                    {
                        "artist": "IVE",
                        "title": "SHOW WHAT I AM",
                        "stages": ["upcoming"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    out = concerts.read_concerts(cache)
    assert out["ok"] is True
    assert out["status"] == "stale"
    assert out["stale"] is True
    assert out["freshness_display"] == "已過期"
    assert out["headline"] == "IVE 9/11 演出"
    assert len(out["events"]) == 1


def test_read_concerts_missing(tmp_path: Path) -> None:
    out = concerts.read_concerts(tmp_path / "missing.json")
    assert out["ok"] is False
    assert out["status"] == "error"
    assert out["stale"] is True
    assert out["freshness_display"] == "錯誤"
    assert out["headline"] == "—"
