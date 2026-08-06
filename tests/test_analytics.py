from datetime import datetime, timedelta, timezone

from app.services.analytics import (
    compute_burn_estimate,
    extract_series_points,
    urgency_for_window,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def test_urgency_5h_never_warns():
    u = urgency_for_window(
        {
            "key": "5h",
            "label": "5-hour",
            "remaining_percent": 5,
            "used_percent": 95,
            "limit_window_seconds": 18000,
            "resets_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        }
    )
    assert u["level"] == "ok"


def test_urgency_weekly_low_remaining():
    u = urgency_for_window(
        {
            "key": "1w",
            "label": "Weekly",
            "remaining_percent": 15,
            "used_percent": 85,
            "limit_window_seconds": 604800,
            "resets_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
        }
    )
    assert u["level"] == "low"


def test_urgency_weekly_reset_soon():
    u = urgency_for_window(
        {
            "key": "weekly",
            "label": "Weekly",
            "remaining_percent": 80,
            "used_percent": 20,
            "limit_window_seconds": 604800,
            "resets_at": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
        }
    )
    assert u["level"] == "critical"


def test_urgency_deepseek_cny_under_10():
    u = urgency_for_window(
        {
            "key": "balance-cny",
            "amount": "8.5",
            "currency": "CNY",
        }
    )
    assert u["level"] in {"low", "critical"}
    assert any("10" in r for r in u["reasons"])


def test_urgency_deepseek_cny_ok_above_10():
    u = urgency_for_window(
        {
            "key": "balance-cny",
            "amount": "35.70",
            "currency": "CNY",
        }
    )
    assert u["level"] == "ok"


def test_burn_estimate_percent():
    now = datetime.now(timezone.utc)
    rows = []
    for i, used in enumerate([10, 20, 30, 40]):
        t = now - timedelta(hours=3 - i)
        rows.append(
            {
                "fetched_at": _iso(t),
                "snapshot": {
                    "fetched_at": _iso(t),
                    "windows": [
                        {
                            "key": "5h",
                            "label": "5-hour",
                            "used_percent": used,
                            "remaining_percent": 100 - used,
                        }
                    ],
                },
            }
        )
    pts = extract_series_points(rows, window_key="5h")
    est = compute_burn_estimate(pts, lookback_hours=24)
    assert est["ok"] is True
    assert est["burn_per_hour"] > 0
    assert est["tasks_remaining"]["medium"] is not None
    assert est["hours_until_empty"] is not None


def test_burn_estimate_balance():
    now = datetime.now(timezone.utc)
    rows = []
    for i, amt in enumerate([40.0, 35.0, 30.0]):
        t = now - timedelta(hours=2 - i)
        rows.append(
            {
                "fetched_at": _iso(t),
                "snapshot": {
                    "windows": [
                        {
                            "key": "balance-cny",
                            "label": "Balance (CNY)",
                            "amount": str(amt),
                            "currency": "CNY",
                        }
                    ],
                },
            }
        )
    pts = extract_series_points(rows, window_key="balance-cny")
    est = compute_burn_estimate(pts, lookback_hours=24)
    assert est["ok"] is True
    assert est["kind"] == "balance"
    assert est["tasks_remaining"]["medium"] is not None
