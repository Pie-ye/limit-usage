"""Tests for card_spend service."""

from __future__ import annotations

import json
from pathlib import Path

from app.services import card_spend


def test_read_card_spend_ok(tmp_path: Path) -> None:
    cache = tmp_path / "homepage-card-spend.json"
    cache.write_text(
        json.dumps(
            {
                "ok": True,
                "month": "2026-08",
                "month_total": 2543,
                "month_total_display": "NT$2,543",
                "count": 7,
                "currency": "TWD",
                "updated_at": "2026-08-18T00:00:00Z",
                "source": "2026月度總覽",
            }
        ),
        encoding="utf-8",
    )
    out = card_spend.read_card_spend(cache)
    assert out["ok"] is True
    assert out["status"] == "ok"
    assert out["month"] == "2026-08"
    assert out["month_total"] == 2543
    assert out["month_total_display"] == "NT$2,543"
    assert out["count"] == 7


def test_read_card_spend_builds_display(tmp_path: Path) -> None:
    cache = tmp_path / "homepage-card-spend.json"
    cache.write_text(
        json.dumps({"month": "2026-08", "month_total": 1000.4, "currency": "TWD"}),
        encoding="utf-8",
    )
    out = card_spend.read_card_spend(cache)
    assert out["month_total_display"] == "NT$1,000"


def test_read_card_spend_missing(tmp_path: Path) -> None:
    out = card_spend.read_card_spend(tmp_path / "missing.json")
    assert out["ok"] is False
    assert out["status"] == "error"
    assert out["month_total_display"] == "—"


def test_read_card_spend_normalizes_current_month_details(tmp_path: Path) -> None:
    cache = tmp_path / "homepage-card-spend.json"
    cache.write_text(
        json.dumps(
            {
                "year": "2026",
                "month": "2026-08",
                "month_total": 237,
                "count": 1,
                "currency": "TWD",
                "details": [
                    {
                        "transaction_date": "2026-08-13",
                        "category": "交通∕運輸",
                        "amount": "237",
                        "merchant": "不應輸出",
                        "card_last4": "5855",
                    },
                    {"transaction_date": "", "category": "壞資料", "amount": "nope"},
                ],
            }
        ),
        encoding="utf-8",
    )

    data = card_spend.read_card_spend(cache)

    assert data["year"] == "2026"
    assert data["details"] == [
        {
            "transaction_date": "2026-08-13",
            "category": "交通∕運輸",
            "amount": 237.0,
            "amount_display": "NT$237",
            "currency": "TWD",
        }
    ]
    assert "merchant" not in data["details"][0]
    assert "card_last4" not in data["details"][0]


def test_read_card_spend_old_cache_has_empty_details(tmp_path: Path) -> None:
    cache = tmp_path / "homepage-card-spend.json"
    cache.write_text(json.dumps({"month": "2026-08", "month_total": 100}), encoding="utf-8")

    data = card_spend.read_card_spend(cache)

    assert data["details"] == []
