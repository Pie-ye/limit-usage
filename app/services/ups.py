"""Read UPS status via upower CLI (host-local; limit-usage runs on host)."""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any


DEFAULT_DEVICE = "/org/freedesktop/UPower/devices/ups_hiddev1"


def _parse_upower(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        data[key.strip().lower()] = val.strip()
    return data


def read_ups(device: str = DEFAULT_DEVICE) -> dict[str, Any]:
    """Return flat UPS fields for customapi. Never raises — degrades to status=error."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    upower = shutil.which("upower")
    if not upower:
        return {
            "percent": None,
            "state": "unavailable",
            "state_display": "無法取得",
            "on_battery": None,
            "time_to_empty_hours": None,
            "model": None,
            "status": "error",
            "message": "upower not found",
            "updated_at": now,
        }

    try:
        proc = subprocess.run(
            [upower, "-i", device],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "percent": None,
            "state": "unavailable",
            "state_display": "無法取得",
            "on_battery": None,
            "time_to_empty_hours": None,
            "model": None,
            "status": "error",
            "message": str(exc),
            "updated_at": now,
        }

    if proc.returncode != 0 or not proc.stdout.strip():
        return {
            "percent": None,
            "state": "unavailable",
            "state_display": "無法取得",
            "on_battery": None,
            "time_to_empty_hours": None,
            "model": None,
            "status": "error",
            "message": (proc.stderr or proc.stdout or "upower failed").strip()[:200],
            "updated_at": now,
        }

    fields = _parse_upower(proc.stdout)
    pct_raw = fields.get("percentage", "")
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", pct_raw)
    percent = float(m.group(1)) if m else None

    state = fields.get("state") or fields.get("warning-level") or "unknown"
    # upower state examples: fully-charged, charging, discharging
    on_battery = state.lower() in {"discharging", "pending-charge", "empty"}

    tte_raw = fields.get("time to empty", "")
    tte_hours = None
    # e.g. "4.9 hours" or "minutes"
    hm = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*hours?", tte_raw, re.I)
    mm = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*minutes?", tte_raw, re.I)
    if hm:
        tte_hours = float(hm.group(1))
    elif mm:
        tte_hours = float(mm.group(1)) / 60.0

    state_display = {
        "fully-charged": "已充滿",
        "charging": "充電中",
        "discharging": "使用電池",
        "pending-charge": "等待充電",
        "empty": "電量耗盡",
    }.get(state.lower(), state)

    return {
        "percent": percent,
        "state": state,
        "state_display": state_display,
        "on_battery": on_battery,
        "time_to_empty_hours": tte_hours,
        "model": fields.get("model"),
        "vendor": fields.get("vendor"),
        "status": "ok",
        "message": None,
        "updated_at": now,
    }
