"""Vendor CLI model lists tracking and delisting detection.

Loads the exported model catalog written by deploy/cli-models-export.py on the
host to /app/data/cli-models.json. When models disappear from vendor CLI availability,
they are identified and excluded from routing recommendations.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FRESH_MAX_AGE_SECONDS = 48 * 3600
FUTURE_CLOCK_SKEW_SECONDS = 300


@dataclass(frozen=True)
class VendorList:
    ok: bool
    fresh: bool              # ok 且整份檔案 generated_at 在 48 小時內
    models: frozenset[str]   # ok 為 False 時為空
    error: str | None


@dataclass(frozen=True)
class CliModels:
    generated_at: datetime | None
    vendors: Mapping[str, VendorList]

    def listed(self, vendor: str, model: str) -> bool | None:
        vl = self.vendors.get(vendor)
        if vl is None or not vl.fresh:
            return None
        return model in vl.models

    def summary(self) -> dict[str, Any]:
        gen_iso: str | None = None
        if self.generated_at is not None:
            dt = self.generated_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            gen_iso = dt.isoformat().replace("+00:00", "Z")
        return {
            "generated_at": gen_iso,
            "vendors": {
                v: {
                    "ok": vl.ok,
                    "fresh": vl.fresh,
                    "count": len(vl.models),
                }
                for v, vl in self.vendors.items()
            },
        }

    def uncatalogued(self, catalogued: Iterable[str]) -> dict[str, list[str]]:
        cat_set = set(catalogued)
        out: dict[str, list[str]] = {}
        for vendor, vl in self.vendors.items():
            if not vl.fresh:
                continue
            unlisted = []
            for m in vl.models:
                if m in cat_set:
                    continue
                if vendor == "agy" and not m.startswith("gemini-"):
                    continue
                unlisted.append(m)
            if unlisted:
                out[vendor] = sorted(unlisted)
        return out


@dataclass(frozen=True)
class _RawVendor:
    ok: bool
    models: frozenset[str]
    error: str | None


@dataclass(frozen=True)
class _RawCliModels:
    generated_at: datetime
    vendors: dict[str, _RawVendor]


_CACHE: tuple[tuple[Path, int], _RawCliModels] | None = None


def _parse_iso(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def load_cli_models(path: str | Path, *, now: datetime) -> CliModels | None:
    global _CACHE

    resolved_path = Path(path).expanduser().resolve()
    try:
        st = resolved_path.stat()
    except OSError:
        return None

    mtime_ns = st.st_mtime_ns
    cache_key = (resolved_path, mtime_ns)

    raw = _CACHE[1] if _CACHE is not None and _CACHE[0] == cache_key else None
    if raw is None:
        try:
            text = resolved_path.read_text(encoding="utf-8")
            data = json.loads(text)
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        gen_raw = data.get("generated_at")
        if not isinstance(gen_raw, str):
            return None

        gen_dt = _parse_iso(gen_raw)
        if gen_dt is None:
            return None

        vendors_raw: dict[str, _RawVendor] = {}
        vendors_data = data.get("vendors")
        if isinstance(vendors_data, dict):
            for v_name, v_val in vendors_data.items():
                if not isinstance(v_name, str):
                    continue
                if not isinstance(v_val, dict):
                    vendors_raw[v_name] = _RawVendor(ok=False, models=frozenset(), error="malformed entry")
                    continue
                ok = v_val.get("ok")
                if not isinstance(ok, bool):
                    vendors_raw[v_name] = _RawVendor(ok=False, models=frozenset(), error="malformed entry")
                    continue
                if ok:
                    models_val = v_val.get("models")
                    if isinstance(models_val, list) and all(isinstance(m, str) for m in models_val):
                        err = v_val.get("error")
                        err_str = str(err) if err is not None else None
                        vendors_raw[v_name] = _RawVendor(ok=True, models=frozenset(models_val), error=err_str)
                    else:
                        vendors_raw[v_name] = _RawVendor(ok=False, models=frozenset(), error="malformed entry")
                else:
                    err = v_val.get("error")
                    err_str = str(err) if err is not None else None
                    vendors_raw[v_name] = _RawVendor(ok=False, models=frozenset(), error=err_str)

        raw = _RawCliModels(generated_at=gen_dt, vendors=vendors_raw)
        _CACHE = (cache_key, raw)

    now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    age = (now_utc - raw.generated_at).total_seconds()
    if -FUTURE_CLOCK_SKEW_SECONDS <= age < 0:
        age = 0
    file_fresh = 0 <= age <= FRESH_MAX_AGE_SECONDS

    vendors_out: dict[str, VendorList] = {}
    for v_name, v_raw in raw.vendors.items():
        vendors_out[v_name] = VendorList(
            ok=v_raw.ok,
            fresh=v_raw.ok and file_fresh,
            models=v_raw.models,
            error=v_raw.error,
        )

    return CliModels(
        generated_at=raw.generated_at,
        vendors=vendors_out,
    )
