"""Fetch and adapt upstream capacity signals for the routing engine.

This module isolates the routing engine from upstream signal availability and data shape
changes. It projects the raw payload into a minimal set of necessary fields to reduce
exposure to external API structures. By caching results and gracefully handling all
upstream failures, it guarantees that the routing engine can always make a decision,
even if it must rely on degraded or empty signals.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

Signals = dict[str, dict[str, Any]]


@dataclass
class SignalResult:
    signals: Signals
    stale: bool
    degraded: bool
    fetched_at: datetime | None
    models: dict[str, bool] = field(default_factory=dict)


class SignalSource:
    def __init__(
        self,
        url: str = "http://127.0.0.1:50048/api/routing",
        *,
        timeout: float = 3.0,
        ttl_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.ttl_seconds = ttl_seconds
        self._client = client

        self._lock = asyncio.Lock()
        self._last_result: SignalResult | None = None
        self._last_success: SignalResult | None = None
        self._last_fetch_time: float = 0.0

    async def get(self, *, now: datetime | None = None) -> SignalResult:
        current_time = time.monotonic()

        # Fast path returns cached results instantly under heavy concurrent load.
        if self._last_result is not None and (current_time - self._last_fetch_time) < self.ttl_seconds:
            return self._last_result

        async with self._lock:
            # Recheck cache inside lock to prevent concurrent refresh thundering herd.
            current_time = time.monotonic()
            if self._last_result is not None and (current_time - self._last_fetch_time) < self.ttl_seconds:
                return self._last_result

            if self._client is None:
                self._client = httpx.AsyncClient(timeout=self.timeout)

            actual_now = now if now is not None else datetime.now(timezone.utc)
            start_time = time.monotonic()
            parsed_url = urlparse(self.url)

            try:
                response = await self._client.get(self.url)
                elapsed_ms = (time.monotonic() - start_time) * 1000
                logger.info("Signal fetch: host=%s status=%s elapsed=%.1fms", parsed_url.hostname, response.status_code, elapsed_ms)
                response.raise_for_status()
                data = response.json()

                if not isinstance(data, dict) or "pools" not in data or not isinstance(data["pools"], dict):
                    raise ValueError("Invalid response format: missing or invalid 'pools'")

                pools = data["pools"]
                signals: Signals = {}
                models: dict[str, bool] = {}

                for pool_id, pool_data in pools.items():
                    if not isinstance(pool_data, dict):
                        continue

                    try:
                        usable = bool(pool_data["usable"])
                        score = pool_data["score"]
                        if score is not None:
                            score = float(score)
                        level = str(pool_data["level"])
                        cooling = bool(pool_data["cooldown"]["cooling"])

                        binding_slot = pool_data.get("binding_slot")
                        seconds_until_reset = None
                        if binding_slot is not None:
                            seconds_until_reset = int(pool_data["windows"][binding_slot]["seconds_until_reset"])

                        stale = bool(pool_data["stale"])

                        signals[str(pool_id)] = {
                            "usable": usable,
                            "score": score,
                            "level": level,
                            "cooling": cooling,
                            "seconds_until_reset": seconds_until_reset,
                            "stale": stale,
                        }
                    except (KeyError, ValueError, TypeError):
                        # Skip pools with malformed structure to isolate blast radius of upstream schema drift.
                        continue

                raw_models = data.get("models")
                if isinstance(raw_models, dict):
                    for model_id, model_data in raw_models.items():
                        if (
                            isinstance(model_data, dict)
                            and isinstance(model_data.get("usable"), bool)
                        ):
                            models[str(model_id)] = model_data["usable"]

                # We consider the overall fetch fresh if we successfully parsed at least the container.
                result = SignalResult(
                    signals=signals,
                    stale=False,
                    degraded=False,
                    fetched_at=actual_now,
                    models=models,
                )
                self._last_success = result
                self._last_result = result
                self._last_fetch_time = current_time
                return result

            except Exception as e:
                elapsed_ms = (time.monotonic() - start_time) * 1000
                error_name = type(e).__name__
                logger.warning("Signal fetch failed: host=%s status=%s elapsed=%.1fms", parsed_url.hostname, error_name, elapsed_ms)

                # Fallback to last known good state minimizes routing disruption during brief upstream outages.
                if self._last_success is not None:
                    result = SignalResult(
                        signals=self._last_success.signals,
                        stale=True,
                        degraded=False,
                        fetched_at=self._last_success.fetched_at,
                        models=self._last_success.models,
                    )
                else:
                    result = SignalResult(
                        signals={},
                        stale=True,
                        degraded=True,
                        fetched_at=None,
                        models={},
                    )

                self._last_result = result
                self._last_fetch_time = current_time
                return result
