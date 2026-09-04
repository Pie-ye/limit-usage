from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

import httpx
import pytest
import respx

from app.models import SnapshotStatus
from app.providers.claude import (
    MONITOR_SOURCE,
    USAGE_URL,
    ClaudeProvider,
    extract_claude_oauth,
    format_subscription,
    format_tier,
    parse_claude_usage,
    parse_monitor_state,
    parse_retry_after,
    parse_scoped_usage,
)
from app.models import utcnow


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


def _monitor_state(
    *,
    generated_at: str | None = None,
    five_hour_confidence: str = "official",
    seven_day_confidence: str = "official",
) -> dict:
    """A trimmed `claude-monitor --once --write-state` snapshot (schema 1.0)."""
    return {
        "schema_version": "1.0",
        "generated_at": generated_at or utcnow().isoformat(),
        "tool": {"name": "claude-monitor", "version": "4.0.0"},
        "confidence": "official",
        "stale": False,
        "plan": "max5",
        "limits": {
            "five_hour": {
                "used_percentage": 12.0,
                "tokens_used": None,
                "token_limit": None,
                "resets_at": "2026-09-04T15:10:00+00:00",
                "resets_at_epoch": 1788534600,
                "source": {"kind": "statusline"},
                "confidence": five_hour_confidence,
            },
            "seven_day": {
                "used_percentage": 13.0,
                "tokens_used": 4200,
                "token_limit": 88000,
                "resets_at": None,
                "resets_at_epoch": 1788591600,
                "source": {"kind": "statusline"},
                "confidence": seven_day_confidence,
            },
        },
    }


def test_parse_monitor_state_official_windows():
    windows, reason = parse_monitor_state(_monitor_state())
    assert reason is None
    by_key = {w.key: w for w in windows}

    assert by_key["5h"].used_percent == 12.0
    assert by_key["5h"].remaining_percent == 88.0
    assert by_key["5h"].limit_window_seconds == 18000
    assert by_key["5h"].resets_at == datetime(2026, 9, 4, 15, 10, tzinfo=timezone.utc)

    assert by_key["1w"].used_percent == 13.0
    assert by_key["1w"].limit_window_seconds == 604800
    # No ISO resets_at on this window, so the epoch field is used instead.
    assert by_key["1w"].resets_at == datetime.fromtimestamp(1788591600, tz=timezone.utc)
    assert by_key["1w"].raw_extra["token_limit"] == 88000
    assert by_key["1w"].raw_extra["source_kind"] == "statusline"


def test_parse_monitor_state_drops_local_estimates():
    """claude-monitor's own token estimate can be off by >10x; only trust official."""
    windows, reason = parse_monitor_state(
        _monitor_state(five_hour_confidence="local_estimate")
    )
    assert [w.key for w in windows] == ["1w"]

    windows, reason = parse_monitor_state(
        _monitor_state(
            five_hour_confidence="local_estimate", seven_day_confidence="local_estimate"
        )
    )
    assert windows == []
    assert "official" in (reason or "")


def test_parse_monitor_state_rejects_stale_file():
    old = (utcnow() - timedelta(seconds=1200)).isoformat()
    windows, reason = parse_monitor_state(_monitor_state(generated_at=old), max_age_seconds=900)
    assert windows == []
    assert "stale" in (reason or "")


def test_parse_monitor_state_rejects_garbage():
    assert parse_monitor_state(None)[0] == []
    assert parse_monitor_state({})[0] == []
    assert parse_monitor_state({"generated_at": "not-a-date", "limits": {}})[0] == []


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


SCOPED_PAYLOAD = {
    "limits": [
        {"kind": "session", "percent": 99, "resets_at": "2026-09-04T15:10:00Z"},
        {"kind": "weekly_all", "percent": 99, "resets_at": "2026-09-05T05:00:00Z"},
        {
            "kind": "weekly_scoped",
            "percent": 9,
            "resets_at": "2026-09-05T05:00:00Z",
            "scope": {"model": {"id": None, "display_name": "Fable"}},
        },
    ]
}


def test_parse_scoped_usage_keeps_only_per_model_windows():
    windows = parse_scoped_usage(SCOPED_PAYLOAD)
    assert [w.key for w in windows] == ["1w-fable"]
    assert windows[0].used_percent == 9.0
    assert windows[0].label == "Claude · 週額度 (Fable)"


def test_parse_scoped_usage_ignores_payloads_without_scoped_limits():
    assert parse_scoped_usage({"limits": [{"kind": "session", "percent": 5}]}) == []
    assert parse_scoped_usage({"five_hour": {"utilization": 5.0}}) == []
    assert parse_scoped_usage(None) == []


def _write_monitor_state(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_prefers_monitor_state_over_api(tmp_path: Path):
    creds = _write_creds(tmp_path / "creds.json")
    state = _write_monitor_state(tmp_path / "latest.json", _monitor_state())
    route = respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json={}))
    provider = ClaudeProvider(creds, monitor_state_path=state, scoped_refresh_seconds=0)

    snap = await provider.fetch()

    assert snap.status == SnapshotStatus.OK
    assert snap.source == MONITOR_SOURCE
    assert {w.key for w in snap.windows} == {"5h", "1w"}
    assert snap.account_hint == "Claude (5x 額度)"
    # The whole point: the account-wide windows cost no API call.
    assert not route.called
    assert provider.min_interval_seconds == 60


@pytest.mark.asyncio
@respx.mock
async def test_scoped_supplement_adds_fable_window_and_is_rate_rationed(tmp_path: Path):
    creds = _write_creds(tmp_path / "creds.json")
    state = _write_monitor_state(tmp_path / "latest.json", _monitor_state())
    route = respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json=SCOPED_PAYLOAD))
    provider = ClaudeProvider(creds, monitor_state_path=state, scoped_refresh_seconds=1800)

    snap = await provider.fetch()
    by_key = {w.key: w for w in snap.windows}
    assert snap.source == MONITOR_SOURCE
    assert by_key["1w-fable"].used_percent == 9.0
    # The scoped call must not overwrite claude-monitor's account-wide numbers,
    # which are fresher than this payload's 99% placeholders.
    assert by_key["5h"].used_percent == 12.0
    assert by_key["1w"].used_percent == 13.0
    assert route.call_count == 1

    # Next poll is inside the refresh window: cached window, no second call.
    snap = await provider.fetch()
    assert {w.key for w in snap.windows} >= {"1w-fable"}
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_scoped_supplement_429_does_not_degrade_the_snapshot(tmp_path: Path):
    creds = _write_creds(tmp_path / "creds.json")
    state = _write_monitor_state(tmp_path / "latest.json", _monitor_state())
    respx.get(USAGE_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}))
    provider = ClaudeProvider(creds, monitor_state_path=state, scoped_refresh_seconds=1800)

    snap = await provider.fetch()

    # Still a healthy snapshot from claude-monitor, just without the Fable row.
    assert snap.status == SnapshotStatus.OK
    assert snap.source == MONITOR_SOURCE
    assert {w.key for w in snap.windows} == {"5h", "1w"}
    # Crucially, the supplement must not back the whole provider off.
    assert provider.retry_after_seconds is None
    assert provider.min_interval_seconds == 60


@pytest.mark.asyncio
@respx.mock
async def test_scoped_supplement_keeps_cached_window_when_refresh_fails(tmp_path: Path):
    creds = _write_creds(tmp_path / "creds.json")
    state = _write_monitor_state(tmp_path / "latest.json", _monitor_state())
    respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json=SCOPED_PAYLOAD))
    provider = ClaudeProvider(creds, monitor_state_path=state, scoped_refresh_seconds=1800)
    await provider.fetch()

    respx.get(USAGE_URL).mock(return_value=httpx.Response(500, text="boom"))
    provider._scoped_next_attempt_at = None  # force a refresh attempt now
    snap = await provider.fetch()

    assert snap.status == SnapshotStatus.OK
    assert {w.key for w in snap.windows} == {"5h", "1w", "1w-fable"}


@pytest.mark.asyncio
@respx.mock
async def test_scoped_cache_expires_once_too_old(tmp_path: Path):
    creds = _write_creds(tmp_path / "creds.json")
    state = _write_monitor_state(tmp_path / "latest.json", _monitor_state())
    respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json=SCOPED_PAYLOAD))
    provider = ClaudeProvider(
        creds, monitor_state_path=state, scoped_refresh_seconds=1800, scoped_max_age_seconds=7200
    )
    await provider.fetch()

    provider._scoped_fetched_at = utcnow() - timedelta(seconds=9000)
    snap = await provider.fetch()

    assert {w.key for w in snap.windows} == {"5h", "1w"}


@pytest.mark.asyncio
@respx.mock
async def test_oauth_fallback_seeds_the_scoped_cache(tmp_path: Path):
    """The fallback payload already has the scoped rows; don't pay for them twice."""
    creds = _write_creds(tmp_path / "creds.json")
    route = respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json=SCOPED_PAYLOAD))
    provider = ClaudeProvider(creds, monitor_state_path=tmp_path / "absent.json")

    snap = await provider.fetch()
    assert snap.source == "oauth/usage"
    assert route.call_count == 1
    assert [w.key for w in provider._scoped_windows] == ["1w-fable"]

    # Now that claude-monitor has a state file, the Fable row carries over
    # without another call.
    _write_monitor_state(tmp_path / "absent.json", _monitor_state())
    snap = await provider.fetch()
    assert snap.source == MONITOR_SOURCE
    assert {w.key for w in snap.windows} == {"5h", "1w", "1w-fable"}
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_works_without_credentials_file(tmp_path: Path):
    """Only the OAuth fallback needs credentials; a missing file must not blank the card."""
    state = _write_monitor_state(tmp_path / "latest.json", _monitor_state())
    provider = ClaudeProvider(tmp_path / "nope.json", monitor_state_path=state)

    snap = await provider.fetch()

    assert snap.status == SnapshotStatus.OK
    assert snap.source == MONITOR_SOURCE
    assert snap.account_hint is None


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_falls_back_to_api_when_state_stale(tmp_path: Path):
    creds = _write_creds(tmp_path / "creds.json")
    old = (utcnow() - timedelta(seconds=3600)).isoformat()
    state = _write_monitor_state(tmp_path / "latest.json", _monitor_state(generated_at=old))
    respx.get(USAGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={"limits": [{"kind": "session", "percent": 40, "resets_at": "2026-09-04T05:00:00Z"}]},
        )
    )
    provider = ClaudeProvider(creds, monitor_state_path=state)

    snap = await provider.fetch()

    assert snap.status == SnapshotStatus.OK
    assert snap.source == "oauth/usage"
    assert snap.windows[0].used_percent == 40.0
    assert provider.min_interval_seconds == 300


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_fallback_error_names_the_monitor_reason(tmp_path: Path):
    creds = _write_creds(tmp_path / "creds.json")
    respx.get(USAGE_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}))
    provider = ClaudeProvider(creds, monitor_state_path=tmp_path / "absent.json")

    snap = await provider.fetch()

    assert snap.status == SnapshotStatus.RATE_LIMITED
    assert "retry-after=3600" in (snap.message or "")
    assert "no claude-monitor state file" in (snap.message or "")


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
