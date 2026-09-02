"""Tests for system_health service mirroring Homepage system_health_api."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.services import system_health


NOW = datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc)  # 14:00 Asia/Taipei
DEPLOYMENT_STATUS = {
    "schema_version": 1,
    "schedules": {
        "offsite_backup": {
            "unit": "three-host-offsite-backup.timer",
            "state": "acceptance-gated",
        },
        "health_verification": {
            "unit": "three-host-health.timer",
            "state": "acceptance-gated",
        },
    },
}
ALL_OK_CHECKS = [
    {"name": "nas-docker", "ok": True, "detail": "30 containers running"},
    {"name": "rsync-timer", "ok": True, "detail": "rsync timer active"},
    {"name": "restic-repository", "ok": True, "detail": "repository check passed"},
    {"name": "nas-disk", "ok": True, "detail": "disk usage 9%"},
    {"name": "oracle-disk", "ok": True, "detail": "disk usage 1%"},
]

COMMON_FIELDS = (
    "status",
    "rsync_status",
    "rsync_display",
    "rsync_stale",
    "restic_status",
    "restic_display",
    "verification_status",
    "verification_display",
    "verification_updated_at",
    "daily_rsync",
    "daily_evidence",
    "daily_timer",
    "daily_continuity",
    "weekly_timeshift",
    "weekly_restic",
)


class SystemHealthEnv:
    def __init__(self, root: Path) -> None:
        self.state = root / "three-host"
        self.rsync = root / "rsync" / "Container"
        self.deployment = root / "deployment-status.json"
        self.state.mkdir(parents=True, exist_ok=True)
        self.rsync.mkdir(parents=True, exist_ok=True)
        self.write_deployment()

    def write_deployment(self, value: object = DEPLOYMENT_STATUS) -> None:
        self.deployment.write_text(json.dumps(value), encoding="utf-8")

    def write_health(self, stamp: str, checks: object = ALL_OK_CHECKS) -> Path:
        path = self.state / f"health-{stamp}.json"
        path.write_text(json.dumps(checks), encoding="utf-8")
        return path

    def make_snapshot(self, stamp: str) -> Path:
        path = self.rsync / stamp
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_weekly(
        self,
        *,
        updated_at: str = "2026-08-31T05:00:00+08:00",
        timeshift_status: str = "ok",
        timeshift_display: str = "3 天前",
        retire_status: str = "ok",
        retire_display: str = "運行中",
        continuity_status: str = "ok",
        continuity_display: str = "過去 7 天連續",
    ) -> Path:
        path = self.state / "weekly-verification.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "updated_at": updated_at,
                    "timeshift": {"status": timeshift_status, "display": timeshift_display},
                    "retire_timer": {"status": retire_status, "display": retire_display},
                    "rsync_continuity": {
                        "status": continuity_status,
                        "display": continuity_display,
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def read(self) -> dict[str, object]:
        return system_health.read_system_health(
            self.state, self.rsync, self.deployment, NOW
        )


def test_docker_compose_mount_contract() -> None:
    compose_path = Path("docker-compose.yml")
    if not compose_path.exists():
        compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = compose_path.read_text(encoding="utf-8")
    assert (
        "${HOME}/Container/scripts/three_host/deployment-status.json:/secrets/three-host-deployment-status.json:ro"
        in compose
    )


def test_today_rsync_and_stale_verification_are_independent(tmp_path: Path) -> None:
    env = SystemHealthEnv(tmp_path)
    env.make_snapshot("2026-09-01_03-35-00")
    env.write_health("20260831T000000000000Z")  # 30 hours old

    data = env.read()

    for field in COMMON_FIELDS:
        assert field in data
    assert data["rsync_status"] == "ok"
    assert data["rsync_display"] == "今日 03:35 成功"
    assert data["rsync_stale"] is False
    assert data["restic_status"] == "disabled"
    assert data["restic_display"] == "尚未啟用"
    assert data["verification_status"] == "stale"
    assert data["verification_display"] == "驗證已過期"
    assert data["status"] == "warn"


def test_fresh_verification_cannot_hide_stale_rsync(tmp_path: Path) -> None:
    env = SystemHealthEnv(tmp_path)
    env.make_snapshot("2026-08-31_03-35-00")
    env.write_health("20260901T050000000000Z")

    data = env.read()

    for field in COMMON_FIELDS:
        assert field in data
    assert data["verification_status"] == "ok"
    assert data["rsync_status"] == "stale"
    assert data["status"] != "ok"
    assert data["status"] == "warn"


def test_failed_check_warns_verification(tmp_path: Path) -> None:
    env = SystemHealthEnv(tmp_path)
    env.make_snapshot("2026-09-01_03-35-00")
    checks = [
        ALL_OK_CHECKS[0],
        {"name": "tailscale-peer", "ok": False, "detail": "unreachable"},
    ]
    env.write_health("20260901T050000000000Z", checks)

    data = env.read()

    for field in COMMON_FIELDS:
        assert field in data
    assert data["verification_status"] == "warn"
    assert data["verification_display"] == "驗證有異常"
    assert data["status"] == "warn"


def test_restic_check_does_not_override_gated_deployment(tmp_path: Path) -> None:
    env = SystemHealthEnv(tmp_path)
    env.make_snapshot("2026-09-01_03-35-00")
    env.write_health(
        "20260901T050000000000Z",
        [{"name": "restic-repository", "ok": True, "detail": "ok"}],
    )

    data = env.read()

    for field in COMMON_FIELDS:
        assert field in data
    assert data["restic_status"] == "disabled"
    assert data["restic_display"] == "尚未啟用"
    assert data["health_schedule_status"] == "disabled"
    assert data["health_schedule_display"] == "排程尚未啟用"
    assert data["status"] == "warn"


def test_invalid_deployment_state_is_unknown_error(tmp_path: Path) -> None:
    env = SystemHealthEnv(tmp_path)
    env.make_snapshot("2026-09-01_03-35-00")
    env.write_health("20260901T050000000000Z")
    cases = {
        "missing": None,
        "corrupt": "{",
        "boolean-schema": json.dumps(
            {**DEPLOYMENT_STATUS, "schema_version": True}
        ),
        "unknown": json.dumps(
            {
                "schema_version": 1,
                "schedules": {
                    **DEPLOYMENT_STATUS["schedules"],
                    "unexpected": {"unit": "surprise.timer", "state": "active"},
                },
            }
        ),
    }

    for label, content in cases.items():
        if env.deployment.exists():
            env.deployment.unlink()
        if content is not None:
            env.deployment.write_text(content, encoding="utf-8")

        data = env.read()

        for field in COMMON_FIELDS:
            assert field in data
        assert data["status"] == "error"
        assert data["ok"] is False
        assert data["restic_status"] == "error"
        assert data["restic_display"] == "狀態未知"
        # Independently parsed rsync fact preserved
        assert data["rsync_status"] == "ok"
        assert data["rsync_display"] == "今日 03:35 成功"


def test_missing_or_invalid_rsync_evidence_is_error(tmp_path: Path) -> None:
    env = SystemHealthEnv(tmp_path)
    env.write_health("20260901T050000000000Z")

    for label, setup in (
        ("missing-directory", lambda: env.rsync.rmdir()),
        ("empty-directory", lambda: None),
        ("invalid-name", lambda: (env.rsync / "not-a-snapshot").mkdir()),
    ):
        if env.rsync.exists():
            for child in env.rsync.iterdir():
                child.rmdir()
        else:
            env.rsync.mkdir(parents=True)
        setup()

        data = env.read()

        for field in COMMON_FIELDS:
            assert field in data
        assert data["rsync_status"] == "error"
        assert data["status"] == "error"
        assert data["ok"] is False


def test_invalid_health_evidence_is_verification_error(tmp_path: Path) -> None:
    env = SystemHealthEnv(tmp_path)
    env.make_snapshot("2026-09-01_03-35-00")
    cases = {
        "missing": None,
        "corrupt": "{",
        "non-array": "{}",
        "empty": "[]",
    }

    for label, content in cases.items():
        for health_file in env.state.glob("health-*.json"):
            health_file.unlink()
        if content is not None:
            (env.state / "health-20260901T050000000000Z.json").write_text(
                content, encoding="utf-8"
            )

        data = env.read()

        for field in COMMON_FIELDS:
            assert field in data
        assert data["verification_status"] == "error"
        assert data["status"] == "error"
        assert data["ok"] is False
        # Independently parsed rsync fact preserved
        assert data["rsync_status"] == "ok"
        assert data["rsync_display"] == "今日 03:35 成功"


def test_backward_compatible_fields_remain_verification_based(tmp_path: Path) -> None:
    env = SystemHealthEnv(tmp_path)
    env.make_snapshot("2026-09-01_03-35-00")
    env.write_health("20260831T000000000000Z")

    data = env.read()

    for field in (
        "stale",
        "freshness_display",
        "updated_at",
        "expires_at",
        "age_seconds",
        "checks",
        "nas_disk_percent",
        "oracle_disk_percent",
    ):
        assert field in data
    assert data["stale"] is True
    assert data["freshness_display"] == data["verification_display"]
    assert data["updated_at"] == data["verification_updated_at"]
    assert data["age_seconds"] == 30 * 60 * 60
    assert data["nas_disk_percent"] == 9
    assert data["oracle_disk_percent"] == 1


def test_parity_with_homepage_contract(tmp_path: Path) -> None:
    import sys
    homepage_dir = Path(__file__).resolve().parents[2] / "homepage"
    if str(homepage_dir) not in sys.path:
        sys.path.insert(0, str(homepage_dir))
    import system_health_api

    env = SystemHealthEnv(tmp_path)
    env.make_snapshot("2026-09-01_03-35-00")
    env.write_health("20260901T050000000000Z")

    limit_data = env.read()
    hp_data = system_health_api.read_system_health(
        env.state, env.rsync, env.deployment, NOW
    )

    for field in COMMON_FIELDS:
        assert limit_data[field] == hp_data[field]
    assert limit_data["display_name"] == hp_data["display_name"]
    assert limit_data["stale_after_seconds"] == hp_data["stale_after_seconds"]
    assert limit_data["nas_disk_percent"] == hp_data["nas_disk_percent"]
    assert limit_data["oracle_disk_percent"] == hp_data["oracle_disk_percent"]


def test_weekly_unknown_without_weekly_file(tmp_path: Path) -> None:
    env = SystemHealthEnv(tmp_path)
    env.make_snapshot("2026-09-01_03-35-00")
    env.write_health("20260831T000000000000Z")  # stale evidence, acceptance-gated

    data = env.read()

    assert data["daily_rsync"] == "✓ 今日 03:35 成功"
    assert data["daily_evidence"] == "⚠ 排程未啟用（最後 08/31 08:00）"
    assert data["daily_timer"] == "rsync —（證據過期） · retire —"
    assert data["daily_continuity"] == "— 無資料（待每週檢查更新）"
    assert data["weekly_timeshift"] == "— 無資料（待每週檢查更新）"
    assert data["weekly_restic"] == "⚠ 尚未啟用"


def test_fresh_weekly_file_populates_fields(tmp_path: Path) -> None:
    env = SystemHealthEnv(tmp_path)
    env.make_snapshot("2026-09-01_03-35-00")
    env.write_health("20260901T050000000000Z")
    env.write_weekly()

    data = env.read()

    assert data["daily_rsync"] == "✓ 今日 03:35 成功"
    assert data["daily_evidence"] == "✓ 09/01 13:00"
    assert data["daily_timer"] == "rsync ✓ · retire ✓"
    assert data["daily_continuity"] == "✓ 過去 7 天連續"
    assert data["weekly_timeshift"] == "✓ 3 天前"
    assert data["weekly_restic"] == "⚠ 尚未啟用"


def test_stale_weekly_file_marks_expired(tmp_path: Path) -> None:
    env = SystemHealthEnv(tmp_path)
    env.make_snapshot("2026-09-01_03-35-00")
    env.write_health("20260901T050000000000Z")
    env.write_weekly(updated_at="2026-08-20T05:00:00+08:00")  # > 7 days old

    data = env.read()

    assert data["daily_continuity"] == "⚠ 資料過期（待每週檢查更新）"
    assert data["weekly_timeshift"] == "⚠ 資料過期（待每週檢查更新）"
    assert data["daily_timer"] == "rsync ✓ · retire —（資料過期）"


def test_weekly_warn_timeshift_and_missing_days(tmp_path: Path) -> None:
    env = SystemHealthEnv(tmp_path)
    env.make_snapshot("2026-09-01_03-35-00")
    env.write_health("20260901T050000000000Z")
    env.write_weekly(
        timeshift_status="warn",
        timeshift_display="9 天前（超過 7 天）",
        continuity_status="warn",
        continuity_display="缺少 2 天（2026-08-29 2026-08-30）",
    )

    data = env.read()

    assert data["weekly_timeshift"] == "✗ 9 天前（超過 7 天）"
    assert data["daily_continuity"] == "✗ 缺少 2 天（2026-08-29 2026-08-30）"

