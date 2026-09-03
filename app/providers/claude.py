from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from app.models import AccountSnapshot, ProviderId, SnapshotStatus, UsageWindow, utcnow
from app.providers.base import error_snapshot


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


def parse_claude_credentials(data: dict[str, Any]) -> tuple[SnapshotStatus, str, list[UsageWindow], str | None]:
    oauth = data.get("claudeAiOauth")
    if not oauth or not isinstance(oauth, dict):
        return SnapshotStatus.AUTH_ERROR, "No claudeAiOauth found in credentials", [], None

    sub_type = oauth.get("subscriptionType")
    tier = oauth.get("rateLimitTier")
    expires_at_ms = oauth.get("expiresAt")
    scopes = oauth.get("scopes") or []

    resets_at = None
    if isinstance(expires_at_ms, (int, float)) and expires_at_ms > 0:
        resets_at = datetime.fromtimestamp(expires_at_ms / 1000, tz=timezone.utc)

    sub_display = format_subscription(sub_type)
    tier_display = format_tier(tier)

    # Calculate remaining / used percentage based on standard 5-hour rate-limit window
    used_pct = None
    rem_pct = None
    if resets_at is not None:
        rem_sec = max(0.0, (resets_at - utcnow()).total_seconds())
        window_sec = 18000.0  # 5 hours
        rem_val = min(100.0, max(0.0, (rem_sec / window_sec) * 100.0))
        rem_pct = round(rem_val, 1)
        used_pct = round(100.0 - rem_val, 1)

    window = UsageWindow(
        key="weekly",
        label="Claude 額度",
        used_percent=used_pct,
        remaining_percent=rem_pct,
        resets_at=resets_at,
        limit_window_seconds=18000,
        amount=None,
        currency="%",
        raw_extra={
            "subscription_type": sub_type,
            "rate_limit_tier": tier,
            "tier_display": tier_display,
            "scopes": scopes,
        },
    )

    hint = f"Claude ({tier_display})"
    return SnapshotStatus.OK, "Claude credentials active", [window], hint


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

        status, msg, windows, hint = parse_claude_credentials(data)
        if status != SnapshotStatus.OK:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                status,
                msg,
                account_hint=hint,
                source="credentials",
            )

        return AccountSnapshot(
            provider=self.provider_id,
            display_name=self.display_name,
            account_hint=hint,
            status=status,
            message=msg,
            windows=windows,
            fetched_at=utcnow(),
            source="credentials",
        )
