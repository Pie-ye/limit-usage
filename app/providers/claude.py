from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Callable

import httpx

from app.models import AccountSnapshot, ProviderId, SnapshotStatus, UsageWindow, utcnow
from app.providers.base import error_snapshot

logger = logging.getLogger(__name__)

# Claude's statusline capture is the primary source, followed by Claude Code's
# usage cache for windows (such as model-scoped weekly caps) not in statusline.
# The OAuth endpoint is used only when neither local source has account windows.
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Accept": "application/json",
    "User-Agent": "limit-usage/0.1",
}

STATUSLINE_SOURCE = "statusline"
USAGE_CACHE_SOURCE = "claude-code-cache"
OAUTH_SOURCE = "oauth/usage"
LOCAL_MIN_INTERVAL_SECONDS = 60
DEFAULT_OAUTH_MIN_INTERVAL_SECONDS = 1800
OFFICIAL_MAX_AGE_SECONDS = 21600
FRESH_MESSAGE_THRESHOLD_SECONDS = 900
FIVE_HOUR_SECONDS = 18000
WEEK_SECONDS = 604800


def format_subscription(sub_type: str | None) -> str:
    if not sub_type:
        return "未知"
    s = sub_type.strip().lower()
    if s == "max":
        return "Claude Max"
    if s == "pro":
        return "Claude Pro"
    if s == "team":
        return "Claude Team"
    if s == "enterprise":
        return "Claude Enterprise"
    if s == "free":
        return "Claude Free"
    return f"Claude {sub_type.capitalize()}"


def format_tier(tier: str | None) -> str:
    if not tier:
        return "標準"
    t = tier.strip().lower()
    if "5x" in t or "max_5x" in t:
        return "5x 額度"
    if "20x" in t:
        return "20x 額度"
    if "default" in t:
        return "標準"
    return tier


def parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = int(float(text))
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _usage_window(
    entry: Any,
    key: str,
    label: str,
    *,
    limit_window_seconds: int | None = None,
) -> UsageWindow | None:
    if not isinstance(entry, dict):
        return None
    utilization = entry.get("utilization")
    if utilization is None:
        return None
    try:
        used = round(float(utilization), 1)
    except (TypeError, ValueError):
        return None
    remaining = round(max(0.0, 100.0 - used), 1)
    return UsageWindow(
        key=key,
        label=label,
        used_percent=used,
        remaining_percent=remaining,
        resets_at=_parse_datetime(entry.get("resets_at")),
        limit_window_seconds=limit_window_seconds,
        currency="%",
        raw_extra={k: v for k, v in entry.items() if k not in {"utilization", "resets_at"}},
    )


def _slugify_model(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "model"


def _limits_windows(limits: list[Any]) -> list[UsageWindow]:
    """Parse the OAuth `limits[]` array."""
    windows: list[UsageWindow] = []
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        percent = entry.get("percent")
        if percent is None:
            continue
        try:
            used = round(float(percent), 1)
        except (TypeError, ValueError):
            continue
        remaining = round(max(0.0, 100.0 - used), 1)
        resets_at = _parse_datetime(entry.get("resets_at"))
        kind = entry.get("kind")

        if kind == "session":
            key, label, window_s = "5h", "Claude · 5小時", FIVE_HOUR_SECONDS
        elif kind == "weekly_all":
            key, label, window_s = "1w", "Claude · 週額度", WEEK_SECONDS
        elif kind == "weekly_scoped":
            scope = entry.get("scope") or {}
            model = (scope.get("model") or {}).get("display_name")
            if not model:
                continue
            key, label, window_s = (
                f"1w-{_slugify_model(model)}",
                f"Claude · 週額度 ({model})",
                WEEK_SECONDS,
            )
        else:
            continue

        windows.append(
            UsageWindow(
                key=key,
                label=label,
                used_percent=used,
                remaining_percent=remaining,
                resets_at=resets_at,
                limit_window_seconds=window_s,
                currency="%",
                raw_extra={k: v for k, v in entry.items() if k not in {"percent", "resets_at"}},
            )
        )
    return windows


def parse_claude_usage(payload: dict[str, Any]) -> list[UsageWindow]:
    """Parse the response of Anthropic's OAuth usage endpoint."""
    limits = payload.get("limits")
    if isinstance(limits, list) and limits:
        windows = _limits_windows(limits)
        if windows:
            return windows

    mapping = [
        ("five_hour", "5h", "Claude · 5小時", FIVE_HOUR_SECONDS),
        ("seven_day", "1w", "Claude · 週額度", WEEK_SECONDS),
        ("seven_day_opus", "1w-opus", "Claude · 週額度 (Opus)", WEEK_SECONDS),
        ("seven_day_sonnet", "1w-sonnet", "Claude · 週額度 (Sonnet)", WEEK_SECONDS),
    ]
    windows: list[UsageWindow] = []
    for field, key, label, window_s in mapping:
        window = _usage_window(
            payload.get(field), key, label, limit_window_seconds=window_s
        )
        if window is not None:
            windows.append(window)
    return windows


def _epoch_datetime(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _statusline_used(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    used = float(value)
    if not math.isfinite(used) or used < 0 or used > 101:
        return None
    return round(min(used, 100.0), 1)


def _statusline_resets_at(value: Any) -> datetime | None:
    if isinstance(value, str):
        return _parse_datetime(value)
    return _epoch_datetime(value)


def parse_statusline_capture(
    payload: Any,
) -> tuple[datetime | None, list[UsageWindow], str | None]:
    if not isinstance(payload, dict):
        return None, [], "statusline capture is not a JSON object"

    observed_at = _epoch_datetime(payload.get("captured_at_epoch"))
    if observed_at is None:
        return None, [], "statusline capture has no captured_at_epoch"

    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict) or not rate_limits:
        return observed_at, [], "statusline capture has no rate_limits"

    windows: list[UsageWindow] = []
    standard_mapping = [
        ("five_hour", "5h", "Claude · 5小時", FIVE_HOUR_SECONDS),
        ("seven_day", "1w", "Claude · 週額度", WEEK_SECONDS),
        ("spend_limit", "spend", "Claude · 支出上限", None),
    ]
    for field, key, label, window_s in standard_mapping:
        entry = rate_limits.get(field)
        if not isinstance(entry, dict):
            continue
        used = _statusline_used(entry.get("used_percentage"))
        if used is None:
            continue
        windows.append(
            UsageWindow(
                key=key,
                label=label,
                used_percent=used,
                remaining_percent=round(max(0.0, 100.0 - used), 1),
                resets_at=_statusline_resets_at(entry.get("resets_at")),
                limit_window_seconds=window_s,
                currency="%",
                raw_extra={
                    k: v for k, v in entry.items() if k not in {"used_percentage", "resets_at"}
                },
            )
        )

    model_scoped = rate_limits.get("model_scoped", [])
    if isinstance(model_scoped, list):
        for entry in model_scoped:
            if not isinstance(entry, dict):
                continue
            display_name = entry.get("displayName")
            limit = entry.get("limit")
            if not isinstance(display_name, str) or not display_name.strip():
                continue
            if not isinstance(limit, dict):
                continue
            used = _statusline_used(limit.get("utilization"))
            if used is None:
                continue
            windows.append(
                UsageWindow(
                    key=f"1w-{_slugify_model(display_name)}",
                    label=f"Claude · 週額度 ({display_name})",
                    used_percent=used,
                    remaining_percent=round(max(0.0, 100.0 - used), 1),
                    resets_at=_statusline_resets_at(limit.get("resets_at")),
                    limit_window_seconds=WEEK_SECONDS,
                    currency="%",
                    raw_extra={k: v for k, v in entry.items() if k != "limit"},
                )
            )

    if not windows:
        return observed_at, [], "statusline capture has no usable windows"
    return observed_at, windows, None


def parse_usage_cache(
    payload: Any,
) -> tuple[datetime | None, list[UsageWindow], str | None]:
    if not isinstance(payload, dict):
        return None, [], "usage cache is not a JSON object"

    fetched_at = payload.get("fetchedAtMs")
    if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
        return None, [], "usage cache has no fetchedAtMs"
    if not math.isfinite(float(fetched_at)):
        return None, [], "usage cache has no fetchedAtMs"
    try:
        observed_at = datetime.fromtimestamp(fetched_at / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None, [], "usage cache has no fetchedAtMs"

    utilization = payload.get("utilization")
    if not isinstance(utilization, dict):
        return observed_at, [], "usage cache has no utilization"
    windows = parse_claude_usage(utilization)
    if not windows:
        return observed_at, [], "usage cache has no usable windows"
    return observed_at, windows, None


def apply_rollover(windows: list[UsageWindow], *, now: datetime) -> list[UsageWindow]:
    rolled: list[UsageWindow] = []
    for window in windows:
        if window.resets_at is None or now < window.resets_at:
            rolled.append(window)
            continue
        raw_extra = {**window.raw_extra, "rolled_over": True}
        if window.key == "5h":
            rolled.append(
                window.model_copy(
                    update={
                        "used_percent": 0.0,
                        "remaining_percent": 100.0,
                        "resets_at": None,
                        "raw_extra": raw_extra,
                    }
                )
            )
        elif window.key == "1w" or window.key.startswith("1w-"):
            resets_at = window.resets_at
            while resets_at <= now:
                resets_at += timedelta(days=7)
            rolled.append(
                window.model_copy(
                    update={
                        "used_percent": 0.0,
                        "remaining_percent": 100.0,
                        "resets_at": resets_at,
                        "raw_extra": raw_extra,
                    }
                )
            )
        # Expired windows with unrelated keys are intentionally omitted.
    return rolled


def merge_official_windows(
    sources: list[tuple[str, datetime, list[UsageWindow]]],
    *,
    now: datetime,
) -> tuple[list[UsageWindow], list[str], datetime | None]:
    ordered = sorted(sources, key=lambda source: source[1], reverse=True)
    windows: list[UsageWindow] = []
    seen: set[str] = set()
    for source_name, observed_at, source_windows in ordered:
        for window in source_windows:
            if window.key in seen:
                continue
            seen.add(window.key)
            windows.append(
                window.model_copy(
                    update={
                        "raw_extra": {
                            **window.raw_extra,
                            "observed_at": observed_at.isoformat(),
                            "source": source_name,
                        }
                    }
                )
            )

    windows = apply_rollover(windows, now=now)
    contributing = {
        window.raw_extra.get("source")
        for window in windows
        if isinstance(window.raw_extra.get("source"), str)
    }
    contributing_observations = {
        source_name: observed_at
        for source_name, observed_at, _source_windows in sources
        if source_name in contributing
    }
    newest_observed_at = (
        max(contributing_observations.values()) if contributing_observations else None
    )

    contributing_sources = [
        source
        for source in (STATUSLINE_SOURCE, USAGE_CACHE_SOURCE)
        if source in contributing
    ]
    return windows, contributing_sources, newest_observed_at


def format_age(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds) // 60} 分鐘"
    return f"{seconds / 3600:.1f} 小時"


def extract_claude_oauth(
    data: dict[str, Any],
) -> tuple[SnapshotStatus, str, str | None, str | None]:
    """Extract the access token and a display hint from the credentials file."""
    oauth = data.get("claudeAiOauth")
    if not oauth or not isinstance(oauth, dict):
        return SnapshotStatus.AUTH_ERROR, "No claudeAiOauth found in credentials", None, None

    access_token = oauth.get("accessToken")
    tier_display = format_tier(oauth.get("rateLimitTier"))
    hint = f"Claude ({tier_display})"

    if not access_token:
        return (
            SnapshotStatus.AUTH_ERROR,
            "No accessToken in Claude credentials. Run `claude login`.",
            None,
            hint,
        )

    return SnapshotStatus.OK, "Claude credentials loaded", access_token, hint


class ClaudeProvider:
    provider_id = ProviderId.CLAUDE
    display_name = "Claude"

    def __init__(
        self,
        credentials_path: Path,
        timeout: float = 20.0,
        *,
        statusline_capture_path: Path | None = None,
        usage_cache_path: Path | None = None,
        official_max_age_seconds: int = OFFICIAL_MAX_AGE_SECONDS,
        oauth_min_interval_seconds: int = DEFAULT_OAUTH_MIN_INTERVAL_SECONDS,
    ) -> None:
        self.credentials_path = credentials_path
        self.timeout = timeout
        self.statusline_capture_path = statusline_capture_path
        self.usage_cache_path = usage_cache_path
        self.official_max_age_seconds = official_max_age_seconds
        self.oauth_min_interval_seconds = oauth_min_interval_seconds
        self.min_interval_seconds = LOCAL_MIN_INTERVAL_SECONDS
        self.retry_after_seconds: int | None = None
        self._oauth_next_attempt_at: datetime | None = None
        self._last_oauth_snapshot: AccountSnapshot | None = None

    def _read_local_source(
        self,
        path: Path | None,
        parser: Callable[[Any], tuple[datetime | None, list[UsageWindow], str | None]],
        name: str,
        *,
        now: datetime | None = None,
    ) -> tuple[list[UsageWindow], datetime | None, str | None]:
        if path is None:
            return [], None, f"{name} path not configured"
        if not path.is_file():
            return [], None, f"{name} missing at {path}"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return [], None, f"{name} unreadable: {exc}"
        observed_at, windows, reason = parser(payload)
        if reason is not None:
            return windows, observed_at, reason
        age = ((now or utcnow()) - observed_at).total_seconds()
        if age > self.official_max_age_seconds:
            return [], observed_at, (
                f"{name} is {int(age)}s old "
                f"(max {self.official_max_age_seconds}s)"
            )
        return windows, observed_at, None

    async def fetch(self) -> AccountSnapshot:
        hint: str | None = None
        creds_error: tuple[SnapshotStatus, str] | None = None
        access_token: str | None = None
        if not self.credentials_path or not self.credentials_path.is_file():
            creds_error = (
                SnapshotStatus.AUTH_ERROR,
                f"Credentials file not found at {self.credentials_path}",
            )
        else:
            try:
                data = json.loads(self.credentials_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("credentials JSON is not an object")
            except Exception as exc:
                creds_error = (SnapshotStatus.ERROR, f"Failed to read credentials: {exc}")
            else:
                status, msg, access_token, hint = extract_claude_oauth(data)
                if status != SnapshotStatus.OK or not access_token:
                    creds_error = (status, msg)

        now = utcnow()
        local_sources: list[tuple[str, datetime, list[UsageWindow]]] = []
        local_reasons: list[str] = []
        local_sources_spec = [
            (
                STATUSLINE_SOURCE,
                "statusline capture",
                self.statusline_capture_path,
                parse_statusline_capture,
            ),
            (
                USAGE_CACHE_SOURCE,
                "usage cache",
                self.usage_cache_path,
                parse_usage_cache,
            ),
        ]
        parsed_sources: list[tuple[str, datetime | None, list[UsageWindow], str | None]] = []
        for source_name, name, path, parser in local_sources_spec:
            windows, observed_at, reason = self._read_local_source(
                path, parser, name, now=now
            )
            parsed_sources.append((source_name, observed_at, windows, reason))
            if reason is None and observed_at is not None and windows:
                local_sources.append((source_name, observed_at, windows))

        for source_name, _observed_at, source_windows, reason in parsed_sources:
            if reason is not None:
                local_reasons.append(reason)
            elif not {window.key for window in source_windows}.intersection({"5h", "1w"}):
                display_name = next(
                    name
                    for spec_source, name, _path, _parser in local_sources_spec
                    if spec_source == source_name
                )
                local_reasons.append(f"{display_name} has no 5h/1w window")

        windows, contributing, newest_observed_at = merge_official_windows(
            local_sources, now=now
        )
        window_keys = {window.key for window in windows}
        if "5h" in window_keys or "1w" in window_keys:
            age = (now - newest_observed_at).total_seconds() if newest_observed_at else 0.0
            message = (
                None
                if age <= FRESH_MESSAGE_THRESHOLD_SECONDS
                else f"官方額度資料為 {format_age(age)}前"
            )
            return AccountSnapshot(
                provider=self.provider_id,
                display_name=self.display_name,
                account_hint=hint,
                status=SnapshotStatus.OK,
                message=message,
                windows=windows,
                fetched_at=now,
                source="+".join(contributing),
            )

        local_note = "; ".join(local_reasons)
        logger.info(
            "Claude local sources unusable (%s); falling back to %s",
            local_note,
            USAGE_URL,
        )

        def fallback_message(detail: str) -> str:
            return f"{detail} (local sources: {local_note})"

        if creds_error is not None:
            status, msg = creds_error
            return error_snapshot(
                self.provider_id,
                self.display_name,
                status,
                fallback_message(msg),
                account_hint=hint,
                source="credentials",
            )

        if self._oauth_next_attempt_at is not None and now < self._oauth_next_attempt_at:
            wait = (self._oauth_next_attempt_at - now).total_seconds()
            if self._last_oauth_snapshot is not None:
                age = (now - self._last_oauth_snapshot.fetched_at).total_seconds()
                return self._last_oauth_snapshot.model_copy(
                    update={
                        "message": (
                            f"沿用 {format_age(age)}前的 OAuth 額度"
                            f"（本地來源：{local_note}）"
                        )
                    }
                )
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.RATE_LIMITED,
                f"OAuth fallback rationed; next attempt in {int(wait)}s "
                f"(local sources: {local_note})",
                account_hint=hint,
                source=OAUTH_SOURCE,
            )

        self._oauth_next_attempt_at = now + timedelta(
            seconds=self.oauth_min_interval_seconds
        )

        headers = {**USAGE_HEADERS, "Authorization": f"Bearer {access_token}"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.get(USAGE_URL, headers=headers)
        except httpx.HTTPError as exc:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.ERROR,
                fallback_message(f"HTTP request failed: {exc}"),
                account_hint=hint,
                source=OAUTH_SOURCE,
            )

        http_status = resp.status_code
        if http_status == 429:
            wait = parse_retry_after(resp.headers.get("Retry-After"))
            self._oauth_next_attempt_at = now + timedelta(
                seconds=max(wait or 0, self.oauth_min_interval_seconds)
            )
            logger.warning(
                "Claude usage API rate limited (retry-after=%s)",
                wait if wait is not None else "none",
            )
            detail = "Claude usage API rate limited"
            if wait is not None:
                detail = f"{detail}; retry-after={wait}s"
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.RATE_LIMITED,
                fallback_message(detail),
                account_hint=hint,
                source=OAUTH_SOURCE,
            )
        if http_status in (401, 403):
            err = (resp.text or resp.reason_phrase or "")[:300]
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.AUTH_ERROR,
                fallback_message(
                    f"Auth failed ({http_status}). Re-run `claude login`. {err}".strip()
                ),
                account_hint=hint,
                source=OAUTH_SOURCE,
            )
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            err = (resp.text or resp.reason_phrase or "invalid JSON response")[:300]
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.ERROR,
                fallback_message(err or f"Unexpected response (HTTP {http_status})"),
                account_hint=hint,
                source=OAUTH_SOURCE,
            )

        windows = parse_claude_usage(payload)
        snapshot = AccountSnapshot(
            provider=self.provider_id,
            display_name=self.display_name,
            account_hint=hint,
            status=SnapshotStatus.OK,
            message=None,
            windows=windows,
            fetched_at=now,
            source=OAUTH_SOURCE,
        )
        self._last_oauth_snapshot = snapshot
        return snapshot
