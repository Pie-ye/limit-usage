from datetime import datetime, timezone

from app.providers.codex import classify_window, parse_rate_limit_windows


def test_classify_window_by_seconds():
    assert classify_window(18000) == ("5h", "5-hour")
    assert classify_window(604800) == ("1w", "Weekly")
    assert classify_window(17900)[0] == "5h"
    assert classify_window(604000)[0] == "1w"


def test_parse_primary_secondary_windows():
    payload = {
        "rate_limit": {
            "primary_window": {
                "limit_window_seconds": 18000,
                "percent_left": 62.5,
                "reset_at": "2026-07-11T12:00:00Z",
            },
            "secondary_window": {
                "limit_window_seconds": 604800,
                "percent_left": 40.0,
                "reset_at": 1784000000,
            },
        }
    }
    windows = parse_rate_limit_windows(payload)
    assert len(windows) >= 2
    by_key = {w.key: w for w in windows}
    assert by_key["5h"].remaining_percent == 62.5
    assert by_key["5h"].used_percent == 37.5
    assert by_key["5h"].resets_at == datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    assert by_key["1w"].remaining_percent == 40.0
    assert by_key["1w"].limit_window_seconds == 604800


def test_parse_used_percent_only():
    payload = {
        "rate_limit": {
            "primary_window": {
                "limit_window_seconds": 18000,
                "used_percent": 25,
            }
        }
    }
    windows = parse_rate_limit_windows(payload)
    assert windows[0].remaining_percent == 75.0
