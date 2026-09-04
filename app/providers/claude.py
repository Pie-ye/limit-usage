from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from app.models import AccountSnapshot, ProviderId, SnapshotStatus, UsageWindow, utcnow
from app.providers.base import error_snapshot

logger = logging.getLogger(__name__)

# Undocumented endpoint that backs Claude Code's own usage HUD/statusline.
# Requires the OAuth access token from ~/.claude/.credentials.json.
#
# Only used as a fallback now: the endpoint is rate limited per *account*, and
# every running Claude Code session already polls it for its own footer, so a
# background poller here reliably earns a 429 with `Retry-After: 3600`.
# The primary source is claude-monitor's state file, which carries the same
# official numbers with zero API calls — see MONITOR_* below.
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# This endpoint 429s quickly if polled as often as Codex/Grok (60s).
DEFAULT_MIN_INTERVAL_SECONDS = 300
# Served from claude-monitor's state file there is no API call to ration, so
# the poller may run at the normal cadence.
MONITOR_MIN_INTERVAL_SECONDS = 60
FIVE_HOUR_SECONDS = 18000
WEEK_SECONDS = 604800

# https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor
# `claude-monitor --statusline` runs as a Claude Code statusline hook and
# captures the official `rate_limits` block Claude Code hands its status line;
# `claude-monitor --once --write-state` folds that capture into this file.
MONITOR_SOURCE = "claude-monitor"
DEFAULT_MONITOR_STATE_PATH = "~/.claude-monitor/state/latest.json"
# The state file is only as fresh as the timer that writes it. Past this age we
# stop trusting it and fall back to the OAuth endpoint.
MONITOR_MAX_AGE_SECONDS = 900
# claude-monitor labels every window it reports. Anything below `official` is
# its own token-count estimate against a guessed plan ceiling, which drifts far
# enough to be useless for a quota dashboard (observed 170% against a real 12%).
MONITOR_TRUSTED_CONFIDENCE = "official"

MONITOR_WINDOWS: dict[str, tuple[str, str, int | None]] = {
    "five_hour": ("5h", "Claude · 5小時", FIVE_HOUR_SECONDS),
    "seven_day": ("1w", "Claude · 週額度", WEEK_SECONDS),
    "spend_limit": ("spend", "Claude · 支出上限", None),
}

# Per-model weekly windows (Fable) exist only on the OAuth endpoint: Claude Code
# hands its status line the account-wide five_hour/seven_day pair and nothing
# scoped, so claude-monitor has nothing to capture. We top the card up with a
# rare, deliberately isolated call — rare enough not to provoke the 429 that
# 300s polling did, and isolated so its failures never touch the main snapshot.
SCOPED_REFRESH_SECONDS = 1800
# Weekly windows move slowly, so a cached value stays useful well past one
# refresh; drop it once even that stops being defensible.
SCOPED_MAX_AGE_SECONDS = 7200

USAGE_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Accept": "application/json",
    "User-Agent": "limit-usage/0.1",
}


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
    """Parse the `limits[]` array — the per-model breakdown (e.g. a Fable-scoped
    weekly cap) that the top-level seven_day_opus/seven_day_sonnet fields don't
    carry once they're null/deprecated on an account.
    """
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
    """Parse the response of Anthropic's OAuth usage endpoint into UsageWindows.

    Real usage, not derived from the OAuth token's expiry — the API reports
    actual 5-hour and 7-day rate-limit consumption for the account. The
    `limits[]` array is the primary source: it carries per-model-scoped weekly
    caps (e.g. Fable) that the top-level seven_day_opus/seven_day_sonnet
    fields no longer populate. Fall back to those top-level fields if `limits`
    is absent (older/alternate response shape).
    """
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
    windows = []
    for field, key, label, window_s in mapping:
        window = _usage_window(
            payload.get(field), key, label, limit_window_seconds=window_s
        )
        if window is not None:
            windows.append(window)
    return windows


def parse_scoped_usage(payload: Any) -> list[UsageWindow]:
    """Pick only the per-model weekly windows out of an OAuth usage payload.

    The account-wide windows are dropped: claude-monitor already supplies those
    from the statusline capture, and they are the fresher of the two.
    """
    if not isinstance(payload, dict):
        return []
    limits = payload.get("limits")
    if not isinstance(limits, list):
        return []
    scoped = [
        entry
        for entry in limits
        if isinstance(entry, dict) and entry.get("kind") == "weekly_scoped"
    ]
    return _limits_windows(scoped)


def parse_monitor_state(
    payload: Any,
    *,
    now: datetime | None = None,
    max_age_seconds: int = MONITOR_MAX_AGE_SECONDS,
) -> tuple[list[UsageWindow], str | None]:
    """Parse claude-monitor's `--write-state` snapshot into UsageWindows.

    Returns `(windows, reason)`; `reason` explains an empty result so the
    caller can say why it fell through to the OAuth endpoint.

    Only windows claude-monitor marks `confidence: official` are kept — those
    came from the statusline capture and are the same server-side percentages
    the API reports. Its `local_estimate` windows are token counts divided by a
    guessed plan ceiling and are not comparable.
    """
    if not isinstance(payload, dict):
        return [], "state file is not a JSON object"

    generated_at = _parse_datetime(payload.get("generated_at"))
    if generated_at is None:
        return [], "state file has no readable generated_at"
    age = ((now or utcnow()) - generated_at).total_seconds()
    if age > max_age_seconds:
        return [], f"state file is stale ({int(age)}s old, max {max_age_seconds}s)"

    limits = payload.get("limits")
    if not isinstance(limits, dict):
        return [], "state file has no limits object"

    windows: list[UsageWindow] = []
    for field, (key, label, window_s) in MONITOR_WINDOWS.items():
        entry = limits.get(field)
        if not isinstance(entry, dict):
            continue
        if entry.get("confidence") != MONITOR_TRUSTED_CONFIDENCE:
            continue
        percent = entry.get("used_percentage")
        if percent is None:
            continue
        try:
            used = round(float(percent), 1)
        except (TypeError, ValueError):
            continue
        resets_at = _parse_datetime(entry.get("resets_at"))
        if resets_at is None:
            epoch = entry.get("resets_at_epoch")
            if isinstance(epoch, (int, float)):
                resets_at = datetime.fromtimestamp(epoch, tz=timezone.utc)
        raw_extra = {
            "confidence": entry.get("confidence"),
            "source_kind": (entry.get("source") or {}).get("kind"),
        }
        for extra in ("tokens_used", "token_limit"):
            if entry.get(extra) is not None:
                raw_extra[extra] = entry[extra]
        windows.append(
            UsageWindow(
                key=key,
                label=label,
                used_percent=used,
                remaining_percent=round(max(0.0, 100.0 - used), 1),
                resets_at=resets_at,
                limit_window_seconds=window_s,
                currency="%",
                raw_extra=raw_extra,
            )
        )

    if not windows:
        return [], "state file has no windows with official rate limits yet"
    return windows, None


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
    min_interval_seconds = DEFAULT_MIN_INTERVAL_SECONDS

    def __init__(
        self,
        credentials_path: Path,
        timeout: float = 20.0,
        min_interval_seconds: int = DEFAULT_MIN_INTERVAL_SECONDS,
        monitor_state_path: Path | None = None,
        monitor_max_age_seconds: int = MONITOR_MAX_AGE_SECONDS,
        scoped_refresh_seconds: int = SCOPED_REFRESH_SECONDS,
        scoped_max_age_seconds: int = SCOPED_MAX_AGE_SECONDS,
    ) -> None:
        self.credentials_path = credentials_path
        self.timeout = timeout
        self.oauth_min_interval_seconds = min_interval_seconds
        self.min_interval_seconds = min_interval_seconds
        self.monitor_state_path = monitor_state_path
        self.monitor_max_age_seconds = monitor_max_age_seconds
        self.scoped_refresh_seconds = scoped_refresh_seconds
        self.scoped_max_age_seconds = scoped_max_age_seconds
        self.retry_after_seconds: int | None = None
        self._scoped_windows: list[UsageWindow] = []
        self._scoped_fetched_at: datetime | None = None
        self._scoped_next_attempt_at: datetime | None = None

    def _store_scoped(self, windows: list[UsageWindow], *, now: datetime) -> None:
        if not windows:
            return
        self._scoped_windows = windows
        self._scoped_fetched_at = now
        # Any successful read counts against the refresh budget, including one
        # that came free with an OAuth fallback — otherwise the next poll spends
        # a call on data we are already holding.
        self._scoped_next_attempt_at = now + timedelta(seconds=self.scoped_refresh_seconds)

    async def _scoped_supplement(self, access_token: str | None) -> list[UsageWindow]:
        """Top the claude-monitor windows up with the per-model weekly caps.

        Deliberately toothless: it never sets `retry_after_seconds` and never
        raises. A 429 or a dead network here must not back off or degrade a
        snapshot that claude-monitor already answered correctly — the worst
        case is that the Fable window keeps its previous value.
        """
        now = utcnow()
        if (
            self._scoped_fetched_at
            and (now - self._scoped_fetched_at).total_seconds() > self.scoped_max_age_seconds
        ):
            self._scoped_windows = []
            self._scoped_fetched_at = None

        if not access_token or self.scoped_refresh_seconds <= 0:
            return self._scoped_windows
        if self._scoped_next_attempt_at and now < self._scoped_next_attempt_at:
            return self._scoped_windows

        # Book the next attempt before the call, so a hang or an exception
        # can't turn this into a retry loop against a rate-limited endpoint.
        self._scoped_next_attempt_at = now + timedelta(seconds=self.scoped_refresh_seconds)
        headers = {**USAGE_HEADERS, "Authorization": f"Bearer {access_token}"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.get(USAGE_URL, headers=headers)
        except httpx.HTTPError as exc:
            logger.info("Claude scoped-window refresh failed: %s", exc)
            return self._scoped_windows

        if resp.status_code == 429:
            wait = parse_retry_after(resp.headers.get("Retry-After"))
            if wait:
                self._scoped_next_attempt_at = now + timedelta(
                    seconds=max(wait, self.scoped_refresh_seconds)
                )
            logger.info(
                "Claude scoped-window refresh rate limited (retry-after=%s); keeping cached windows",
                wait if wait is not None else "none",
            )
            return self._scoped_windows
        if resp.status_code >= 400:
            logger.info("Claude scoped-window refresh returned HTTP %s", resp.status_code)
            return self._scoped_windows

        try:
            payload = resp.json()
        except ValueError:
            logger.info("Claude scoped-window refresh returned invalid JSON")
            return self._scoped_windows

        self._store_scoped(parse_scoped_usage(payload), now=now)
        return self._scoped_windows

    def _read_monitor_state(self) -> tuple[list[UsageWindow], str | None]:
        if not self.monitor_state_path:
            return [], "no claude-monitor state path configured"
        if not self.monitor_state_path.is_file():
            return [], f"no claude-monitor state file at {self.monitor_state_path}"
        try:
            payload = json.loads(self.monitor_state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return [], f"failed to read claude-monitor state: {exc}"
        return parse_monitor_state(payload, max_age_seconds=self.monitor_max_age_seconds)

    async def fetch(self) -> AccountSnapshot:
        self.retry_after_seconds = None
        # Best effort: the tier hint is nice to have, but claude-monitor's
        # numbers don't need the credentials file, so a broken one must not
        # blank the card on its own.
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
            except Exception as exc:
                creds_error = (SnapshotStatus.ERROR, f"Failed to read credentials: {exc}")
            else:
                status, msg, access_token, hint = extract_claude_oauth(data)
                if status != SnapshotStatus.OK or not access_token:
                    creds_error = (status, msg)

        windows, monitor_note = self._read_monitor_state()
        if windows:
            self.min_interval_seconds = MONITOR_MIN_INTERVAL_SECONDS
            return AccountSnapshot(
                provider=self.provider_id,
                display_name=self.display_name,
                account_hint=hint,
                status=SnapshotStatus.OK,
                message=None,
                windows=windows + await self._scoped_supplement(access_token),
                fetched_at=utcnow(),
                source=MONITOR_SOURCE,
            )

        # Falling through to the rate-limited endpoint; ration it again.
        self.min_interval_seconds = self.oauth_min_interval_seconds
        logger.info("claude-monitor state unusable (%s); falling back to %s", monitor_note, USAGE_URL)

        def fallback_message(detail: str) -> str:
            return f"{detail} (claude-monitor: {monitor_note})"

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

        headers = {
            **USAGE_HEADERS,
            "Authorization": f"Bearer {access_token}",
        }
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
                source="oauth/usage",
            )

        http_status = resp.status_code
        if http_status == 429:
            self.retry_after_seconds = parse_retry_after(resp.headers.get("Retry-After"))
            wait = self.retry_after_seconds
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
                source="oauth/usage",
            )
        if http_status in (401, 403):
            err = (resp.text or resp.reason_phrase or "")[:300]
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.AUTH_ERROR,
                fallback_message(f"Auth failed ({http_status}). Re-run `claude login`. {err}".strip()),
                account_hint=hint,
                source="oauth/usage",
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
                source="oauth/usage",
            )

        windows = parse_claude_usage(payload)
        # This payload already carries the scoped windows, so seed the cache
        # from it rather than spending a second call on them later.
        self._store_scoped(parse_scoped_usage(payload), now=utcnow())
        return AccountSnapshot(
            provider=self.provider_id,
            display_name=self.display_name,
            account_hint=hint,
            status=SnapshotStatus.OK,
            message=None,
            windows=windows,
            fetched_at=utcnow(),
            source="oauth/usage",
        )
