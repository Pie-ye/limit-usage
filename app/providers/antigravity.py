"""Google Antigravity usage via Cloud Code quota summary (OAuth)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.models import AccountSnapshot, ProviderId, SnapshotStatus, UsageWindow, utcnow
from app.providers.base import error_snapshot

logger = logging.getLogger(__name__)

# agy CLI /usage uses the daily channel, not prod cloudcode-pa.
# The two hosts return different remainingFraction/resetTime for the same account.
CLOUD_CODE_BASE = "https://daily-cloudcode-pa.googleapis.com"
CLOUD_CODE_SOURCE = "daily-cloudcode-pa.googleapis.com"
QUOTA_SUMMARY_PATH = "/v1internal:retrieveUserQuotaSummary"
TOKEN_URI_DEFAULT = "https://oauth2.googleapis.com/token"
DEFAULT_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
DEFAULT_CLIENT_SECRET = "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"
USER_AGENT = "antigravity"
REFRESH_SKEW = timedelta(minutes=5)

WEEK_SECONDS = 604800
FIVE_HOUR_SECONDS = 18000
_WINDOW_SECONDS = {"weekly": WEEK_SECONDS, "5h": FIVE_HOUR_SECONDS, "1w": WEEK_SECONDS}
_WINDOW_BASE = {"weekly": "1w", "week": "1w", "1w": "1w", "5h": "5h"}
_FAMILY_LABEL = {"gemini": "Gemini", "3p": "Claude/GPT"}
_WINDOW_LABEL = {"1w": "週", "5h": "5小時"}


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def load_token(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _token_fields(token: dict[str, Any]) -> dict[str, Any]:
    return {
        "refresh": token.get("refresh_token") or token.get("refreshToken"),
        "client_id": token.get("client_id") or DEFAULT_CLIENT_ID,
        "client_secret": token.get("client_secret") or DEFAULT_CLIENT_SECRET,
        "token_uri": token.get("token_uri") or TOKEN_URI_DEFAULT,
        "email": token.get("email") or token.get("account_hint"),
        "access": token.get("access_token") or token.get("accessToken"),
        "expires_at": token.get("expires_at") or token.get("expiresAt"),
        "project_id": token.get("project_id") or token.get("projectId"),
    }


def _access_expired(expires_at: Any, now: datetime) -> bool:
    if expires_at is None:
        return True
    try:
        ts = float(expires_at)
        if ts > 1e12:
            ts /= 1000.0
        return now >= datetime.fromtimestamp(ts, tz=timezone.utc) - REFRESH_SKEW
    except (TypeError, ValueError, OSError, OverflowError):
        return True


def _quota_family(bucket_id: str, group_name: str) -> str:
    bid = (bucket_id or "").lower()
    group = (group_name or "").lower()
    if bid.startswith("gemini") or "gemini" in group:
        return "gemini"
    if bid.startswith("3p") or "claude" in group or "gpt" in group:
        return "3p"
    if bid and "-" in bid:
        return bid.split("-", 1)[0]
    return "other"


def _window_key(family: str, window: str, bucket_id: str) -> str:
    base = _WINDOW_BASE.get(window) or _WINDOW_BASE.get((bucket_id or "").rsplit("-", 1)[-1])
    if not base:
        base = window or (bucket_id or "quota")
    if family == "gemini":
        return base
    return f"{family}-{base}"


def _window_label(family: str, key: str) -> str:
    base = key.split("-")[-1] if key else ""
    family_label = _FAMILY_LABEL.get(family, family or "其他")
    window_label = _WINDOW_LABEL.get(base, base or "額度")
    return f"{family_label} · {window_label}"


def _sort_windows(windows: list[UsageWindow]) -> list[UsageWindow]:
    family_order = {"gemini": 0, "3p": 1}
    window_order = {"1w": 0, "5h": 1}

    def sort_key(w: UsageWindow) -> tuple[int, int, str]:
        extra = w.raw_extra or {}
        family = str(extra.get("family") or "")
        base = (w.key or "").split("-")[-1]
        return (family_order.get(family, 9), window_order.get(base, 9), w.key)

    windows.sort(key=sort_key)
    return windows


def parse_quota_payload(payload: dict[str, Any]) -> tuple[list[UsageWindow], str | None]:
    """Build usage windows from retrieveUserQuotaSummary groups/buckets.

    Response shape (verified live):
      groups: [{displayName, description,
                buckets: [{bucketId, displayName, window ("weekly"|"5h"),
                           resetTime (ISO), description, remainingFraction}]}]

    Keys stay unique across families so Gemini does not overwrite Claude/GPT:
      gemini weekly/5h -> 1w / 5h (keeps existing trends continuity)
      Claude/GPT weekly/5h -> 3p-1w / 3p-5h
    """
    groups = payload.get("groups") or []
    if not isinstance(groups, list):
        return [], "Quota summary 回應中沒有 groups"

    windows: list[UsageWindow] = []
    seen: set[str] = set()
    for gi, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("displayName") or f"Group {gi}")
        buckets = group.get("buckets") or []
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            bucket_id = str(bucket.get("bucketId") or bucket.get("bucket_id") or "")
            window = str(bucket.get("window") or "").lower()
            family = _quota_family(bucket_id, group_name)
            key = _window_key(family, window, bucket_id)
            if key in seen:
                key = f"{key}-{bucket_id or gi}"
            seen.add(key)
            fraction = bucket.get("remainingFraction")
            if fraction is None:
                continue
            try:
                remaining = max(0.0, min(100.0, 100.0 * float(fraction)))
            except (TypeError, ValueError):
                continue
            used = max(0.0, min(100.0, 100.0 - remaining))
            reset = _parse_datetime(bucket.get("resetTime") or bucket.get("reset_time"))
            windows.append(
                UsageWindow(
                    key=key,
                    label=_window_label(family, key),
                    used_percent=round(used, 2),
                    remaining_percent=round(remaining, 2),
                    resets_at=reset,
                    limit_window_seconds=_WINDOW_SECONDS.get(window) or _WINDOW_SECONDS.get(key.split("-")[-1]),
                    raw_extra={
                        "family": family,
                        "bucket_id": bucket_id or None,
                        "group": group_name,
                        "description": bucket.get("description"),
                    },
                )
            )

    _sort_windows(windows)
    if not windows:
        return [], "Quota summary 回應中沒有可用的額度資料"
    return windows, None


class AntigravityProvider:
    provider_id = ProviderId.ANTIGRAVITY
    display_name = "Antigravity"

    def __init__(self, token_path: Path, timeout: float = 20.0) -> None:
        self.token_path = token_path
        self.timeout = timeout

    async def fetch(self) -> AccountSnapshot:
        try:
            token = load_token(self.token_path)
        except FileNotFoundError as exc:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.AUTH_ERROR,
                "尚未登入 Antigravity（缺少 acp_token.json）。請執行 "
                "limit-usage/scripts/antigravity_login.py 完成一次 Google 授權。",
                source="acp_token.json",
            )
        except (OSError, json.JSONDecodeError) as exc:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.AUTH_ERROR,
                f"無法讀取 Antigravity 憑證檔: {exc}",
                source="acp_token.json",
            )

        fields = _token_fields(token)
        refresh = fields["refresh"]
        if not refresh:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.AUTH_ERROR,
                "Antigravity 憑證檔缺少 refresh_token，請重新登入。",
                account_hint=fields["email"],
                source="acp_token.json",
            )

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            access = fields["access"]
            now = utcnow()
            if not access or _access_expired(fields["expires_at"], now):
                try:
                    access = await self._refresh_access(client, fields)
                except Exception as exc:
                    logger.warning("Antigravity token refresh failed: %s", exc)
                    access = None
            if not access:
                return error_snapshot(
                    self.provider_id,
                    self.display_name,
                    SnapshotStatus.AUTH_ERROR,
                    "Antigravity 存取憑證刷新失敗，請重新執行登入。",
                    account_hint=fields["email"],
                    source="oauth2.googleapis.com/token",
                )

            payload, err = await self._post_json(
                client, access, QUOTA_SUMMARY_PATH, {}
            )
            if err is not None:
                return self._api_error(err, fields["email"])

            windows, message = parse_quota_payload(payload)
            return AccountSnapshot(
                provider=self.provider_id,
                display_name=self.display_name,
                account_hint=fields["email"],
                status=SnapshotStatus.OK,
                message=message,
                windows=windows,
                fetched_at=utcnow(),
                source=CLOUD_CODE_SOURCE,
            )

    async def _refresh_access(self, client: httpx.AsyncClient, fields: dict[str, Any]) -> str | None:
        resp = await client.post(
            fields["token_uri"],
            data={
                "grant_type": "refresh_token",
                "refresh_token": fields["refresh"],
                "client_id": fields["client_id"],
                "client_secret": fields["client_secret"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            logger.warning("Antigravity refresh HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        return str(data.get("access_token") or "") or None

    async def _post_json(
        self, client: httpx.AsyncClient, access: str, path: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        try:
            resp = await client.post(
                f"{CLOUD_CODE_BASE}{path}",
                headers={
                    "Authorization": f"Bearer {access}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
                json=body,
            )
        except httpx.HTTPError as exc:
            return {}, f"HTTP request failed: {exc}"
        if resp.status_code == 429:
            return {}, "rate limited"
        if resp.status_code >= 400:
            return {}, resp.text[:300] or resp.reason_phrase
        try:
            return resp.json(), None
        except ValueError:
            return {}, "invalid JSON response"

    def _api_error(self, err: str, email: str | None) -> AccountSnapshot:
        text = (err or "").lower()
        if "rate limited" in text or "429" in text:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.RATE_LIMITED,
                "Antigravity Cloud Code API 被限流",
                account_hint=email,
                source=CLOUD_CODE_SOURCE,
            )
        if "401" in text or "403" in text or "authentication" in text or "unauthorized" in text:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.AUTH_ERROR,
                f"Antigravity 授權失敗（{err[:120]}）。請重新執行登入。",
                account_hint=email,
                source=CLOUD_CODE_SOURCE,
            )
        return error_snapshot(
            self.provider_id,
            self.display_name,
            SnapshotStatus.ERROR,
            err or "Unknown error",
            account_hint=email,
            source=CLOUD_CODE_SOURCE,
        )
