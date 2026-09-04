from datetime import datetime, timezone
from pathlib import Path
import json

import httpx
import pytest
import respx

from app.models import SnapshotStatus
from app.providers.claude import (
    USAGE_URL,
    ClaudeProvider,
    extract_claude_oauth,
    format_subscription,
    format_tier,
    parse_claude_usage,
    parse_retry_after,
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


def test_parse_retry_after():
    assert parse_retry_after("120") == 120
    assert parse_retry_after("120.9") == 120
    assert parse_retry_after("0") == 0
    assert parse_retry_after("nope") is None
    assert parse_retry_after(None) is None


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
    assert by_key["5h"].limit_window_seconds == 18000

    assert by_key["1w"].used_percent == 3.0
    assert by_key["1w"].remaining_percent == 97.0
    assert by_key["1w"].limit_window_seconds == 604800

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

    assert "1w-opus" not in by_key
    assert by_key["5h"].used_percent == 33.0
    assert by_key["5h"].remaining_percent == 67.0
    assert by_key["5h"].resets_at == datetime(2026, 4, 11, 7, 0, tzinfo=timezone.utc)
    assert by_key["1w"].used_percent == 13.0
    assert by_key["1w-sonnet"].used_percent == 1.0


def test_parse_claude_usage_empty_payload():
    assert parse_claude_usage({}) == []


def _write_creds(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-test",
                    "rateLimitTier": "default_claude_max_5x",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


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


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_fetch_rate_limited_reads_retry_after(tmp_path: Path):
    creds = _write_creds(tmp_path / "creds.json")
    provider = ClaudeProvider(creds)
    respx.get(USAGE_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "180"}, text="rate limited")
    )
    snap = await provider.fetch()
    assert snap.status == SnapshotStatus.RATE_LIMITED
    assert snap.windows == []
    assert provider.retry_after_seconds == 180
    assert "retry-after=180" in (snap.message or "")


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_fetch_ok_parses_limits(tmp_path: Path):
    creds = _write_creds(tmp_path / "creds.json")
    provider = ClaudeProvider(creds)
    respx.get(USAGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "limits": [
                    {
                        "kind": "session",
                        "percent": 12,
                        "resets_at": "2026-09-04T05:00:00Z",
                    },
                    {
                        "kind": "weekly_all",
                        "percent": 4,
                        "resets_at": "2026-09-07T05:00:00Z",
                    },
                ]
            },
        )
    )
    snap = await provider.fetch()
    assert snap.status == SnapshotStatus.OK
    by_key = {w.key: w for w in snap.windows}
    assert by_key["5h"].used_percent == 12.0
    assert by_key["1w"].remaining_percent == 96.0
    assert provider.retry_after_seconds is None
    request = respx.calls.last.request
    assert request.headers["authorization"] == "Bearer sk-ant-test"
    assert request.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert request.headers["anthropic-version"] == "2023-06-01"
