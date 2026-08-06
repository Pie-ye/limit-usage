from __future__ import annotations

import errno
import json
import logging
import os
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.models import AccountSnapshot, ProviderId, SnapshotStatus, UsageWindow, utcnow
from app.providers.base import error_snapshot, http_get_json
from app.providers.codex import _parse_datetime

logger = logging.getLogger(__name__)

# Primary path used by CodexBar / Grok Build billing UI
GROK_CREDITS_GRPC_URL = (
    "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"
)

# Secondary best-effort JSON endpoints (often 404 / Cloudflare)
GROK_USAGE_CANDIDATES = [
    "https://grok.com/rest/rate-limits",
    "https://grok.x.ai/api/usage",
    "https://api.x.ai/v1/usage",
]

# Product id → label (best-effort; ids observed in protobuf breakdown)
PRODUCT_LABELS = {
    1: "Chat / Build",
    2: "Imagine / Media",
    3: "Voice",
    4: "API",
}


def _walk_auth_entries(auth: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect credential dicts from ~/.grok/auth.json shapes."""
    entries: list[dict[str, Any]] = []
    if "key" in auth or "access_token" in auth or "accessToken" in auth:
        entries.append(auth)
    for value in auth.values():
        if isinstance(value, dict) and (
            "key" in value or "access_token" in value or "refresh_token" in value
        ):
            entries.append(value)
        elif isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, dict) and (
                    "key" in nested
                    or "access_token" in nested
                    or "refresh_token" in nested
                ):
                    entries.append(nested)
    return entries


def load_grok_auth(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Grok auth file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_grok_auth(path: Path, auth: dict[str, Any]) -> None:
    """Persist auth, retaining atomic writes except for bind-mounted files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(auth, indent=2, ensure_ascii=False) + "\n"
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        tmp.replace(path)
    except OSError as exc:
        # Docker bind-mounted files cannot be replaced with rename(2), even
        # when the mount is writable. Update that inode in place so refreshed
        # OIDC credentials survive the next poll and container restart.
        if exc.errno not in {errno.EBUSY, errno.EXDEV, errno.EPERM}:
            tmp.unlink(missing_ok=True)
            raise
        try:
            with path.open("w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            tmp.unlink(missing_ok=True)
        logger.info("Updated bind-mounted Grok auth file in place: %s", path)


def _parse_expires_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    # Nanoseconds tail: 2026-07-13T12:37:33.710730091Z
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Truncate fractional seconds > 6 digits for fromisoformat
    if "." in text:
        head, rest = text.split(".", 1)
        frac = ""
        tz = ""
        for i, ch in enumerate(rest):
            if ch.isdigit():
                frac += ch
            else:
                tz = rest[i:]
                break
        frac = (frac + "000000")[:6]
        text = f"{head}.{frac}{tz}"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def select_grok_auth_entry(
    auth: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """
    Pick best auth entry. Returns (top_level_key_or_None, entry_dict).
    top_level_key is the auth.json object key (scope URL) when nested.
    """
    candidates: list[tuple[str | None, dict[str, Any]]] = []
    if "key" in auth or "access_token" in auth or "refresh_token" in auth:
        candidates.append((None, auth))
    for k, value in auth.items():
        if isinstance(value, dict) and (
            "key" in value or "access_token" in value or "refresh_token" in value
        ):
            candidates.append((str(k), value))
        elif isinstance(value, dict):
            for nk, nested in value.items():
                if isinstance(nested, dict) and (
                    "key" in nested
                    or "access_token" in nested
                    or "refresh_token" in nested
                ):
                    candidates.append((str(k), nested))

    preferred: list[tuple[str | None, dict[str, Any]]] = []
    others: list[tuple[str | None, dict[str, Any]]] = []
    for item in candidates:
        mode = str(item[1].get("auth_mode") or item[1].get("authMode") or "").lower()
        if "legacy" in mode:
            others.append(item)
        else:
            preferred.append(item)

    for scope_key, entry in preferred + others:
        token = (
            entry.get("key")
            or entry.get("access_token")
            or entry.get("accessToken")
            or entry.get("token")
        )
        if token or entry.get("refresh_token"):
            return scope_key, entry
    return None, None


def extract_grok_credentials(auth: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (bearer, email/hint)."""
    _scope, entry = select_grok_auth_entry(auth)
    if not entry:
        return None, None
    token = (
        entry.get("key")
        or entry.get("access_token")
        or entry.get("accessToken")
        or entry.get("token")
    )
    email = entry.get("email") or entry.get("user_id") or entry.get("userId")
    return (str(token) if token else None, str(email) if email else None)


def access_token_needs_refresh(entry: dict[str, Any], skew: timedelta = timedelta(minutes=5)) -> bool:
    """True if access token is missing/expired/near expiry."""
    token = entry.get("key") or entry.get("access_token") or entry.get("accessToken")
    if not token:
        return True
    exp = _parse_expires_at(entry.get("expires_at") or entry.get("expiresAt"))
    if exp is None:
        return False  # unknown expiry; try as-is first
    return utcnow() >= (exp - skew)


async def refresh_grok_access_token(
    client: httpx.AsyncClient,
    entry: dict[str, Any],
    *,
    auth_path: Path | None = None,
    auth_root: dict[str, Any] | None = None,
    scope_key: str | None = None,
) -> tuple[str | None, str | None]:
    """
    OIDC refresh_token grant against auth.x.ai.
    Updates entry in-place and optionally persists auth.json.
    Returns (new_access_token, error_message).
    """
    refresh = entry.get("refresh_token") or entry.get("refreshToken")
    if not refresh:
        return None, "No refresh_token in grok auth.json"

    client_id = (
        entry.get("oidc_client_id")
        or entry.get("oidcClientId")
        or entry.get("client_id")
        or "b1a00492-073a-47ea-816f-4c329264a828"
    )
    issuer = str(entry.get("oidc_issuer") or entry.get("oidcIssuer") or "https://auth.x.ai").rstrip(
        "/"
    )
    token_url = f"{issuer}/oauth2/token"

    try:
        resp = await client.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": str(refresh),
                "client_id": str(client_id),
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
    except httpx.HTTPError as exc:
        return None, f"Token refresh request failed: {exc}"

    if resp.status_code >= 400:
        detail = resp.text[:240]
        return None, f"Token refresh failed HTTP {resp.status_code}: {detail}"

    try:
        body = resp.json()
    except ValueError:
        return None, "Token refresh returned non-JSON"

    access = body.get("access_token")
    if not access:
        return None, "Token refresh response missing access_token"

    # Persist into the same shape Grok CLI uses (`key` + expires_at)
    entry["key"] = str(access)
    if body.get("refresh_token"):
        entry["refresh_token"] = str(body["refresh_token"])
    expires_in = body.get("expires_in")
    try:
        sec = int(expires_in) if expires_in is not None else 21600
    except (TypeError, ValueError):
        sec = 21600
    entry["expires_at"] = (utcnow() + timedelta(seconds=sec)).isoformat().replace(
        "+00:00", "Z"
    )

    if auth_path is not None and auth_root is not None:
        if scope_key is not None and scope_key in auth_root:
            auth_root[scope_key] = entry
        try:
            save_grok_auth(auth_path, auth_root)
            logger.info("Refreshed SuperGrok token and wrote %s", auth_path)
        except OSError as exc:
            logger.warning("Refreshed token but failed to write auth.json: %s", exc)

    return str(access), None


def _label_for_reset(resets_at: datetime | None, period_start: datetime | None = None) -> str:
    if period_start and resets_at:
        span = resets_at - period_start
        if timedelta(days=5) <= span <= timedelta(days=9):
            return "Weekly"
        if timedelta(days=25) <= span <= timedelta(days=35):
            return "Monthly"
    if not resets_at:
        return "Usage pool"
    now = utcnow()
    delta = resets_at - now
    if timedelta(days=5) <= delta <= timedelta(days=9):
        return "Weekly"
    if timedelta(days=25) <= delta <= timedelta(days=35):
        return "Monthly"
    return "Usage pool"


# --- Minimal protobuf helpers (enough for GetGrokCreditsConfig) ---


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 63:
            break
    raise ValueError("invalid varint")


def _parse_protobuf_fields(buf: bytes) -> list[tuple[int, str, Any]]:
    i = 0
    fields: list[tuple[int, str, Any]] = []
    while i < len(buf):
        key, i = _read_varint(buf, i)
        field_no, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _read_varint(buf, i)
            fields.append((field_no, "varint", val))
        elif wire == 1:
            val = buf[i : i + 8]
            i += 8
            fields.append((field_no, "fixed64", val))
        elif wire == 2:
            length, i = _read_varint(buf, i)
            val = buf[i : i + length]
            i += length
            fields.append((field_no, "len", val))
        elif wire == 5:
            val = buf[i : i + 4]
            i += 4
            fields.append((field_no, "fixed32", val))
        else:
            break
    return fields


def _decode_timestamp(msg: bytes) -> datetime | None:
    """Decode google.protobuf.Timestamp { seconds, nanos }."""
    try:
        fields = _parse_protobuf_fields(msg)
    except ValueError:
        return None
    secs = nanos = None
    for fn, kind, val in fields:
        if fn == 1 and kind == "varint":
            secs = int(val)
        if fn == 2 and kind == "varint":
            nanos = int(val)
    if secs is None:
        return None
    return datetime.fromtimestamp(secs + (nanos or 0) / 1e9, tz=timezone.utc)


def _unwrap_grpc_web(data: bytes) -> bytes | None:
    """Extract first data frame from grpc-web response."""
    if not data:
        return None
    # Some servers return raw protobuf without framing
    if len(data) >= 5 and data[0] in (0, 1):
        length = int.from_bytes(data[1:5], "big")
        if 5 + length <= len(data):
            return data[5 : 5 + length]
    return data


def parse_grok_credits_protobuf(data: bytes) -> list[UsageWindow]:
    """
    Parse GetGrokCreditsConfig response.

    Observed shape (outer field 1 = config message):
      1: float credit_usage_percent (used %)
      4: Timestamp period_start
      5: Timestamp period_end (reset)
      7: repeated { 1: product_id, 2: float used_percent }
    """
    payload = _unwrap_grpc_web(data)
    if not payload:
        return []

    try:
        outer = _parse_protobuf_fields(payload)
    except ValueError:
        return []

    # Prefer nested field 1; else treat whole payload as config
    config_bytes: bytes | None = None
    for fn, kind, val in outer:
        if fn == 1 and kind == "len" and val:
            config_bytes = val
            break
    if config_bytes is None:
        config_bytes = payload

    try:
        fields = _parse_protobuf_fields(config_bytes)
    except ValueError:
        return []

    used_pct: float | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    products: list[tuple[int, float]] = []

    for fn, kind, val in fields:
        if fn == 1 and kind == "fixed32" and isinstance(val, (bytes, bytearray)):
            used_pct = float(struct.unpack("<f", val)[0])
        elif fn == 4 and kind == "len":
            period_start = _decode_timestamp(val)
        elif fn == 5 and kind == "len":
            period_end = _decode_timestamp(val)
        elif fn == 7 and kind == "len":
            try:
                nested = _parse_protobuf_fields(val)
            except ValueError:
                continue
            pid = None
            pct = None
            for nfn, nkind, nval in nested:
                if nfn == 1 and nkind == "varint":
                    pid = int(nval)
                if nfn == 2 and nkind == "fixed32" and isinstance(nval, (bytes, bytearray)):
                    pct = float(struct.unpack("<f", nval)[0])
            if pid is not None and pct is not None:
                products.append((pid, pct))

    if used_pct is None:
        if products:
            used_pct = sum(p for _, p in products)
        elif period_start is not None or period_end is not None:
            # Proto3 omits scalar fields whose value is the default zero. Grok
            # therefore returns a valid billing period with no field 1 (and no
            # product rows) immediately after usage resets. Treat that shape as
            # 0% used instead of reporting an unparseable response.
            used_pct = 0.0
        else:
            return []

    used_pct = max(0.0, min(100.0, float(used_pct)))
    remaining = max(0.0, min(100.0, 100.0 - used_pct))
    label = _label_for_reset(period_end, period_start)

    windows: list[UsageWindow] = [
        UsageWindow(
            key="weekly",
            label=label,
            used_percent=used_pct,
            remaining_percent=remaining,
            resets_at=period_end,
            limit_window_seconds=(
                int((period_end - period_start).total_seconds())
                if period_start and period_end
                else 604800
            ),
            raw_extra={
                "source_shape": "GetGrokCreditsConfig",
                "period_start": period_start.isoformat() if period_start else None,
                "products": [
                    {
                        "id": pid,
                        "label": PRODUCT_LABELS.get(pid, f"product-{pid}"),
                        "used_percent": pct,
                    }
                    for pid, pct in products
                ],
            },
        )
    ]

    # Optional breakdown rows (do not replace main bar)
    for pid, pct in products:
        rem = max(0.0, min(100.0, 100.0 - pct))
        windows.append(
            UsageWindow(
                key=f"product-{pid}",
                label=PRODUCT_LABELS.get(pid, f"Product {pid}"),
                used_percent=pct,
                remaining_percent=rem,
                resets_at=period_end,
                raw_extra={"product_id": pid},
            )
        )

    return windows


def parse_supergrok_payload(data: dict[str, Any]) -> list[UsageWindow]:
    """Best-effort parse of various JSON usage shapes (fallback)."""
    windows: list[UsageWindow] = []

    usage = data.get("usage") or {}
    billing = data.get("billingCycle") or data.get("billing_cycle") or {}
    monthly_limit = data.get("monthlyLimit") or data.get("monthly_limit") or {}
    if isinstance(usage, dict) and monthly_limit:
        total_used = usage.get("totalUsed") or usage.get("total_used") or {}
        used_val = total_used.get("val") if isinstance(total_used, dict) else total_used
        limit_val = (
            monthly_limit.get("val") if isinstance(monthly_limit, dict) else monthly_limit
        )
        try:
            used_f = float(used_val or 0)
            limit_f = float(limit_val or 0)
            used_pct = (used_f / limit_f * 100.0) if limit_f > 0 else 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            used_pct = 0.0
        reset = _parse_datetime(
            billing.get("billingPeriodEnd") or billing.get("billing_period_end")
        )
        remaining = max(0.0, min(100.0, 100.0 - used_pct))
        windows.append(
            UsageWindow(
                key="weekly",
                label=_label_for_reset(reset),
                used_percent=used_pct,
                remaining_percent=remaining,
                resets_at=reset,
                raw_extra={"source_shape": "billing_rpc_json"},
            )
        )
        return windows

    for used_key, remain_key, reset_key in (
        ("used_percent", "remaining_percent", "resets_at"),
        ("usagePercent", "remainingPercent", "resetAt"),
        ("credit_usage_percent", None, "reset_at"),
        ("percent_used", "percent_left", "reset_time"),
    ):
        if used_key in data or (remain_key and remain_key in data):
            used = data.get(used_key)
            remaining = data.get(remain_key) if remain_key else None
            try:
                used_f = float(used) if used is not None else None
            except (TypeError, ValueError):
                used_f = None
            try:
                rem_f = float(remaining) if remaining is not None else None
            except (TypeError, ValueError):
                rem_f = None
            if used_f is None and rem_f is not None:
                used_f = 100.0 - rem_f
            if rem_f is None and used_f is not None:
                rem_f = 100.0 - used_f
            reset = _parse_datetime(data.get(reset_key) or data.get("resets_at"))
            windows.append(
                UsageWindow(
                    key="weekly",
                    label=_label_for_reset(reset),
                    used_percent=used_f,
                    remaining_percent=rem_f,
                    resets_at=reset,
                    raw_extra={"source_shape": "flat_percent"},
                )
            )
            return windows

    return windows


class SuperGrokProvider:
    provider_id = ProviderId.SUPERGROK
    display_name = "SuperGrok"

    def __init__(
        self,
        auth_path: Path,
        cookie: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.auth_path = auth_path
        self.cookie = cookie
        self.timeout = timeout

    async def _fetch_grpc_credits(
        self, client: httpx.AsyncClient, bearer: str
    ) -> tuple[list[UsageWindow] | None, str | None]:
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/grpc-web+proto",
            "Accept": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "User-Agent": "limit-usage/0.1",
            "Origin": "https://grok.com",
            "Referer": "https://grok.com/",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie

        # Empty protobuf message framed as grpc-web
        body = b"\x00\x00\x00\x00\x00"
        try:
            resp = await client.post(GROK_CREDITS_GRPC_URL, content=body, headers=headers)
        except httpx.HTTPError as exc:
            return None, f"gRPC billing request failed: {exc}"

        if resp.status_code in (401, 403):
            return None, f"Auth rejected by billing gRPC ({resp.status_code})"
        if resp.status_code >= 400:
            return None, f"billing gRPC HTTP {resp.status_code}: {resp.text[:200]}"

        windows = parse_grok_credits_protobuf(resp.content)
        if not windows:
            return None, "billing gRPC returned no parseable usage fields"
        return windows, None

    async def fetch(self) -> AccountSnapshot:
        bearer: str | None = None
        hint: str | None = None
        auth_error: str | None = None
        auth_root: dict[str, Any] | None = None
        scope_key: str | None = None
        entry: dict[str, Any] | None = None

        try:
            # Always re-read file (Grok CLI may have refreshed independently)
            auth_root = load_grok_auth(self.auth_path)
            scope_key, entry = select_grok_auth_entry(auth_root)
            if entry:
                bearer = (
                    entry.get("key")
                    or entry.get("access_token")
                    or entry.get("accessToken")
                    or entry.get("token")
                )
                if bearer:
                    bearer = str(bearer)
                hint_val = entry.get("email") or entry.get("user_id") or entry.get("userId")
                hint = str(hint_val) if hint_val else None
            if not bearer and not (entry and entry.get("refresh_token")):
                auth_error = "No bearer/refresh token in grok auth.json"
        except FileNotFoundError as exc:
            auth_error = str(exc)
        except (OSError, json.JSONDecodeError) as exc:
            auth_error = f"Failed to read grok auth: {exc}"

        if not bearer and not self.cookie and not (entry and entry.get("refresh_token")):
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.AUTH_ERROR,
                (auth_error or "No SuperGrok credentials")
                + ". Provide ~/.grok/auth.json (run `grok login`) or SUPERGROK_COOKIE.",
                account_hint=hint,
                source="auth",
            )

        last_err = auth_error
        grpc_err: str | None = None
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            # Proactive refresh when access token is near/past expiry (~6h lifetime)
            if entry and access_token_needs_refresh(entry):
                new_tok, rerr = await refresh_grok_access_token(
                    client,
                    entry,
                    auth_path=self.auth_path,
                    auth_root=auth_root,
                    scope_key=scope_key,
                )
                if new_tok:
                    bearer = new_tok
                else:
                    last_err = rerr or last_err
                    logger.warning("Proactive SuperGrok refresh failed: %s", rerr)

            # 1) Primary: Grok Build billing gRPC-web
            if bearer:
                windows, err = await self._fetch_grpc_credits(client, bearer)
                if windows:
                    return AccountSnapshot(
                        provider=self.provider_id,
                        display_name=self.display_name,
                        account_hint=hint,
                        status=SnapshotStatus.OK,
                        windows=windows,
                        fetched_at=utcnow(),
                        source="GetGrokCreditsConfig",
                    )
                grpc_err = err
                last_err = err or last_err

                # Reactive refresh on auth rejection, then retry once
                auth_fail = err and (
                    "401" in err or "403" in err or "Auth rejected" in err
                )
                if auth_fail and entry and entry.get("refresh_token"):
                    new_tok, rerr = await refresh_grok_access_token(
                        client,
                        entry,
                        auth_path=self.auth_path,
                        auth_root=auth_root,
                        scope_key=scope_key,
                    )
                    if new_tok:
                        bearer = new_tok
                        windows, err2 = await self._fetch_grpc_credits(client, bearer)
                        if windows:
                            return AccountSnapshot(
                                provider=self.provider_id,
                                display_name=self.display_name,
                                account_hint=hint,
                                status=SnapshotStatus.OK,
                                windows=windows,
                                fetched_at=utcnow(),
                                source="GetGrokCreditsConfig+refresh",
                            )
                        grpc_err = err2 or grpc_err
                        last_err = err2 or rerr or last_err
                    else:
                        last_err = rerr or last_err

            # 2) Fallback JSON probes (usually 404; kept as last resort)
            headers_base = {
                "Accept": "application/json",
                "User-Agent": "limit-usage/0.1",
            }
            if bearer:
                for url in GROK_USAGE_CANDIDATES:
                    headers = {**headers_base, "Authorization": f"Bearer {bearer}"}
                    if self.cookie:
                        headers["Cookie"] = self.cookie
                    status, data, err = await http_get_json(client, url, headers=headers)
                    if isinstance(data, dict):
                        windows = parse_supergrok_payload(data)
                        if windows:
                            return AccountSnapshot(
                                provider=self.provider_id,
                                display_name=self.display_name,
                                account_hint=hint,
                                status=SnapshotStatus.OK,
                                windows=windows,
                                fetched_at=utcnow(),
                                source=url,
                            )
                    # Prefer keeping gRPC error over noisy 404 from public API
                    if status != 404:
                        last_err = err or f"{url}: HTTP {status}"

        if bearer or (entry and entry.get("refresh_token")):
            # Clearer message: CLI login ≠ long-lived access token for this app
            hint_msg = (
                f"Billing fetch failed. Primary error: {grpc_err or last_err}. "
                "Access tokens expire ~6h; this service now auto-refreshes via "
                "refresh_token. If this persists, run `grok login` once to renew refresh_token."
            )
            status = (
                SnapshotStatus.AUTH_ERROR
                if last_err and ("refresh failed" in last_err.lower() or "401" in last_err or "403" in last_err)
                else SnapshotStatus.UNSUPPORTED
            )
            return AccountSnapshot(
                provider=self.provider_id,
                display_name=self.display_name,
                account_hint=hint,
                status=status,
                message=hint_msg,
                windows=[],
                fetched_at=utcnow(),
                source="auth-only",
            )

        return error_snapshot(
            self.provider_id,
            self.display_name,
            SnapshotStatus.AUTH_ERROR,
            last_err or "Unable to fetch SuperGrok usage",
            account_hint=hint,
            source="supergrok",
        )
