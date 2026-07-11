from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.models import AccountSnapshot, ProviderId, SnapshotStatus, UsageWindow, utcnow
from app.providers.base import error_snapshot, http_get_json

logger = logging.getLogger(__name__)

WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
FIVE_HOUR_SECONDS = 18000
WEEK_SECONDS = 604800
WINDOW_TOLERANCE = 600  # seconds


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # seconds or ms
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _parse_datetime(int(text))
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None
    return None


def _percent_left(window: dict[str, Any]) -> float | None:
    for key in ("percent_left", "remaining_percent", "percent_remaining"):
        if key in window and window[key] is not None:
            try:
                return float(window[key])
            except (TypeError, ValueError):
                pass
    used = window.get("used_percent")
    if used is not None:
        try:
            return max(0.0, min(100.0, 100.0 - float(used)))
        except (TypeError, ValueError):
            return None
    return None


def _limit_seconds(window: dict[str, Any]) -> int | None:
    for key in ("limit_window_seconds", "limit_window", "window_seconds"):
        if key in window and window[key] is not None:
            try:
                return int(window[key])
            except (TypeError, ValueError):
                pass
    return None


def classify_window(limit_seconds: int | None, name_hint: str = "") -> tuple[str, str]:
    hint = name_hint.lower()
    if limit_seconds is not None:
        if abs(limit_seconds - FIVE_HOUR_SECONDS) <= WINDOW_TOLERANCE:
            return "5h", "5-hour"
        if abs(limit_seconds - WEEK_SECONDS) <= WINDOW_TOLERANCE:
            return "1w", "Weekly"
        if limit_seconds <= 6 * 3600:
            return "5h", "5-hour"
        if limit_seconds >= 3 * 24 * 3600:
            return "1w", "Weekly"
    if "week" in hint or "secondary" in hint:
        return "1w", "Weekly"
    if "hour" in hint or "5h" in hint or "primary" in hint or "session" in hint:
        return "5h", "5-hour"
    return "window", name_hint or "Window"


def parse_rate_limit_windows(payload: dict[str, Any]) -> list[UsageWindow]:
    rate = payload.get("rate_limit") or payload.get("rateLimit") or {}
    raw_windows: list[tuple[str, dict[str, Any]]] = []

    primary = rate.get("primary_window") or rate.get("primaryWindow")
    secondary = rate.get("secondary_window") or rate.get("secondaryWindow")
    if isinstance(primary, dict):
        raw_windows.append(("primary", primary))
    if isinstance(secondary, dict):
        raw_windows.append(("secondary", secondary))

    additional = rate.get("additional_rate_limits") or rate.get("additionalRateLimits") or []
    if isinstance(additional, list):
        for i, item in enumerate(additional):
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("feature") or f"extra_{i}")
                raw_windows.append((name, item))

    # Some payloads nest under code_review / other keys with similar shapes
    if not raw_windows:
        for key, value in rate.items():
            if isinstance(value, dict) and (
                "percent_left" in value
                or "limit_window_seconds" in value
                or "reset_at" in value
            ):
                raw_windows.append((str(key), value))

    windows: list[UsageWindow] = []
    seen_keys: set[str] = set()
    for hint, win in raw_windows:
        limit_s = _limit_seconds(win)
        key, label = classify_window(limit_s, hint)
        if key in seen_keys:
            key = f"{key}-{hint}"
            label = f"{label} ({hint})"
        seen_keys.add(key)
        remaining = _percent_left(win)
        used = None if remaining is None else max(0.0, min(100.0, 100.0 - remaining))
        reset = _parse_datetime(
            win.get("reset_at")
            or win.get("resets_at")
            or win.get("reset_time")
            or win.get("resetAt")
        )
        windows.append(
            UsageWindow(
                key=key,
                label=label,
                used_percent=used,
                remaining_percent=remaining,
                resets_at=reset,
                limit_window_seconds=limit_s,
                raw_extra={
                    k: v
                    for k, v in win.items()
                    if k
                    not in {
                        "percent_left",
                        "reset_at",
                        "limit_window_seconds",
                        "used_percent",
                    }
                },
            )
        )
    # Prefer 5h before 1w for stable UI order
    order = {"5h": 0, "1w": 1}
    windows.sort(key=lambda w: order.get(w.key.split("-")[0], 50))
    return windows


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        data = base64.urlsafe_b64decode(payload + padding)
        return json.loads(data)
    except Exception:
        return {}


def load_codex_auth(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Codex auth file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def extract_tokens(auth: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return (access_token, refresh_token, account_hint)."""
    access = (
        auth.get("access_token")
        or auth.get("accessToken")
        or (auth.get("tokens") or {}).get("access_token")
        or (auth.get("tokens") or {}).get("accessToken")
    )
    refresh = (
        auth.get("refresh_token")
        or auth.get("refreshToken")
        or (auth.get("tokens") or {}).get("refresh_token")
        or (auth.get("tokens") or {}).get("refreshToken")
    )
    hint = auth.get("email") or auth.get("account_id") or auth.get("accountId")
    if not hint and access:
        claims = _jwt_claims(str(access))
        hint = claims.get("email") or claims.get("https://api.openai.com/profile", {}).get(
            "email"
        )
        if isinstance(hint, dict):
            hint = hint.get("email")
    return (
        str(access) if access else None,
        str(refresh) if refresh else None,
        str(hint) if hint else None,
    )


async def try_refresh_token(
    client: httpx.AsyncClient,
    refresh_token: str,
) -> str | None:
    """Best-effort OAuth refresh; returns new access token or None."""
    # ChatGPT/Codex OAuth endpoints vary; attempt common OpenAI auth refresh.
    endpoints = [
        "https://auth.openai.com/oauth/token",
        "https://api.openai.com/auth/refresh",
    ]
    for url in endpoints:
        try:
            resp = await client.post(
                url,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                },
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code >= 400:
                continue
            data = resp.json()
            token = data.get("access_token") or data.get("accessToken")
            if token:
                return str(token)
        except Exception as exc:
            logger.debug("Codex refresh via %s failed: %s", url, exc)
    return None


class CodexProvider:
    provider_id = ProviderId.CODEX
    display_name = "Codex"

    def __init__(self, auth_path: Path, timeout: float = 20.0) -> None:
        self.auth_path = auth_path
        self.timeout = timeout

    async def fetch(self) -> AccountSnapshot:
        try:
            auth = load_codex_auth(self.auth_path)
        except FileNotFoundError as exc:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.AUTH_ERROR,
                str(exc),
                source="auth.json",
            )
        except (OSError, json.JSONDecodeError) as exc:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.AUTH_ERROR,
                f"Failed to read auth file: {exc}",
                source="auth.json",
            )

        access, refresh, hint = extract_tokens(auth)
        if not access:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.AUTH_ERROR,
                "No access_token in Codex auth.json. Run `codex login`.",
                account_hint=hint,
                source="auth.json",
            )

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            status, data, err = await http_get_json(
                client,
                WHAM_USAGE_URL,
                headers={
                    "Authorization": f"Bearer {access}",
                    "Accept": "application/json",
                    "User-Agent": "limit-usage/0.1",
                },
            )
            if status in (401, 403) and refresh:
                new_access = await try_refresh_token(client, refresh)
                if new_access:
                    access = new_access
                    status, data, err = await http_get_json(
                        client,
                        WHAM_USAGE_URL,
                        headers={
                            "Authorization": f"Bearer {access}",
                            "Accept": "application/json",
                            "User-Agent": "limit-usage/0.1",
                        },
                    )

        if status == 429:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.RATE_LIMITED,
                "Codex usage API rate limited",
                account_hint=hint,
                source="wham/usage",
            )
        if status in (401, 403):
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.AUTH_ERROR,
                f"Auth failed ({status}). Re-run `codex login`. {err or ''}".strip(),
                account_hint=hint,
                source="wham/usage",
            )
        if not isinstance(data, dict):
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.ERROR,
                err or f"Unexpected response (HTTP {status})",
                account_hint=hint,
                source="wham/usage",
            )

        windows = parse_rate_limit_windows(data)
        if not windows:
            return AccountSnapshot(
                provider=self.provider_id,
                display_name=self.display_name,
                account_hint=hint,
                status=SnapshotStatus.OK,
                message="Usage payload had no rate-limit windows",
                windows=[],
                fetched_at=utcnow(),
                source="wham/usage",
            )

        return AccountSnapshot(
            provider=self.provider_id,
            display_name=self.display_name,
            account_hint=hint,
            status=SnapshotStatus.OK,
            message=None,
            windows=windows,
            fetched_at=utcnow(),
            source="wham/usage",
        )
