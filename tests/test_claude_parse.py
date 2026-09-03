from datetime import datetime, timezone
from pathlib import Path
import pytest

from app.models import SnapshotStatus
from app.providers.claude import (
    ClaudeProvider,
    format_subscription,
    format_tier,
    parse_claude_credentials,
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


def test_parse_claude_credentials_valid():
    sample = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-test",
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_5x",
            "expiresAt": 1756888102000,
            "scopes": ["user:file_upload", "user:inference"],
        }
    }
    status, msg, windows, hint = parse_claude_credentials(sample)
    assert status == SnapshotStatus.OK
    assert len(windows) == 1
    w = windows[0]
    assert w.amount == "5x 額度"
    assert w.currency == "額度重置"
    assert w.resets_at == datetime.fromtimestamp(1756888102, tz=timezone.utc)
    assert hint == "額度重置 (5x 額度)"


def test_parse_claude_credentials_invalid():
    sample = {"other": {}}
    status, msg, windows, hint = parse_claude_credentials(sample)
    assert status == SnapshotStatus.AUTH_ERROR
    assert len(windows) == 0


@pytest.mark.asyncio
async def test_claude_provider_fetch_missing(tmp_path: Path):
    missing_file = tmp_path / "not_found.json"
    provider = ClaudeProvider(missing_file)
    snap = await provider.fetch()
    assert snap.status == SnapshotStatus.AUTH_ERROR
