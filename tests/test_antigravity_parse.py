from datetime import datetime, timezone

from app.models import AccountSnapshot, ProviderId, SnapshotStatus, UsageWindow
from app.providers.antigravity import parse_quota_payload
from app.services.homepage_view import build_homepage_payload


PAYLOAD = {
    "groups": [
        {
            "displayName": "Gemini Models",
            "buckets": [
                {
                    "bucketId": "gemini-weekly",
                    "displayName": "Weekly Limit Remaining",
                    "window": "weekly",
                    "resetTime": "2026-09-02T08:32:33Z",
                    "remainingFraction": 0.9816,
                },
                {
                    "bucketId": "gemini-5h",
                    "displayName": "Five Hour Limit Remaining",
                    "window": "5h",
                    "resetTime": "2026-08-27T05:42:51Z",
                    "remainingFraction": 0.9499,
                },
            ],
        },
        {
            "displayName": "Claude and GPT models",
            "buckets": [
                {
                    "bucketId": "3p-weekly",
                    "displayName": "Weekly Limit Remaining",
                    "window": "weekly",
                    "resetTime": "2026-09-02T15:15:03Z",
                    "remainingFraction": 0.9893,
                },
                {
                    "bucketId": "3p-5h",
                    "displayName": "Five Hour Limit Remaining",
                    "window": "5h",
                    "resetTime": "2026-08-27T06:27:37Z",
                    "remainingFraction": 1.0,
                },
            ],
        },
    ]
}


def test_parse_quota_payload_unique_family_keys():
    windows, message = parse_quota_payload(PAYLOAD)
    assert message is None
    by_key = {w.key: w for w in windows}
    assert set(by_key) == {"1w", "5h", "3p-1w", "3p-5h"}
    assert by_key["1w"].label.startswith("Gemini")
    assert by_key["3p-1w"].label.startswith("Claude/GPT")
    assert by_key["1w"].used_percent == 1.84
    assert by_key["1w"].remaining_percent == 98.16
    assert by_key["3p-1w"].used_percent == 1.07
    assert by_key["3p-1w"].remaining_percent == 98.93
    assert by_key["3p-5h"].used_percent == 0.0
    assert by_key["1w"].raw_extra["family"] == "gemini"
    assert by_key["3p-1w"].raw_extra["family"] == "3p"
    assert by_key["1w"].resets_at == datetime(2026, 9, 2, 8, 32, 33, tzinfo=timezone.utc)


def test_homepage_payload_exposes_gemini_and_other_weekly():
    windows, _ = parse_quota_payload(PAYLOAD)
    snaps = [
        AccountSnapshot(
            provider=ProviderId.ANTIGRAVITY,
            display_name="Antigravity",
            status=SnapshotStatus.OK,
            windows=windows,
            fetched_at=datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc),
        )
    ]
    out = build_homepage_payload(snaps, now=datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc))
    assert out["antigravity_status"] == "ok"
    assert out["antigravity_weekly_used_percent"] == 1.84
    assert out["antigravity_weekly_remaining_percent"] == 98.16
    assert out["antigravity_other_weekly_used_percent"] == 1.07
    assert out["antigravity_other_weekly_remaining_percent"] == 98.93
    assert out["antigravity_resets_at"].startswith("2026-09-02T08:32:33")


def test_homepage_payload_missing_antigravity_has_other_fields():
    out = build_homepage_payload([])
    assert out["antigravity_status"] == "missing"
    assert out["antigravity_weekly_used_percent"] is None
    assert out["antigravity_other_weekly_used_percent"] is None
