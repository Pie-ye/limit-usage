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

    window = UsageWindow(
        key="quota_reset",
        label="額度重置",
        amount=tier_display,
        currency="額度重置",
        resets_at=resets_at,
        raw_extra={
            "subscription_type": sub_type,
            "rate_limit_tier": tier,
            "scopes": scopes,
        },
    )

    hint = f"額度重置 ({tier_display})"
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
