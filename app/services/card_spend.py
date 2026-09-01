"""Read Cathay monthly card-spend cache produced by n8n."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CACHE_CANDIDATES = (
    Path("/secrets/card-spend/homepage-card-spend.json"),
    Path("/app/data/homepage-card-spend.json"),
    Path("/home/pieye/Container/n8n/data/homepage-card-spend.json"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_cache(cache_path: Path | None = None) -> Path | None:
    if cache_path is not None:
        return cache_path
    for candidate in DEFAULT_CACHE_CANDIDATES:
        if candidate.is_file():
            return candidate
    return DEFAULT_CACHE_CANDIDATES[0]


def _read_details(raw: Any, currency: str) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return details
    for item in raw:
        if not isinstance(item, dict):
            continue
        date = str(item.get("transaction_date") or "").strip()
        if not date:
            continue
        try:
            value = float(item.get("amount"))
        except (TypeError, ValueError):
            continue
        item_currency = str(item.get("currency") or currency)
        display = item.get("amount_display")
        if not display:
            display = f"NT${int(round(value)):,}" if item_currency == "TWD" else str(value)
        details.append(
            {
                "transaction_date": date,
                "category": str(item.get("category") or "未分類").strip() or "未分類",
                "amount": value,
                "amount_display": str(display),
                "currency": item_currency,
            }
        )
    details.sort(key=lambda item: item["transaction_date"], reverse=True)
    return details


def read_card_spend(cache_path: Path | None = None) -> dict[str, Any]:
    """Return flat monthly total fields. Never raises — degrades to status=error."""
    path = _resolve_cache(cache_path)
    now = _now()
    if path is None or not path.is_file():
        return {
            "ok": False,
            "status": "error",
            "display_name": "本月刷卡",
            "year": None,
            "month": None,
            "month_total": None,
            "month_total_display": "—",
            "count": None,
            "currency": "TWD",
            "details": [],
            "updated_at": now,
            "source": "n8n-cache",
            "message": "尚無刷卡總額快取（請執行 n8n「Homepage 當月刷卡總額」）",
        }

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "status": "error",
            "display_name": "本月刷卡",
            "year": None,
            "month": None,
            "month_total": None,
            "month_total_display": "—",
            "count": None,
            "currency": "TWD",
            "details": [],
            "updated_at": now,
            "source": "n8n-cache",
            "message": f"讀取快取失敗：{exc}",
        }

    if not isinstance(data, dict):
        return {
            "ok": False,
            "status": "error",
            "display_name": "本月刷卡",
            "year": None,
            "month": None,
            "month_total": None,
            "month_total_display": "—",
            "count": None,
            "currency": "TWD",
            "details": [],
            "updated_at": now,
            "source": "n8n-cache",
            "message": "快取格式錯誤",
        }

    month = str(data.get("month") or "") or None
    try:
        total = float(data.get("month_total")) if data.get("month_total") is not None else 0.0
    except (TypeError, ValueError):
        total = 0.0
    currency = str(data.get("currency") or "TWD")
    display = data.get("month_total_display")
    if not display:
        display = f"NT${int(round(total)):,}" if currency == "TWD" else str(total)
    count = data.get("count")
    try:
        count_n = int(float(count)) if count is not None else None
    except (TypeError, ValueError):
        count_n = None

    monthly_raw = data.get("monthly") or []
    monthly: list[dict[str, Any]] = []
    if isinstance(monthly_raw, list):
        for item in monthly_raw:
            if not isinstance(item, dict):
                continue
            mkey = str(item.get("month") or "").strip()
            if not mkey or mkey == "TOTAL":
                continue
            try:
                mtotal = float(item.get("total")) if item.get("total") is not None else 0.0
            except (TypeError, ValueError):
                mtotal = 0.0
            try:
                mcount = int(float(item.get("count"))) if item.get("count") is not None else 0
            except (TypeError, ValueError):
                mcount = 0
            mcurrency = str(item.get("currency") or currency)
            mdisplay = item.get("total_display")
            if not mdisplay:
                mdisplay = f"NT${int(round(mtotal)):,}" if mcurrency == "TWD" else str(mtotal)
            monthly.append(
                {
                    "month": mkey,
                    "month_num": str(item.get("month_num") or mkey[-2:]).zfill(2),
                    "count": mcount,
                    "total": mtotal,
                    "total_display": str(mdisplay),
                    "currency": mcurrency,
                }
            )
    monthly.sort(key=lambda x: x["month"], reverse=True)

    try:
        year_total = float(data.get("year_total")) if data.get("year_total") is not None else sum(m["total"] for m in monthly)
    except (TypeError, ValueError):
        year_total = sum(m["total"] for m in monthly)
    year_display = data.get("year_total_display") or (
        f"NT${int(round(year_total)):,}" if currency == "TWD" else str(year_total)
    )
    try:
        year_count = int(float(data.get("year_count"))) if data.get("year_count") is not None else sum(m["count"] for m in monthly)
    except (TypeError, ValueError):
        year_count = sum(m["count"] for m in monthly)
    details = _read_details(data.get("details") or [], currency)

    return {
        "ok": True,
        "status": "ok",
        "display_name": "本月刷卡",
        "year": str(data.get("year") or ""),
        "month": month,
        "month_total": total,
        "month_total_display": str(display),
        "count": count_n,
        "currency": currency,
        "year_total": year_total,
        "year_total_display": str(year_display),
        "year_count": year_count,
        "monthly": monthly,
        "details": details,
        "updated_at": data.get("updated_at") or now,
        "source": data.get("source") or "n8n-cache",
        "message": None,
    }
