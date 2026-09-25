"""Dispatch feedback for quota-aware routing (modelled on 9router's account fallback).

``/api/routing`` ranks pools by the remaining % the pollers see. That view lags
reality: a subagent that just hit a 429 knows the pool is dead *now*, minutes
before the next poll shows it. 9router solves this per account with
``rateLimitedUntil`` + ``backoffLevel`` + ``lastError`` and clears all three on
the next success. This module is the same idea per vendor pool:

* ``classify(status, text)`` maps an error to a cooldown, text rules first,
  then HTTP status, then a transient default (9router's ``ERROR_RULES``).
* ``FeedbackRegistry.report(...)`` records one dispatch outcome. Rate-limit
  kinds use exponential backoff (base × 2^level, capped); a server-issued
  ``retry_after_seconds`` wins over the computed cooldown but is capped too
  (9router: ``MAX_RATE_LIMIT_COOLDOWN_MS``), because a codex ``resets_at`` five
  hours out would otherwise lock the pool for the whole window.
* ``FeedbackRegistry.active(now)`` is what ``build_routing_payload`` folds into
  ``usable`` / ``next_available_at``.

A 404 failure with a specified model represents a single model missing/delisted,
cooldown for that model only (24 hours), separate from vendor pool cooldown.

State is in-memory (as in 9router); a restart clears it, which is the safe
direction — the poller's remaining % is still the primary signal.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.models import utcnow

# Exponential backoff for rate-limit kinds: 60 s, 120 s, 240 s … capped.
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_LEVEL = 8
# Hard cap on any cooldown, including a provider-reported retry-after.
MAX_COOLDOWN_SECONDS = 30 * 60
# Model missing / delisted cooldown (24 hours).
MODEL_MISSING_SECONDS = 24 * 3600
# Fixed cooldowns (seconds).
COOLDOWN_LONG = 5 * 60       # auth / billing / not-found: needs a human or a re-login
COOLDOWN_SHORT = 5           # request rejected as malformed: try the next model
COOLDOWN_TRANSIENT = 30      # anything unclassified (5xx, timeouts, crashes)

# Checked top-to-bottom: text rules first (order = priority), then status.
# ``kind`` is what callers see; ``backoff`` kinds escalate on repeat.
ERROR_RULES: list[dict[str, Any]] = [
    {"text": "no credentials", "kind": "auth", "cooldown": COOLDOWN_LONG},
    {"text": "not logged in", "kind": "auth", "cooldown": COOLDOWN_LONG},
    {"text": "please run /login", "kind": "auth", "cooldown": COOLDOWN_LONG},
    {"text": "invalid api key", "kind": "auth", "cooldown": COOLDOWN_LONG},
    {"text": "authentication", "kind": "auth", "cooldown": COOLDOWN_LONG},
    {"text": "request not allowed", "kind": "rejected", "cooldown": COOLDOWN_SHORT},
    {"text": "improperly formed request", "kind": "rejected", "cooldown": COOLDOWN_SHORT},
    {"text": "rate limit", "kind": "rate_limit", "backoff": True},
    {"text": "rate_limit", "kind": "rate_limit", "backoff": True},
    {"text": "too many requests", "kind": "rate_limit", "backoff": True},
    {"text": "quota exceeded", "kind": "rate_limit", "backoff": True},
    {"text": "usage limit", "kind": "rate_limit", "backoff": True},
    {"text": "resource_exhausted", "kind": "rate_limit", "backoff": True},
    {"text": "capacity", "kind": "rate_limit", "backoff": True},
    {"text": "overloaded", "kind": "rate_limit", "backoff": True},
    {"status": 401, "kind": "auth", "cooldown": COOLDOWN_LONG},
    {"status": 402, "kind": "auth", "cooldown": COOLDOWN_LONG},
    {"status": 403, "kind": "auth", "cooldown": COOLDOWN_LONG},
    {"status": 404, "kind": "rejected", "cooldown": COOLDOWN_LONG},
    {"status": 429, "kind": "rate_limit", "backoff": True},
]


def backoff_seconds(level: int) -> int:
    """Cooldown for backoff ``level`` (1-based): base × 2^(level-1), capped."""
    lvl = max(0, min(level, BACKOFF_MAX_LEVEL) - 1)
    return min(BACKOFF_BASE_SECONDS * (2 ** lvl), MAX_COOLDOWN_SECONDS)


def classify(status: int | None, text: str | None, *, backoff_level: int = 0) -> dict[str, Any]:
    """Map a failed dispatch to ``{kind, cooldown_seconds, backoff_level}``.

    ``backoff_level`` is the pool's current level; rate-limit kinds return the
    incremented level and the cooldown that goes with it.
    """
    lower = (text or "").lower()
    for rule in ERROR_RULES:
        hit = False
        if "text" in rule:
            hit = bool(lower) and rule["text"] in lower
        elif "status" in rule:
            hit = status is not None and rule["status"] == status
        if not hit:
            continue
        if rule.get("backoff"):
            level = min(backoff_level + 1, BACKOFF_MAX_LEVEL)
            return {"kind": rule["kind"], "cooldown_seconds": backoff_seconds(level), "backoff_level": level}
        return {"kind": rule["kind"], "cooldown_seconds": rule["cooldown"], "backoff_level": backoff_level}
    return {"kind": "transient", "cooldown_seconds": COOLDOWN_TRANSIENT, "backoff_level": backoff_level}


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return _as_utc(dt).isoformat().replace("+00:00", "Z")


@dataclass
class PoolState:
    unavailable_until: datetime | None = None
    backoff_level: int = 0
    kind: str | None = None
    last_error: str | None = None
    last_model: str | None = None
    last_failure_at: datetime | None = None
    last_ok_at: datetime | None = None
    failures: int = 0
    successes: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    def cooling(self, now: datetime) -> bool:
        return self.unavailable_until is not None and _as_utc(self.unavailable_until) > _as_utc(now)

    def view(self, now: datetime) -> dict[str, Any]:
        cooling = self.cooling(now)
        left = int((_as_utc(self.unavailable_until) - _as_utc(now)).total_seconds()) if cooling else 0
        return {
            "cooling": cooling,
            "unavailable_until": _iso(self.unavailable_until) if cooling else None,
            "seconds_left": left,
            "backoff_level": self.backoff_level,
            "kind": self.kind if cooling else None,
            "last_error": self.last_error,
            "last_model": self.last_model,
            "last_failure_at": _iso(self.last_failure_at),
            "last_ok_at": _iso(self.last_ok_at),
            "failures": self.failures,
            "successes": self.successes,
        }


@dataclass
class ModelState:
    missing_until: datetime | None = None
    last_error: str | None = None
    reported_at: datetime | None = None

    def missing(self, now: datetime) -> bool:
        return self.missing_until is not None and _as_utc(self.missing_until) > _as_utc(now)

    def view(self, now: datetime) -> dict[str, Any]:
        missing = self.missing(now)
        left = int((_as_utc(self.missing_until) - _as_utc(now)).total_seconds()) if missing else 0
        return {
            "missing": missing,
            "until": _iso(self.missing_until) if missing else None,
            "seconds_left": left,
            "last_error": self.last_error,
            "reported_at": _iso(self.reported_at),
        }


HISTORY_LIMIT = 20


class FeedbackRegistry:
    """Per-pool cooldown state fed by dispatch outcomes. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pools: dict[str, PoolState] = {}
        self._models: dict[str, ModelState] = {}

    def report(
        self,
        pool: str,
        *,
        ok: bool,
        status: int | None = None,
        error: str | None = None,
        retry_after_seconds: float | None = None,
        model: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = _as_utc(now or utcnow())
        with self._lock:
            state = self._pools.setdefault(pool, PoolState())
            if ok:
                if model:
                    self._models.pop(model, None)
                # 9router resetAccountState: one success clears cooldown + backoff.
                state.unavailable_until = None
                state.backoff_level = 0
                state.kind = None
                state.last_ok_at = current
                state.last_model = model or state.last_model
                state.successes += 1
                decision = {"kind": "ok", "cooldown_seconds": 0, "backoff_level": 0}
            elif status == 404 and model:
                m_state = self._models.setdefault(model, ModelState())
                m_state.missing_until = current + timedelta(seconds=MODEL_MISSING_SECONDS)
                m_state.last_error = (error or "")[:500] or None
                m_state.reported_at = current

                state.last_error = (error or "")[:500] or None
                state.last_model = model or state.last_model
                state.last_failure_at = current
                state.failures += 1

                decision = {
                    "kind": "model_missing",
                    "cooldown_seconds": MODEL_MISSING_SECONDS,
                    "backoff_level": state.backoff_level,
                }
            else:
                decision = classify(status, error, backoff_level=state.backoff_level)
                cooldown = decision["cooldown_seconds"]
                if retry_after_seconds is not None and retry_after_seconds > 0:
                    cooldown = max(cooldown, int(retry_after_seconds))
                cooldown = min(cooldown, MAX_COOLDOWN_SECONDS)
                decision["cooldown_seconds"] = cooldown
                until = current + timedelta(seconds=cooldown)
                # Never shorten an existing cooldown with a milder later report.
                if state.unavailable_until is None or _as_utc(state.unavailable_until) < until:
                    state.unavailable_until = until
                state.backoff_level = decision["backoff_level"]
                state.kind = decision["kind"]
                state.last_error = (error or "")[:500] or None
                state.last_model = model or state.last_model
                state.last_failure_at = current
                state.failures += 1
            state.history.append(
                {
                    "at": _iso(current),
                    "ok": ok,
                    "model": model,
                    "status": status,
                    "kind": decision["kind"],
                    "cooldown_seconds": decision["cooldown_seconds"],
                }
            )
            del state.history[:-HISTORY_LIMIT]
            out = {"pool": pool, **state.view(current), **decision}
            if status == 404 and not ok and model:
                out["model"] = model
            return out

    def active(self, now: datetime | None = None) -> dict[str, dict[str, Any]]:
        """Pools currently cooling → their view. Expired entries are dropped."""
        current = _as_utc(now or utcnow())
        with self._lock:
            return {p: s.view(current) for p, s in self._pools.items() if s.cooling(current)}

    def missing_models(self, now: datetime | None = None) -> dict[str, dict[str, Any]]:
        """Currently missing models → their view. Expired entries are pruned."""
        current = _as_utc(now or utcnow())
        with self._lock:
            expired = [m for m, s in self._models.items() if not s.missing(current)]
            for m in expired:
                del self._models[m]
            return {m: s.view(current) for m, s in self._models.items()}

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        current = _as_utc(now or utcnow())
        with self._lock:
            expired = [m for m, s in self._models.items() if not s.missing(current)]
            for m in expired:
                del self._models[m]
            return {
                "server_time": _iso(current),
                "pools": {p: {**s.view(current), "history": list(s.history)} for p, s in self._pools.items()},
                "models": {m: s.view(current) for m, s in self._models.items()},
            }

    def clear(self, pool: str | None = None) -> None:
        with self._lock:
            if pool is None:
                self._pools.clear()
                self._models.clear()
            else:
                self._pools.pop(pool, None)
