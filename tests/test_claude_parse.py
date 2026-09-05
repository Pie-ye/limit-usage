from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx
import pytest
import respx

from app.models import SnapshotStatus, UsageWindow
from app.providers.claude import (
    FIVE_HOUR_SECONDS,
    OAUTH_SOURCE,
    STATUSLINE_SOURCE,
    USAGE_CACHE_SOURCE,
    USAGE_URL,
    WEEK_SECONDS,
    ClaudeProvider,
    apply_rollover,
    extract_claude_oauth,
    format_age,
    format_subscription,
    format_tier,
    merge_official_windows,
    parse_claude_usage,
    parse_retry_after,
    parse_statusline_capture,
    parse_usage_cache,
)


def test_format_subscription() -> None:
    assert format_subscription("max") == "Claude Max"
    assert format_subscription("pro") == "Claude Pro"
    assert format_subscription("team") == "Claude Team"
    assert format_subscription("free") == "Claude Free"
    assert format_subscription(None) == "未知"


def test_format_tier() -> None:
    assert format_tier("default_claude_max_5x") == "5x 額度"
    assert format_tier("default") == "標準"
    assert format_tier(None) == "標準"


def test_parse_retry_after() -> None:
    assert parse_retry_after("120") == 120
    assert parse_retry_after("120.9") == 120
    assert parse_retry_after("0") == 0
    assert parse_retry_after("nope") is None
    assert parse_retry_after(None) is None


def test_extract_claude_oauth_valid() -> None:
    sample = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-test",
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_5x",
            "expiresAt": 1756888102000,
            "scopes": ["user:file_upload", "user:inference"],
        }
    }
    status, _msg, access_token, hint = extract_claude_oauth(sample)
    assert status == SnapshotStatus.OK
    assert access_token == "sk-ant-test"
    assert hint == "Claude (5x 額度)"


def test_extract_claude_oauth_missing_oauth_block() -> None:
    status, _msg, access_token, _hint = extract_claude_oauth({"other": {}})
    assert status == SnapshotStatus.AUTH_ERROR
    assert access_token is None


def test_extract_claude_oauth_missing_access_token() -> None:
    status, _msg, access_token, hint = extract_claude_oauth(
        {"claudeAiOauth": {"rateLimitTier": "default"}}
    )
    assert status == SnapshotStatus.AUTH_ERROR
    assert access_token is None
    assert hint == "Claude (標準)"


def test_parse_claude_usage_limits_array_with_fable_scope() -> None:
    payload = {
        "five_hour": {"utilization": 15.0, "resets_at": "2026-09-03T10:20:00+00:00"},
        "seven_day": {"utilization": 3.0, "resets_at": "2026-09-05T05:00:00+00:00"},
        "seven_day_opus": None,
        "seven_day_sonnet": None,
        "limits": [
            {"kind": "session", "percent": 15, "resets_at": "2026-09-03T10:20:00.428916+00:00"},
            {"kind": "weekly_all", "percent": 3, "resets_at": "2026-09-05T05:00:00.428942+00:00"},
            {
                "kind": "weekly_scoped",
                "percent": 2,
                "resets_at": "2026-09-05T05:00:00.429331+00:00",
                "scope": {"model": {"id": None, "display_name": "Fable"}},
            },
        ],
    }
    windows = parse_claude_usage(payload)
    by_key = {window.key: window for window in windows}
    assert by_key["5h"].used_percent == 15.0
    assert by_key["5h"].remaining_percent == 85.0
    assert by_key["5h"].limit_window_seconds == FIVE_HOUR_SECONDS
    assert by_key["1w"].used_percent == 3.0
    assert by_key["1w"].limit_window_seconds == WEEK_SECONDS
    assert by_key["1w-fable"].used_percent == 2.0
    assert by_key["1w-fable"].remaining_percent == 98.0


def test_parse_claude_usage_falls_back_to_top_level_fields_without_limits() -> None:
    payload = {
        "five_hour": {"utilization": 33.0, "resets_at": "2026-04-11T07:00:00Z"},
        "seven_day": {"utilization": 13.0, "resets_at": "2026-04-17T00:59:59Z"},
        "seven_day_opus": None,
        "seven_day_sonnet": {"utilization": 1.0, "resets_at": "2026-04-17T00:59:59Z"},
    }
    windows = parse_claude_usage(payload)
    by_key = {window.key: window for window in windows}
    assert "1w-opus" not in by_key
    assert by_key["5h"].used_percent == 33.0
    assert by_key["5h"].remaining_percent == 67.0
    assert by_key["1w"].used_percent == 13.0
    assert by_key["1w-sonnet"].used_percent == 1.0


def test_parse_claude_usage_empty_payload() -> None:
    assert parse_claude_usage({}) == []


def _epoch(value: datetime) -> float:
    return value.timestamp()


def _statusline_payload(now: datetime, *, age_seconds: int = 0) -> dict:
    captured_at = now - timedelta(seconds=age_seconds)
    return {
        "captured_at_epoch": _epoch(captured_at),
        "rate_limits": {
            "five_hour": {"used_percentage": 12.34, "resets_at": _epoch(now + timedelta(hours=1))},
            "seven_day": {"used_percentage": 23.45, "resets_at": _epoch(now + timedelta(days=2))},
            "spend_limit": {"used_percentage": 4.5, "resets_at": "2026-09-10T00:00:00Z"},
            "model_scoped": [
                {
                    "displayName": "Fable",
                    "limit": {"utilization": 34.56, "resets_at": _epoch(now + timedelta(days=3))},
                }
            ],
        },
    }


def _write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_creds(path: Path) -> Path:
    return _write_json(
        path,
        {"claudeAiOauth": {"accessToken": "sk-ant-test", "rateLimitTier": "default_claude_max_5x"}},
    )


def test_parse_statusline_capture_maps_all_windows() -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    observed_at, windows, reason = parse_statusline_capture(_statusline_payload(now))
    assert reason is None
    assert observed_at == now
    assert [window.key for window in windows] == ["5h", "1w", "spend", "1w-fable"]
    assert windows[0].used_percent == 12.3
    assert windows[0].remaining_percent == 87.7
    assert windows[0].resets_at == now + timedelta(hours=1)
    assert windows[1].used_percent == 23.4
    assert windows[2].limit_window_seconds is None
    assert windows[3].used_percent == 34.6
    assert windows[3].resets_at == now + timedelta(days=3)


def test_parse_statusline_capture_model_scoped_iso_and_missing() -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    payload = _statusline_payload(now)
    payload["rate_limits"]["model_scoped"][0]["limit"]["resets_at"] = "2026-09-08T12:00:00Z"
    observed_at, windows, reason = parse_statusline_capture(payload)
    assert observed_at == now
    assert reason is None
    assert next(window for window in windows if window.key == "1w-fable").resets_at == datetime(
        2026, 9, 8, 12, tzinfo=timezone.utc
    )
    del payload["rate_limits"]["model_scoped"]
    _observed_at, windows, reason = parse_statusline_capture(payload)
    assert reason is None
    assert "1w-fable" not in {window.key for window in windows}


def test_parse_statusline_capture_tombstone() -> None:
    observed_at, windows, reason = parse_statusline_capture(
        {"captured_at_epoch": 1788609600, "rate_limits": None}
    )
    assert observed_at is not None
    assert windows == []
    assert "no rate_limits" in (reason or "")


def test_parse_statusline_capture_guards_leaked_epoch() -> None:
    payload = {
        "captured_at_epoch": 1788609600,
        "rate_limits": {
            "five_hour": {"used_percentage": 1788534600},
            "seven_day": {"used_percentage": 100.5},
        },
    }
    _observed_at, windows, reason = parse_statusline_capture(payload)
    assert reason is None
    assert [window.key for window in windows] == ["1w"]
    assert windows[0].used_percent == 100.0
    assert windows[0].remaining_percent == 0.0


def test_parse_statusline_capture_requires_timestamp() -> None:
    observed_at, windows, reason = parse_statusline_capture({"rate_limits": {}})
    assert observed_at is None
    assert windows == []
    assert reason == "statusline capture has no captured_at_epoch"


def test_parse_usage_cache_uses_limits_array() -> None:
    fetched_at = datetime(2026, 9, 5, 11, tzinfo=timezone.utc)
    payload = {
        "fetchedAtMs": fetched_at.timestamp() * 1000,
        "utilization": {
            "limits": [
                {"kind": "weekly_scoped", "percent": 9, "resets_at": "2026-09-10T00:00:00Z", "scope": {"model": {"display_name": "Fable"}}}
            ]
        },
    }
    observed_at, windows, reason = parse_usage_cache(payload)
    assert reason is None
    assert observed_at == fetched_at
    assert [window.key for window in windows] == ["1w-fable"]


def _window(key: str, used: float, resets_at: datetime | None = None) -> UsageWindow:
    return UsageWindow(
        key=key,
        label=key,
        used_percent=used,
        remaining_percent=100.0 - used,
        resets_at=resets_at,
        currency="%",
    )


def test_merge_prefers_newer_source_per_key() -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    newer = now - timedelta(minutes=5)
    older = now - timedelta(minutes=10)
    windows, contributing, _newest = merge_official_windows(
        [
            (STATUSLINE_SOURCE, newer, [_window("5h", 10), _window("1w", 20)]),
            (USAGE_CACHE_SOURCE, older, [_window("5h", 30), _window("1w", 40), _window("1w-fable", 50)]),
        ],
        now=now,
    )
    by_key = {window.key: window for window in windows}
    assert by_key["5h"].used_percent == 10
    assert by_key["1w"].used_percent == 20
    assert by_key["1w-fable"].used_percent == 50
    assert by_key["5h"].raw_extra["source"] == STATUSLINE_SOURCE
    assert by_key["1w-fable"].raw_extra["source"] == USAGE_CACHE_SOURCE
    assert contributing == [STATUSLINE_SOURCE, USAGE_CACHE_SOURCE]


def test_apply_rollover_five_hour_and_weekly() -> None:
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    windows = apply_rollover(
        [
            _window("5h", 80, now - timedelta(hours=1)),
            _window("1w", 80, now - timedelta(days=8)),
            _window("spend", 80, now - timedelta(hours=1)),
            _window("1w-fable", 80, now + timedelta(days=1)),
        ],
        now=now,
    )
    by_key = {window.key: window for window in windows}
    assert by_key["5h"].used_percent == 0.0
    assert by_key["5h"].remaining_percent == 100.0
    assert by_key["5h"].resets_at is None
    assert by_key["1w"].used_percent == 0.0
    assert now < by_key["1w"].resets_at <= now + timedelta(days=7)
    assert "spend" not in by_key
    assert by_key["1w-fable"].used_percent == 80


def test_format_age() -> None:
    assert format_age(3599) == "59 分鐘"
    assert format_age(3600) == "1.0 小時"
    assert format_age(7200) == "2.0 小時"


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_serves_statusline_capture_without_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.providers.claude.utcnow", lambda: now)
    capture = _write_json(tmp_path / "capture.json", _statusline_payload(now))
    route = respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json={}))
    provider = ClaudeProvider(tmp_path / "missing.json", statusline_capture_path=capture)
    snapshot = await provider.fetch()
    assert snapshot.status == SnapshotStatus.OK
    assert snapshot.source == STATUSLINE_SOURCE
    assert provider.min_interval_seconds == 60
    assert snapshot.message is None
    assert not route.called


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_serves_usage_cache_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.providers.claude.utcnow", lambda: now)
    cache = _write_json(
        tmp_path / "cache.json",
        {
            "fetchedAtMs": now.timestamp() * 1000,
            "utilization": {
                "limits": [
                    {"kind": "session", "percent": 12, "resets_at": "2026-09-06T00:00:00Z"},
                    {"kind": "weekly_all", "percent": 4, "resets_at": "2026-09-07T00:00:00Z"},
                    {
                        "kind": "weekly_scoped",
                        "percent": 8,
                        "resets_at": "2026-09-07T00:00:00Z",
                        "scope": {"model": {"display_name": "Fable"}},
                    },
                ]
            },
        },
    )
    route = respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json={}))
    provider = ClaudeProvider(tmp_path / "missing-capture.json", usage_cache_path=cache)
    snapshot = await provider.fetch()
    assert snapshot.status == SnapshotStatus.OK
    assert snapshot.source == USAGE_CACHE_SOURCE
    assert {window.key for window in snapshot.windows} >= {"5h", "1w", "1w-fable"}
    assert not route.called
    assert provider.min_interval_seconds == 60


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_reports_age_when_capture_is_old(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.providers.claude.utcnow", lambda: now)
    capture = _write_json(tmp_path / "capture.json", _statusline_payload(now, age_seconds=7200))
    provider = ClaudeProvider(tmp_path / "missing.json", statusline_capture_path=capture)
    snapshot = await provider.fetch()
    assert snapshot.status == SnapshotStatus.OK
    assert snapshot.message == "官方額度資料為 2.0 小時前"


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_merges_cache_for_fable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.providers.claude.utcnow", lambda: now)
    capture_payload = _statusline_payload(now)
    del capture_payload["rate_limits"]["model_scoped"]
    capture = _write_json(tmp_path / "capture.json", capture_payload)
    cache = _write_json(
        tmp_path / "cache.json",
        {
            "fetchedAtMs": (now - timedelta(minutes=5)).timestamp() * 1000,
            "utilization": {"limits": [{"kind": "weekly_scoped", "percent": 9, "resets_at": "2026-09-10T00:00:00Z", "scope": {"model": {"display_name": "Fable"}}}]},
        },
    )
    provider = ClaudeProvider(tmp_path / "missing.json", statusline_capture_path=capture, usage_cache_path=cache)
    snapshot = await provider.fetch()
    assert snapshot.source == "statusline+claude-code-cache"
    assert "1w-fable" in {window.key for window in snapshot.windows}


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_falls_back_to_oauth_when_local_expired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.providers.claude.utcnow", lambda: now)
    old_payload = _statusline_payload(now, age_seconds=21601)
    capture = _write_json(tmp_path / "capture.json", old_payload)
    route = respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json={"limits": [{"kind": "session", "percent": 40, "resets_at": "2026-09-06T00:00:00Z"}]}))
    provider = ClaudeProvider(_write_creds(tmp_path / "creds.json"), statusline_capture_path=capture, usage_cache_path=tmp_path / "missing-cache.json")
    snapshot = await provider.fetch()
    assert route.called
    assert snapshot.source == OAUTH_SOURCE
    assert provider.min_interval_seconds == 60


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_oauth_429_message_names_local_reasons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.providers.claude.utcnow", lambda: now)
    before = now
    respx.get(USAGE_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}))
    provider = ClaudeProvider(_write_creds(tmp_path / "creds.json"))
    snapshot = await provider.fetch()
    assert snapshot.status == SnapshotStatus.RATE_LIMITED
    assert provider.retry_after_seconds is None
    assert provider._oauth_next_attempt_at >= before + timedelta(seconds=3600)
    assert "local sources:" in (snapshot.message or "")


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_rations_oauth_between_polls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.providers.claude.utcnow", lambda: now)
    route = respx.get(USAGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "limits": [
                    {"kind": "session", "percent": 12, "resets_at": "2026-09-06T00:00:00Z"},
                    {"kind": "weekly_all", "percent": 4, "resets_at": "2026-09-07T00:00:00Z"},
                    {
                        "kind": "weekly_scoped",
                        "percent": 8,
                        "resets_at": "2026-09-07T00:00:00Z",
                        "scope": {"model": {"display_name": "Fable"}},
                    },
                ]
            },
        )
    )
    provider = ClaudeProvider(_write_creds(tmp_path / "creds.json"))
    first = await provider.fetch()
    second = await provider.fetch()
    assert route.call_count == 1
    assert second.status == SnapshotStatus.OK
    assert second.source == OAUTH_SOURCE
    assert second.windows == first.windows
    assert (second.message or "").startswith("沿用")
    assert second.fetched_at == first.fetched_at


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_rationed_without_prior_snapshot_returns_rate_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.providers.claude.utcnow", lambda: now)
    route = respx.get(USAGE_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3600"})
    )
    provider = ClaudeProvider(_write_creds(tmp_path / "creds.json"))
    await provider.fetch()
    second = await provider.fetch()
    assert route.call_count == 1
    assert second.status == SnapshotStatus.RATE_LIMITED
    assert "rationed" in (second.message or "")


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_rereads_local_source_while_oauth_rationed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.providers.claude.utcnow", lambda: now)
    capture = tmp_path / "capture.json"
    route = respx.get(USAGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={"limits": [{"kind": "session", "percent": 12, "resets_at": "2026-09-06T00:00:00Z"}]},
        )
    )
    provider = ClaudeProvider(_write_creds(tmp_path / "creds.json"), statusline_capture_path=capture)
    first = await provider.fetch()
    assert first.source == OAUTH_SOURCE
    _write_json(capture, _statusline_payload(now))
    second = await provider.fetch()
    assert second.source == STATUSLINE_SOURCE
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_local_reason_order_is_statusline_then_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.providers.claude.utcnow", lambda: now)
    spend_only = {
        "captured_at_epoch": now.timestamp(),
        "rate_limits": {"spend_limit": {"used_percentage": 5}},
    }
    capture = _write_json(tmp_path / "capture.json", spend_only)
    respx.get(USAGE_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}))
    provider = ClaudeProvider(
        _write_creds(tmp_path / "creds.json"),
        statusline_capture_path=capture,
        usage_cache_path=tmp_path / "missing-cache.json",
    )
    snapshot = await provider.fetch()
    message = snapshot.message or ""
    assert message.index("statusline capture") < message.index("usage cache")


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_works_without_credentials_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.providers.claude.utcnow", lambda: now)
    capture = _write_json(tmp_path / "capture.json", _statusline_payload(now))
    provider = ClaudeProvider(tmp_path / "nope.json", statusline_capture_path=capture)
    snapshot = await provider.fetch()
    assert snapshot.status == SnapshotStatus.OK
    assert snapshot.source == STATUSLINE_SOURCE
    assert snapshot.account_hint is None


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_fetch_ok_parses_limits(tmp_path: Path) -> None:
    creds = _write_creds(tmp_path / "creds.json")
    route = respx.get(USAGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "limits": [
                    {"kind": "session", "percent": 12, "resets_at": "2026-09-06T00:00:00Z"},
                    {"kind": "weekly_all", "percent": 4, "resets_at": "2026-09-07T00:00:00Z"},
                    {
                        "kind": "weekly_scoped",
                        "percent": 8,
                        "resets_at": "2026-09-07T00:00:00Z",
                        "scope": {"model": {"display_name": "Fable"}},
                    },
                ]
            },
        )
    )
    provider = ClaudeProvider(creds)
    snapshot = await provider.fetch()
    assert snapshot.status == SnapshotStatus.OK
    assert snapshot.source == OAUTH_SOURCE
    assert {window.key for window in snapshot.windows} == {"5h", "1w", "1w-fable"}
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer sk-ant-test"
    assert request.headers["anthropic-beta"] == "oauth-2025-04-20"


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_fetch_missing_credentials_reports_credentials_error(tmp_path: Path) -> None:
    provider = ClaudeProvider(tmp_path / "missing.json")
    snapshot = await provider.fetch()
    assert snapshot.status == SnapshotStatus.AUTH_ERROR
    assert snapshot.source == "credentials"
    assert "local sources:" in (snapshot.message or "")


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_fetch_no_access_token_reports_credentials_error(tmp_path: Path) -> None:
    creds = _write_json(tmp_path / "creds.json", {"claudeAiOauth": {"rateLimitTier": "default"}})
    provider = ClaudeProvider(creds)
    snapshot = await provider.fetch()
    assert snapshot.status == SnapshotStatus.AUTH_ERROR
    assert snapshot.source == "credentials"
    assert "local sources:" in (snapshot.message or "")
