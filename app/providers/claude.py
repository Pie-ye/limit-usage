from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typing import Any

import httpx

from app.models import AccountSnapshot, ProviderId, SnapshotStatus, UsageWindow, utcnow
from app.providers.base import error_snapshot, http_get_json

# Undocumented endpoint that backs Claude Code's own usage HUD/statusline.
# Requires the OAuth access token from ~/.claude/.credentials.json.
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


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


def _usage_window(entry: Any, key: str, label: str) -> UsageWindow | None:
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
            key, label = "5h", "Claude · 5小時"
        elif kind == "weekly_all":
            key, label = "1w", "Claude · 週額度"
        elif kind == "weekly_scoped":
            scope = entry.get("scope") or {}
            model = (scope.get("model") or {}).get("display_name")
            if not model:
                continue
            key, label = f"1w-{_slugify_model(model)}", f"Claude · 週額度 ({model})"
        else:
            continue

        windows.append(
            UsageWindow(
                key=key,
                label=label,
                used_percent=used,
                remaining_percent=remaining,
                resets_at=resets_at,
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
        ("five_hour", "5h", "Claude · 5小時"),
        ("seven_day", "1w", "Claude · 週額度"),
        ("seven_day_opus", "1w-opus", "Claude · 週額度 (Opus)"),
        ("seven_day_sonnet", "1w-sonnet", "Claude · 週額度 (Sonnet)"),
    ]
    windows = []
    for field, key, label in mapping:
        window = _usage_window(payload.get(field), key, label)
        if window is not None:
            windows.append(window)
    return windows


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

    def __init__(self, credentials_path: Path, timeout: float = 20.0) -> None:
        self.credentials_path = credentials_path
        self.timeout = timeout

    async def fetch(self) -> AccountSnapshot:
        if not self.credentials_path or not self.credentials_path.is_file():
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.AUTH_ERROR,
                f"Credentials file not found at {self.credentials_path}",
                source="credentials",
            )

        try:
            content = self.credentials_path.read_text(encoding="utf-8")
            data = json.loads(content)
        except Exception as exc:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.ERROR,
                f"Failed to read credentials: {exc}",
                source="credentials",
            )

        status, msg, access_token, hint = extract_claude_oauth(data)
        if status != SnapshotStatus.OK or not access_token:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                status,
                msg,
                account_hint=hint,
                source="credentials",
            )

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            http_status, payload, err = await http_get_json(
                client,
                USAGE_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "Accept": "application/json",
                    "User-Agent": "limit-usage/0.1",
                },
            )

        if http_status == 429:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.RATE_LIMITED,
                "Claude usage API rate limited",
                account_hint=hint,
                source="oauth/usage",
            )
        if http_status in (401, 403):
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.AUTH_ERROR,
                f"Auth failed ({http_status}). Re-run `claude login`. {err or ''}".strip(),
                account_hint=hint,
                source="oauth/usage",
            )
        if not isinstance(payload, dict):
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.ERROR,
                err or f"Unexpected response (HTTP {http_status})",
                account_hint=hint,
                source="oauth/usage",
            )

        windows = parse_claude_usage(payload)
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
