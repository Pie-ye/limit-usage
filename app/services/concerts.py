"""Read Taiwan concert-watch cache produced by n8n."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

DEFAULT_CACHE_CANDIDATES = (
    Path("/secrets/concerts/homepage-concerts.json"),
    Path("/app/data/homepage-concerts.json"),
    Path("/home/pieye/Container/n8n/data/homepage-concerts.json"),
)

EVENT_FIELDS = (
    "id",
    "artist",
    "title",
    "venue",
    "show_date",
    "show_date_display",
    "sale_start",
    "sale_start_display",
    "url",
    "source",
    "stages",
    "is_new",
)
ALLOWED_STAGES = {"announced", "on_sale", "upcoming"}
STALE_AFTER_SECONDS = 93600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _resolve_cache(cache_path: Path | None = None) -> Path | None:
    if cache_path is not None:
        return cache_path
    for candidate in DEFAULT_CACHE_CANDIDATES:
        if candidate.is_file():
            return candidate
    return DEFAULT_CACHE_CANDIDATES[-1]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _read_events(raw: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return events
    for item in raw:
        if not isinstance(item, dict):
            continue
        artist = str(item.get("artist") or "").strip()
        title = str(item.get("title") or "").strip()
        if not artist or not title:
            continue
        stages = [s for s in item.get("stages") or [] if s in ALLOWED_STAGES]
        url = str(item.get("url") or "").strip()
        if url and not (url.startswith("https://") or url.startswith("http://") or url.startswith("/")):
            url = ""
        events.append(
            {
                "id": str(item.get("id") or f"{artist}:{title}"),
                "artist": artist,
                "title": title,
                "venue": str(item.get("venue") or "").strip(),
                "show_date": str(item.get("show_date") or "").strip(),
                "show_date_display": str(item.get("show_date_display") or "").strip(),
                "sale_start": item.get("sale_start") or None,
                "sale_start_display": str(item.get("sale_start_display") or "").strip(),
                "url": url,
                "source": str(item.get("source") or "").strip(),
                "stages": stages,
                "is_new": bool(item.get("is_new")),
            }
        )
    return events


def _error(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "error",
        "stale": True,
        "freshness_display": "錯誤",
        "display_name": "演唱會",
        "headline": "—",
        "headline_stage": "",
        "announced_count": 0,
        "on_sale_count": 0,
        "upcoming_count": 0,
        "announced_display": "宣布 —",
        "on_sale_display": "開賣 —",
        "upcoming_display": "即將 —",
        "events": [],
        "artists": [],
        "source_errors": [],
        "updated_at": _now(),
        "expires_at": None,
        "stale_after_seconds": STALE_AFTER_SECONDS,
        "age_seconds": None,
        "source": "n8n-cache",
        "message": message,
    }


def read_concerts(cache_path: Path | None = None) -> dict[str, Any]:
    """Return flat concert fields. Never raises — degrades to status=error."""
    path = _resolve_cache(cache_path)
    if path is None or not path.is_file():
        return _error("尚無演唱會快取（請執行 n8n「台灣演唱會追蹤」）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _error(f"讀取快取失敗：{exc}")
    if not isinstance(data, dict):
        return _error("快取格式錯誤")
    if data.get("ok") is False:
        return _error("演唱會快取標示為失敗")

    events = _read_events(data.get("events"))
    announced = _as_int(data.get("announced_count"), sum(1 for e in events if "announced" in e["stages"]))
    on_sale = _as_int(data.get("on_sale_count"), sum(1 for e in events if "on_sale" in e["stages"]))
    upcoming = _as_int(data.get("upcoming_count"), sum(1 for e in events if "upcoming" in e["stages"]))
    headline = str(data.get("headline") or "尚無來台場次").strip() or "尚無來台場次"

    updated_at_str = data.get("updated_at")
    updated_at_dt = _parse_iso(updated_at_str)

    expires_at_str = data.get("expires_at")
    expires_at_dt = _parse_iso(expires_at_str)

    stale_after_seconds = _as_int(data.get("stale_after_seconds"), STALE_AFTER_SECONDS)
    if stale_after_seconds <= 0:
        stale_after_seconds = STALE_AFTER_SECONDS

    if expires_at_dt is None and updated_at_dt is not None:
        expires_at_dt = updated_at_dt + timedelta(seconds=stale_after_seconds)

    now_dt = datetime.now(timezone.utc)

    if updated_at_dt is None:
        stale = True
        status = "stale"
        freshness_display = "已過期"
        age_seconds = None
    else:
        age_seconds = max(0, int((now_dt - updated_at_dt).total_seconds()))
        if expires_at_dt is not None and now_dt > expires_at_dt:
            stale = True
            status = "stale"
            freshness_display = "已過期"
        else:
            stale = False
            status = "ok"
            freshness_display = "正常"

    updated_at_out = updated_at_str if updated_at_str else _now()
    expires_at_out = expires_at_dt.isoformat().replace("+00:00", "Z") if expires_at_dt else None

    return {
        "ok": True,
        "status": status,
        "stale": stale,
        "freshness_display": freshness_display,
        "display_name": "演唱會",
        "headline": headline,
        "headline_stage": str(data.get("headline_stage") or ""),
        "announced_count": announced,
        "on_sale_count": on_sale,
        "upcoming_count": upcoming,
        "announced_display": str(data.get("announced_display") or f"宣布 {announced}"),
        "on_sale_display": str(data.get("on_sale_display") or f"開賣 {on_sale}"),
        "upcoming_display": str(data.get("upcoming_display") or f"即將 {upcoming}"),
        "events": events,
        "artists": [str(a) for a in (data.get("artists") or []) if str(a).strip()],
        "source_errors": [str(e) for e in (data.get("source_errors") or []) if str(e).strip()],
        "updated_at": updated_at_out,
        "expires_at": expires_at_out,
        "stale_after_seconds": stale_after_seconds,
        "age_seconds": age_seconds,
        "source": data.get("source") or "n8n-cache",
        "message": None,
    }
