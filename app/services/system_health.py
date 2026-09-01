"""Read system health evidence and rsync backup status for three-host setup."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_STATE_DIRS = (
    Path("/secrets/three-host"),
    Path("/home/pieye/.local/state/three-host"),
)

DEFAULT_RSYNC_DIRS = (
    Path("/secrets/rsync-backups/Container"),
    Path("/home/pieye/backup/rsync-backups/Container"),
)

DEFAULT_DEPLOYMENT_STATUS_FILES = (
    Path("/secrets/three-host-deployment-status.json"),
    Path("/home/pieye/Container/scripts/three_host/deployment-status.json"),
)

EXPECTED_SCHEDULES = {
    "offsite_backup": ("three-host-offsite-backup.timer", "acceptance-gated"),
    "health_verification": ("three-host-health.timer", "acceptance-gated"),
}
STALE_AFTER_SECONDS = 93600  # 26 hours
TAIPEI = timezone(timedelta(hours=8))
SNAPSHOT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
HEALTH_PATTERN = re.compile(r"^health-(\d{8}T\d{12}Z)\.json$")

CHECK_LABELS = {
    "nas-docker": "Docker 容器",
    "rsync-timer": "本機備份排程",
    "oracle-health": "Oracle 災備節點",
    "tailscale-peer": "Tailscale 通道",
    "restic-repository": "Restic 異地庫",
    "azure-target": "Azure 應急節點",
    "nas-disk": "NAS 磁碟容量",
    "oracle-disk": "Oracle 磁碟容量",
}


class SystemHealthError(Exception):
    def __init__(self, message: str, *, status: int = 500, error_code: str = "system_health_failed"):
        super().__init__(message)
        self.status = status
        self.error_code = error_code


def _resolve_dir(candidates: tuple[Path, ...], explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def _resolve_file(candidates: tuple[Path, ...], explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _find_latest_health_file(state_dir: Path) -> Path | None:
    try:
        files = sorted(state_dir.glob("health-*.json"))
    except OSError:
        return None
    return files[-1] if files else None


def _parse_snapshot_time(name: str) -> datetime | None:
    if not SNAPSHOT_PATTERN.fullmatch(name):
        return None
    try:
        return datetime.strptime(name, "%Y-%m-%d_%H-%M-%S").replace(tzinfo=TAIPEI)
    except ValueError:
        return None


def _find_latest_rsync_snapshot(rsync_dir: Path) -> tuple[str | None, datetime | None]:
    if not rsync_dir.is_dir():
        return None, None

    candidates: dict[str, Path] = {}
    latest_link = rsync_dir / "latest"
    try:
        if latest_link.exists():
            target = latest_link.resolve()
            if target.is_dir() and _parse_snapshot_time(target.name) is not None:
                candidates[target.name] = target
        for path in rsync_dir.iterdir():
            if path.is_dir() and _parse_snapshot_time(path.name) is not None:
                candidates[path.name] = path
    except OSError:
        return None, None

    if not candidates:
        return None, None
    name = max(candidates)
    return name, _parse_snapshot_time(name)


def _read_deployment_status(path: Path) -> dict[str, str]:
    unknown = {
        "restic_status": "error",
        "restic_display": "狀態未知",
        "health_schedule_status": "error",
        "health_schedule_display": "狀態未知",
    }
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return unknown

    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
    ):
        return unknown
    schedules = document.get("schedules")
    if not isinstance(schedules, dict) or set(schedules) != set(EXPECTED_SCHEDULES):
        return unknown
    for name, (expected_unit, expected_state) in EXPECTED_SCHEDULES.items():
        entry = schedules.get(name)
        if not isinstance(entry, dict) or set(entry) != {"unit", "state"}:
            return unknown
        if entry.get("unit") != expected_unit or entry.get("state") != expected_state:
            return unknown

    return {
        "restic_status": "disabled",
        "restic_display": "尚未啟用",
        "health_schedule_status": "disabled",
        "health_schedule_display": "排程尚未啟用",
    }


def _extract_percent(detail: str) -> int | None:
    match = re.search(r"(\d+)%", detail)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _read_rsync_fact(rsync_dir: Path, now_dt: datetime) -> dict[str, Any]:
    _, rsync_dt = _find_latest_rsync_snapshot(rsync_dir)
    if rsync_dt is None:
        return {
            "rsync_status": "error",
            "rsync_display": "狀態未知",
            "rsync_stale": False,
            "rsync_last_run": None,
            "rsync_age_seconds": None,
        }

    rsync_utc = rsync_dt.astimezone(timezone.utc)
    rsync_age = max(0, int((now_dt - rsync_utc).total_seconds()))
    rsync_stale = rsync_age > STALE_AFTER_SECONDS
    now_taipei = now_dt.astimezone(TAIPEI)
    if rsync_stale:
        display = f"已過期（{rsync_dt:%m/%d %H:%M}）"
    elif rsync_dt.date() == now_taipei.date():
        display = f"今日 {rsync_dt:%H:%M} 成功"
    elif rsync_dt.date() == (now_taipei.date() - timedelta(days=1)):
        display = f"昨日 {rsync_dt:%H:%M}"
    else:
        display = f"{rsync_dt:%m/%d %H:%M}"

    return {
        "rsync_status": "stale" if rsync_stale else "ok",
        "rsync_display": display,
        "rsync_stale": rsync_stale,
        "rsync_last_run": rsync_utc.isoformat().replace("+00:00", "Z"),
        "rsync_age_seconds": rsync_age,
    }


def _verification_error() -> dict[str, Any]:
    return {
        "verification_status": "error",
        "verification_display": "狀態未知",
        "verification_updated_at": None,
        "expires_at": None,
        "age_seconds": None,
        "healthy_checks": 0,
        "total_checks": 0,
        "checks_display": "—",
        "checks": [],
        "nas_disk_percent": None,
        "oracle_disk_percent": None,
    }


def _read_verification_fact(state_dir: Path, now_dt: datetime) -> dict[str, Any]:
    if not state_dir.is_dir():
        return _verification_error()
    health_file = _find_latest_health_file(state_dir)
    if health_file is None or not health_file.is_file():
        return _verification_error()

    match = HEALTH_PATTERN.fullmatch(health_file.name)
    if match is None:
        return _verification_error()
    try:
        updated_at_dt = datetime.strptime(
            match.group(1), "%Y%m%dT%H%M%S%fZ"
        ).replace(tzinfo=timezone.utc)
        raw_checks = json.loads(health_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return _verification_error()
    if not isinstance(raw_checks, list) or not raw_checks:
        return _verification_error()

    checks: list[dict[str, Any]] = []
    healthy_count = 0
    nas_disk_pct = None
    oracle_disk_pct = None
    for item in raw_checks:
        if not isinstance(item, dict):
            return _verification_error()
        name = str(item.get("name") or "").strip()
        ok = bool(item.get("ok"))
        detail = str(item.get("detail") or "").strip()
        if ok:
            healthy_count += 1
        if name == "nas-disk":
            nas_disk_pct = _extract_percent(detail)
        elif name == "oracle-disk":
            oracle_disk_pct = _extract_percent(detail)
        checks.append(
            {
                "name": name,
                "label": CHECK_LABELS.get(name, name),
                "ok": ok,
                "detail": detail,
            }
        )

    age_seconds = max(0, int((now_dt - updated_at_dt).total_seconds()))
    expires_at_dt = updated_at_dt + timedelta(seconds=STALE_AFTER_SECONDS)
    if age_seconds > STALE_AFTER_SECONDS:
        status = "stale"
        display = "驗證已過期"
    elif healthy_count == len(checks):
        status = "ok"
        display = "驗證正常"
    else:
        status = "warn"
        display = "驗證有異常"

    return {
        "verification_status": status,
        "verification_display": display,
        "verification_updated_at": updated_at_dt.isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at_dt.isoformat().replace("+00:00", "Z"),
        "age_seconds": age_seconds,
        "healthy_checks": healthy_count,
        "total_checks": len(checks),
        "checks_display": f"{healthy_count}/{len(checks)} 正常",
        "checks": checks,
        "nas_disk_percent": nas_disk_pct,
        "oracle_disk_percent": oracle_disk_pct,
    }


def read_system_health(
    state_dir_override: Path | None = None,
    rsync_dir_override: Path | None = None,
    deployment_status_override: Path | None = None,
    now_override: datetime | None = None,
) -> dict[str, Any]:
    state_dir = _resolve_dir(DEFAULT_STATE_DIRS, state_dir_override)
    rsync_dir = _resolve_dir(DEFAULT_RSYNC_DIRS, rsync_dir_override)
    deployment_path = _resolve_file(
        DEFAULT_DEPLOYMENT_STATUS_FILES, deployment_status_override
    )
    now_dt = now_override or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    else:
        now_dt = now_dt.astimezone(timezone.utc)

    payload: dict[str, Any] = {
        "display_name": "系統備份",
        "stale_after_seconds": STALE_AFTER_SECONDS,
    }
    payload.update(_read_rsync_fact(rsync_dir, now_dt))
    payload.update(_read_deployment_status(deployment_path))
    payload.update(_read_verification_fact(state_dir, now_dt))

    required_statuses = (
        payload["rsync_status"],
        payload["restic_status"],
        payload["verification_status"],
    )
    if any(value == "error" for value in required_statuses):
        status, ok = "error", False
    elif (
        payload["rsync_status"] == "stale"
        or payload["verification_status"] in {"stale", "warn"}
        or payload["restic_status"] == "disabled"
    ):
        status, ok = "warn", True
    else:
        status, ok = "ok", True

    payload["status"] = status
    payload["ok"] = ok
    payload["stale"] = payload["verification_status"] == "stale"
    payload["freshness_display"] = payload["verification_display"]
    payload["updated_at"] = payload["verification_updated_at"]
    return payload
