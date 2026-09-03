from datetime import datetime, timezone
from pathlib import Path
import pytest

from app.models import SnapshotStatus
from app.providers.claude import (
    ClaudeProvider,
    extract_claude_oauth,
    format_subscription,
    format_tier,
    parse_claude_usage,
)


def test_format_subscription():
    assert format_subscription("max") == "Claude Max"
    assert format_subscription("pro") == "Claude Pro"
    assert format_subscription("team") == "Claude Team"
    assert format_subscription("free") == "Claude Free"
    assert format_subscription(None) == "未知"


def test_format_tier():
    assert format_tier("default_claude_max_5x") == "5x 額度"
    assert format_tier("default") == "標準"
    assert format_tier(None) == "標準"


def test_extract_claude_oauth_valid():
    sample = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-test",
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_5x",
            "expiresAt": 1756888102000,
            "scopes": ["user:file_upload", "user:inference"],
        }
    }
    status, msg, access_token, hint = extract_claude_oauth(sample)
    assert status == SnapshotStatus.OK
    assert access_token == "sk-ant-test"
    assert hint == "Claude (5x 額度)"


def test_extract_claude_oauth_missing_oauth_block():
    status, msg, access_token, hint = extract_claude_oauth({"other": {}})
    assert status == SnapshotStatus.AUTH_ERROR
    assert access_token is None


def test_extract_claude_oauth_missing_access_token():
    status, msg, access_token, hint = extract_claude_oauth(
        {"claudeAiOauth": {"rateLimitTier": "default"}}
    )
    assert status == SnapshotStatus.AUTH_ERROR
    assert access_token is None
    assert hint == "Claude (標準)"


def test_parse_claude_usage_limits_array_with_fable_scope():
    # Real shape returned by api.anthropic.com/api/oauth/usage: the top-level
    # seven_day_opus/seven_day_sonnet fields are null/deprecated, and the
    # per-model breakdown (e.g. Fable) lives in limits[] with kind
    # "weekly_scoped" instead.
    payload = {
        "five_hour": {"utilization": 15.0, "resets_at": "2026-09-03T10:20:00+00:00"},
        "seven_day": {"utilization": 3.0, "resets_at": "2026-09-05T05:00:00+00:00"},
        "seven_day_opus": None,
        "seven_day_sonnet": None,
        "limits": [
            {
                "kind": "session",
                "group": "session",
                "percent": 15,
                "resets_at": "2026-09-03T10:20:00.428916+00:00",
                "scope": None,
                "is_active": True,
            },
            {
                "kind": "weekly_all",
                "group": "weekly",
                "percent": 3,
                "resets_at": "2026-09-05T05:00:00.428942+00:00",
                "scope": None,
                "is_active": False,
            },
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 2,
                "resets_at": "2026-09-05T05:00:00.429331+00:00",
                "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
                "is_active": False,
            },
        ],
    }
    windows = parse_claude_usage(payload)
    by_key = {w.key: w for w in windows}

    assert by_key["5h"].used_percent == 15.0
    assert by_key["5h"].remaining_percent == 85.0

    assert by_key["1w"].used_percent == 3.0
    assert by_key["1w"].remaining_percent == 97.0

    assert by_key["1w-fable"].used_percent == 2.0
    assert by_key["1w-fable"].remaining_percent == 98.0
    assert by_key["1w-fable"].resets_at == datetime(
        2026, 9, 5, 5, 0, 0, 429331, tzinfo=timezone.utc
    )


def test_parse_claude_usage_falls_back_to_top_level_fields_without_limits():
    payload = {
        "five_hour": {"utilization": 33.0, "resets_at": "2026-04-11T07:00:00Z"},
        "seven_day": {"utilization": 13.0, "resets_at": "2026-04-17T00:59:59Z"},
        "seven_day_opus": None,
        "seven_day_sonnet": {"utilization": 1.0, "resets_at": "2026-04-17T00:59:59Z"},
    }
    windows = parse_claude_usage(payload)
    by_key = {w.key: w for w in windows}

    assert "1w-opus" not in by_key  # null entries are skipped
    assert by_key["5h"].used_percent == 33.0
    assert by_key["5h"].remaining_percent == 67.0
    assert by_key["5h"].resets_at == datetime(2026, 4, 11, 7, 0, tzinfo=timezone.utc)
    assert by_key["1w"].used_percent == 13.0
    assert by_key["1w-sonnet"].used_percent == 1.0


def test_parse_claude_usage_empty_payload():
    assert parse_claude_usage({}) == []


@pytest.mark.asyncio
async def test_claude_provider_fetch_missing(tmp_path: Path):
    missing_file = tmp_path / "not_found.json"
    provider = ClaudeProvider(missing_file)
    snap = await provider.fetch()
    assert snap.status == SnapshotStatus.AUTH_ERROR


@pytest.mark.asyncio
async def test_claude_provider_fetch_no_access_token(tmp_path: Path):
    creds = tmp_path / "creds.json"
    creds.write_text('{"claudeAiOauth": {"rateLimitTier": "default"}}', encoding="utf-8")
    provider = ClaudeProvider(creds)
    snap = await provider.fetch()
    assert snap.status == SnapshotStatus.AUTH_ERROR
    assert snap.windows == []
