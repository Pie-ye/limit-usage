from datetime import datetime, timezone

from app.models import AccountSnapshot, ProviderId, SnapshotStatus, UsageWindow
from app.services.homepage_view import build_homepage_payload


def test_build_homepage_payload_weekly_and_cny():
    snaps = [
        AccountSnapshot(
            provider=ProviderId.CODEX,
            display_name="Codex",
            status=SnapshotStatus.OK,
            windows=[
                UsageWindow(key="5h", label="5 hour", used_percent=10.0, remaining_percent=90.0),
                UsageWindow(
                    key="1w",
                    label="Weekly",
                    used_percent=12.5,
                    remaining_percent=87.5,
                    resets_at=datetime(2026, 8, 8, 5, 0, tzinfo=timezone.utc),
                    limit_window_seconds=604800,
                ),
            ],
            fetched_at=datetime(2026, 8, 1, 5, 0, tzinfo=timezone.utc),
        ),
        AccountSnapshot(
            provider=ProviderId.SUPERGROK,
            display_name="SuperGrok",
            status=SnapshotStatus.OK,
            windows=[
                UsageWindow(
                    key="weekly",
                    label="Weekly",
                    used_percent=41.0,
                    remaining_percent=59.0,
                    resets_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
                ),
                UsageWindow(key="product-2", label="Imagine", used_percent=35.0),
            ],
            fetched_at=datetime(2026, 8, 1, 5, 1, tzinfo=timezone.utc),
        ),
        AccountSnapshot(
            provider=ProviderId.DEEPSEEK,
            display_name="DeepSeek",
            status=SnapshotStatus.OK,
            windows=[
                UsageWindow(
                    key="balance-cny",
                    label="Balance (CNY)",
                    amount="19.52",
                    currency="CNY",
                )
            ],
            fetched_at=datetime(2026, 8, 1, 5, 2, tzinfo=timezone.utc),
        ),
    ]
    out = build_homepage_payload(snaps, now=datetime(2026, 8, 1, 5, 0, tzinfo=timezone.utc))
    assert out["codex_5h_used_percent"] == 10.0
    assert out["codex_5h_remaining_percent"] == 90.0
    assert out["codex_weekly_used_percent"] == 12.5
    assert out["codex_weekly_remaining_percent"] == 87.5
    assert out["codex_status"] == "ok"
    assert out["codex_resets_at"].startswith("2026-08-08")
    assert out["codex_reset_display"] == "7天"
    assert out["grok_weekly_used_percent"] == 41.0
    assert out["grok_status"] == "ok"
    assert out["deepseek_cny"] == 19.52
    assert out["deepseek_status"] == "ok"
    assert out["updated_at"].startswith("2026-08-01")


def test_build_homepage_payload_countdown_units_and_expiry():
    now = datetime(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
    snaps = [
        AccountSnapshot(
            provider=ProviderId.CODEX,
            display_name="Codex",
            status=SnapshotStatus.OK,
            windows=[UsageWindow(
                key="1w",
                label="Weekly",
                resets_at=datetime(2026, 8, 1, 10, 1, tzinfo=timezone.utc),
            )],
            fetched_at=now,
        ),
        AccountSnapshot(
            provider=ProviderId.SUPERGROK,
            display_name="SuperGrok",
            status=SnapshotStatus.OK,
            windows=[UsageWindow(
                key="weekly",
                label="Weekly",
                resets_at=datetime(2026, 8, 1, 4, 59, tzinfo=timezone.utc),
            )],
            fetched_at=now,
        ),
    ]
    out = build_homepage_payload(snaps, now=now)
    assert out["codex_reset_display"] == "5小時 1分"
    assert out["grok_reset_display"] == "已到期"


def test_build_homepage_payload_missing_providers():
    out = build_homepage_payload([])
    assert out["codex_status"] == "missing"
    assert out["grok_status"] == "missing"
    assert out["codex_reset_display"] is None
    assert out["grok_reset_display"] is None
    assert out["deepseek_status"] == "missing"
    assert out["codex_weekly_used_percent"] is None
    assert out["deepseek_cny"] is None


def test_build_homepage_payload_claude_subscription():
    now = datetime(2026, 9, 3, 3, 0, tzinfo=timezone.utc)
    snaps = [
        AccountSnapshot(
            provider=ProviderId.CLAUDE,
            display_name="Claude",
            status=SnapshotStatus.OK,
            windows=[
                UsageWindow(
                    key="5h",
                    label="Claude · 5小時",
                    used_percent=10.0,
                    remaining_percent=90.0,
                    resets_at=datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc),
                    limit_window_seconds=18000,
                ),
                UsageWindow(
                    key="1w",
                    label="Claude · 週額度",
                    used_percent=15.0,
                    remaining_percent=85.0,
                    resets_at=datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc),
                    limit_window_seconds=604800,
                ),
                UsageWindow(
                    key="1w-fable",
                    label="Claude · 週額度 (Fable)",
                    used_percent=20.0,
                    remaining_percent=80.0,
                    resets_at=datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc),
                    limit_window_seconds=604800,
                ),
            ],
            fetched_at=now,
        ),
    ]
    out = build_homepage_payload(snaps, now=now)
    assert out["claude_status"] == "ok"
    assert out["claude_5h_used_percent"] == 10.0
    assert out["claude_weekly_used_percent"] == 15.0
    assert out["claude_weekly_remaining_percent"] == 85.0
    assert out["claude_fable_used_percent"] == 20.0
    assert out["claude_resets_at"].startswith("2026-09-03T08:00")
    assert out["claude_reset_display"] == "5小時 0分"


def test_build_homepage_payload_claude_missing():
    out = build_homepage_payload([])
    assert out["claude_status"] == "missing"
    assert out["claude_5h_used_percent"] is None
    assert out["claude_weekly_used_percent"] is None
    assert out["claude_fable_used_percent"] is None
    assert out["claude_reset_display"] is None
