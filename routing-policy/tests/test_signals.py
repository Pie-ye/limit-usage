"""Tests for the routing engine signal adapter."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest

from app.signals import SignalSource


class MockHandler:
    def __init__(self) -> None:
        self.call_count = 0
        self.response = httpx.Response(200, json={"pools": {}})
        self.exception: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        if self.exception:
            raise self.exception
        return self.response


def create_valid_upstream_payload() -> dict:
    return {
        "models": {
            "model-claude": {"usable": True},
            "model-disabled": {"usable": False},
        },
        "pools": {
            "claude": {
                "provider": "claude",
                "display_name": "Claude Pool",
                "status": "ok",
                "usable": True,
                "stale": False,
                "data_age_seconds": 42,
                "windows": {
                    "5h": {"seconds_until_reset": 300},
                    "1w": {"seconds_until_reset": 10000}
                },
                "binding_slot": "1w",
                "score": 96.0,
                "level": "ok",
                "pace_penalty": False,
                "cooldown": {"cooling": False, "unavailable_until": None, "seconds_left": 0},
                "message": "All good"
            }
        }
    }


@pytest.mark.asyncio
async def test_normal_projection() -> None:
    """Test case a: Normal projection with all fields and seconds_until_reset correctly set."""
    handler = MockHandler()
    handler.response = httpx.Response(200, json=create_valid_upstream_payload())
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    source = SignalSource(client=client)
    now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
    
    with patch("app.signals.time.monotonic", return_value=100.0):
        result = await source.get(now=now)

    assert not result.stale
    assert not result.degraded
    assert result.fetched_at == now
    assert result.models == {
        "model-claude": True,
        "model-disabled": False,
    }
    assert "claude" in result.signals

    c = result.signals["claude"]
    assert c["usable"] is True
    assert c["score"] == 96.0
    assert c["level"] == "ok"
    assert c["cooling"] is False
    assert c["seconds_until_reset"] == 10000
    assert c["stale"] is False


@pytest.mark.asyncio
async def test_binding_slot_null() -> None:
    """Test case b: binding_slot is null."""
    payload = {
        "pools": {
            "grok": {
                "provider": "grok",
                "display_name": "Grok Pool",
                "usable": False,
                "stale": True,
                "windows": {
                    "1h": {"seconds_until_reset": 100}
                },
                "binding_slot": None,
                "score": None,
                "level": "exhausted",
                "cooldown": {"cooling": True}
            }
        }
    }
    handler = MockHandler()
    handler.response = httpx.Response(200, json=payload)
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    source = SignalSource(client=client)
    with patch("app.signals.time.monotonic", return_value=100.0):
        result = await source.get()

    g = result.signals["grok"]
    assert g["seconds_until_reset"] is None
    assert g["usable"] is False
    assert g["score"] is None
    assert g["cooling"] is True
    assert g["stale"] is True


@pytest.mark.asyncio
async def test_ttl_cache_and_expiration() -> None:
    """Test cases c, d: TTL cache reuses result, expiration refetches."""
    handler = MockHandler()
    handler.response = httpx.Response(200, json={"pools": {}})
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    source = SignalSource(ttl_seconds=30.0, client=client)

    with patch("app.signals.time.monotonic", return_value=100.0):
        await source.get()
    
    assert handler.call_count == 1
    
    # c. inside TTL, no refetch
    with patch("app.signals.time.monotonic", return_value=129.9):
        await source.get()
    
    assert handler.call_count == 1
    
    # d. outside TTL, refetch
    with patch("app.signals.time.monotonic", return_value=130.1):
        await source.get()
        
    assert handler.call_count == 2


@pytest.mark.asyncio
async def test_fallback_when_previously_successful() -> None:
    """Test case e: Upstream 500 but previous success -> stale=True, degraded=False."""
    handler = MockHandler()
    handler.response = httpx.Response(
        200,
        json={
            "models": {"model-pool1": {"usable": False}},
            "pools": {
                "pool1": {
                    "usable": True,
                    "score": 10,
                    "level": "ok",
                    "cooldown": {"cooling": False},
                    "binding_slot": None,
                    "stale": False,
                }
            },
        },
    )
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    source = SignalSource(ttl_seconds=30.0, client=client)

    # First call succeeds
    with patch("app.signals.time.monotonic", return_value=100.0):
        res1 = await source.get()
    
    assert not res1.stale
    assert not res1.degraded
    
    # Second call fails
    handler.response = httpx.Response(500, text="Internal Server Error")
    
    with patch("app.signals.time.monotonic", return_value=150.0):
        res2 = await source.get()
        
    assert handler.call_count == 2
    assert res2.stale is True
    assert res2.degraded is False
    assert res2.signals == res1.signals
    assert res2.models == {"model-pool1": False}


@pytest.mark.asyncio
async def test_downgrade_when_never_successful() -> None:
    """Test case f: Timeout and never successful -> degraded=True."""
    handler = MockHandler()
    handler.exception = httpx.TimeoutException("Timeout")
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    source = SignalSource(client=client)

    with patch("app.signals.time.monotonic", return_value=100.0):
        res = await source.get()
        
    assert handler.call_count == 1
    assert res.stale is True
    assert res.degraded is True
    assert res.signals == {}
    assert res.models == {}


@pytest.mark.asyncio
async def test_missing_pools_key() -> None:
    """Test case g: JSON missing pools -> fallback/downgrade."""
    handler = MockHandler()
    handler.response = httpx.Response(200, json={"other": "data"})
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    source = SignalSource(client=client)

    with patch("app.signals.time.monotonic", return_value=100.0):
        res = await source.get()
        
    assert handler.call_count == 1
    assert res.stale is True
    assert res.degraded is True
    assert res.signals == {}


@pytest.mark.asyncio
async def test_broken_pool_skipped() -> None:
    """Test case h: Single pool invalid format -> skip it, but others project correctly."""
    payload = {
        "pools": {
            "claude": {
                "usable": True,
                "stale": False,
                "binding_slot": None,
                "score": 96.0,
                "level": "ok",
                "cooldown": {"cooling": False}
            },
            "broken": {
                "usable": True,
                "score": "not_a_float",
                "level": "ok",
                "stale": False,
                "binding_slot": None,
                "cooldown": {"cooling": False}
            }
        }
    }
    handler = MockHandler()
    handler.response = httpx.Response(200, json=payload)
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    source = SignalSource(client=client)
    with patch("app.signals.time.monotonic", return_value=100.0):
        result = await source.get()

    assert "claude" in result.signals
    assert "broken" not in result.signals


@pytest.mark.asyncio
async def test_missing_models_defaults_to_empty_mapping() -> None:
    handler = MockHandler()
    handler.response = httpx.Response(200, json={"pools": {}})
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    source = SignalSource(client=client)
    with patch("app.signals.time.monotonic", return_value=100.0):
        result = await source.get()

    assert result.models == {}


@pytest.mark.asyncio
async def test_malformed_model_usability_entries_are_skipped() -> None:
    payload = {
        "pools": {},
        "models": {
            "model-good": {"usable": True},
            "model-not-bool": {"usable": 1},
            "model-missing": {"listed": True},
            "model-not-mapping": False,
        },
    }
    handler = MockHandler()
    handler.response = httpx.Response(200, json=payload)
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    source = SignalSource(client=client)
    with patch("app.signals.time.monotonic", return_value=100.0):
        result = await source.get()

    assert result.models == {"model-good": True}


@pytest.mark.asyncio
async def test_no_extra_fields() -> None:
    """Test case i: Expose exactly the 6 required keys, omit provider, message, etc."""
    handler = MockHandler()
    handler.response = httpx.Response(200, json=create_valid_upstream_payload())
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    source = SignalSource(client=client)
    with patch("app.signals.time.monotonic", return_value=100.0):
        result = await source.get()

    c = result.signals["claude"]
    # assert exactly 6 keys
    expected_keys = {"usable", "score", "level", "cooling", "seconds_until_reset", "stale"}
    assert set(c.keys()) == expected_keys
    
    # assert explicit omission of these keys
    assert "provider" not in c
    assert "display_name" not in c
    assert "windows" not in c
    assert "message" not in c
    assert "data_age_seconds" not in c
    assert "cooldown" not in c


@pytest.mark.asyncio
async def test_continuous_failure_maintains_degraded_status() -> None:
    """Regression test for finding 1: Continuous failure with no success always keeps degraded=True."""
    handler = MockHandler()
    handler.exception = httpx.TimeoutException("Timeout")
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    source = SignalSource(ttl_seconds=30.0, client=client)

    # First fetch fails
    with patch("app.signals.time.monotonic", return_value=100.0):
        res1 = await source.get()
        
    assert handler.call_count == 1
    assert res1.degraded is True
    assert res1.stale is True

    # Second fetch outside TTL fails again
    with patch("app.signals.time.monotonic", return_value=150.0):
        res2 = await source.get()
        
    assert handler.call_count == 2
    # Important: it should still be degraded
    assert res2.degraded is True
    assert res2.stale is True
