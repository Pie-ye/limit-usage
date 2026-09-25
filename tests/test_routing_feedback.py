from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.models import ProviderId
from app.services.routing_feedback import (
    BACKOFF_BASE_SECONDS,
    COOLDOWN_LONG,
    COOLDOWN_SHORT,
    COOLDOWN_TRANSIENT,
    MAX_COOLDOWN_SECONDS,
    FeedbackRegistry,
    backoff_seconds,
    classify,
)
from app.services.routing_view import build_routing_payload
from catalog.tiering import Catalog
from tests.test_routing_view import NOW, _all_ok, _snap, _win, make_fake_catalog


@pytest.fixture
def fake_catalog(tmp_path: Path) -> Catalog:
    return make_fake_catalog(tmp_path)


# --- classify -----------------------------------------------------------------

def test_classify_text_rules_win_over_status():
    # 403 with a rate-limit message is a rate limit, not an auth failure
    out = classify(403, "Rate limit reached for model-terra")
    assert out["kind"] == "rate_limit"
    assert out["backoff_level"] == 1
    assert out["cooldown_seconds"] == BACKOFF_BASE_SECONDS


def test_classify_status_rules_and_default():
    assert classify(429, "")["kind"] == "rate_limit"
    assert classify(401, "")["kind"] == "auth"
    assert classify(401, "")["cooldown_seconds"] == COOLDOWN_LONG
    assert classify(None, "request not allowed")["cooldown_seconds"] == COOLDOWN_SHORT
    assert classify(None, "not logged in, please run /login")["kind"] == "auth"
    assert classify(500, "segfault")["kind"] == "transient"
    assert classify(500, "segfault")["cooldown_seconds"] == COOLDOWN_TRANSIENT
    assert classify(None, None)["kind"] == "transient"


def test_backoff_doubles_and_caps():
    assert [backoff_seconds(n) for n in (1, 2, 3)] == [60, 120, 240]
    assert backoff_seconds(50) == MAX_COOLDOWN_SECONDS
    assert classify(429, "", backoff_level=2)["backoff_level"] == 3
    assert classify(429, "", backoff_level=2)["cooldown_seconds"] == 240


# --- registry -----------------------------------------------------------------

def test_registry_rate_limit_escalates_and_success_clears():
    reg = FeedbackRegistry()
    t0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    r1 = reg.report("codex", ok=False, status=429, model="model-terra", now=t0)
    assert r1["kind"] == "rate_limit"
    assert r1["cooldown_seconds"] == 60
    assert r1["cooling"] is True
    assert r1["seconds_left"] == 60
    # still cooling 30 s later
    assert reg.active(t0 + timedelta(seconds=30))["codex"]["seconds_left"] == 30
    # expired after 60 s
    assert reg.active(t0 + timedelta(seconds=61)) == {}
    # second 429 escalates to level 2 (120 s)
    r2 = reg.report("codex", ok=False, status=429, now=t0 + timedelta(seconds=70))
    assert r2["backoff_level"] == 2
    assert r2["cooldown_seconds"] == 120
    # success clears everything
    r3 = reg.report("codex", ok=True, model="model-terra", now=t0 + timedelta(seconds=80))
    assert r3["kind"] == "ok"
    assert r3["cooling"] is False
    assert r3["backoff_level"] == 0
    assert reg.active(t0 + timedelta(seconds=81)) == {}
    snap = reg.snapshot(now=t0 + timedelta(seconds=90))
    assert snap["pools"]["codex"]["failures"] == 2
    assert snap["pools"]["codex"]["successes"] == 1
    assert len(snap["pools"]["codex"]["history"]) == 3


def test_registry_retry_after_wins_but_is_capped():
    reg = FeedbackRegistry()
    t0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    r = reg.report("claude", ok=False, status=429, retry_after_seconds=600, now=t0)
    assert r["cooldown_seconds"] == 600
    r = reg.report("grok", ok=False, status=429, retry_after_seconds=5 * 3600, now=t0)
    assert r["cooldown_seconds"] == MAX_COOLDOWN_SECONDS


def test_registry_never_shortens_active_cooldown():
    reg = FeedbackRegistry()
    t0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    reg.report("agy", ok=False, status=429, retry_after_seconds=900, now=t0)
    r = reg.report("agy", ok=False, status=500, error="boom", now=t0 + timedelta(seconds=10))
    assert r["kind"] == "transient"
    assert r["seconds_left"] == 890


def test_registry_clear():
    reg = FeedbackRegistry()
    reg.report("codex", ok=False, status=429)
    reg.report("grok", ok=False, status=429)
    reg.clear("codex")
    assert set(reg.active()) == {"grok"}
    reg.clear()
    assert reg.active() == {}


# --- payload integration --------------------------------------------------------

def test_cooldown_makes_pool_and_models_unusable(fake_catalog):
    cooldowns = {"codex": {"cooling": True, "unavailable_until": "2026-09-10T12:01:00Z", "seconds_left": 60,
                           "backoff_level": 1, "kind": "rate_limit", "last_error": "429"}}
    out = build_routing_payload(_all_ok(), now=NOW, cooldowns=cooldowns, catalog=fake_catalog)
    assert out["pools"]["codex"]["usable"] is False
    assert out["pools"]["codex"]["cooldown"]["cooling"] is True
    assert out["pools"]["codex"]["cooldown"]["seconds_left"] == 60
    assert out["pools"]["claude"]["cooldown"]["cooling"] is False
    mid = out["models"]["model-codex-mid"]
    assert mid["usable"] is False
    assert mid["cooldown"]["kind"] == "rate_limit"
    # codex was the top T2 pick in the all-ok fixture; now it drops behind
    t2 = out["tiers"]["T2"]
    assert t2["vendor"] != "codex"
    codex_rows = [c for c in t2["candidates"] if c["vendor"] == "codex"]
    assert all(c["cooling"] and not c["usable"] and c["wait_seconds"] == 60 for c in codex_rows)


def test_avoid_vendor_and_vendors_filters_affect_eligible_only(fake_catalog):
    out = build_routing_payload(_all_ok(), now=NOW, avoid_vendor="codex", catalog=fake_catalog)
    t2 = out["tiers"]["T2"]
    assert t2["vendor"] != "codex"
    codex_rows = [c for c in t2["candidates"] if c["vendor"] == "codex"]
    assert all(c["usable"] and not c["eligible"] for c in codex_rows)
    assert t2["eligible_candidates"] == t2["usable_candidates"] - len(codex_rows)
    assert out["filters"]["avoid_vendor"] == "codex"

    out = build_routing_payload(_all_ok(), now=NOW, vendors=["claude", "codex"], catalog=fake_catalog)
    for tier in ("T0", "T1", "T2", "T3", "review"):
        assert out["tiers"][tier]["vendor"] in {"claude", "codex"}
        assert all(c["vendor"] in {"claude", "codex"} for c in out["tiers"][tier]["candidates"] if c["eligible"])
    assert out["filters"]["vendors"] == ["claude", "codex"]


def test_min_score_filter(fake_catalog):
    out = build_routing_payload(_all_ok(), now=NOW, min_score=99.0, catalog=fake_catalog)
    t3 = out["tiers"]["T3"]
    assert t3["eligible_candidates"] == 0
    assert t3["usable_candidates"] == 4
    assert "caller's filters" in t3["reason"]
    # candidates keep their usable flag; recommended is still the best row
    assert t3["recommended"] == "model-codex-top"


def test_wait_seconds_when_nothing_eligible(fake_catalog):
    # claude 5h critical (resets in 11000 s), codex cooling 300 s, grok missing.
    snaps = [s for s in _all_ok(claude_5h_used=95.0) if s.provider != ProviderId.SUPERGROK]
    cooldowns = {"codex": {"cooling": True, "seconds_left": 300, "backoff_level": 1, "kind": "rate_limit"}}
    out = build_routing_payload(snaps, now=NOW, cooldowns=cooldowns, catalog=fake_catalog)
    t3 = out["tiers"]["T3"]
    assert t3["eligible_candidates"] == 0
    assert t3["wait_seconds"] == 300
    assert t3["next_available_at"] == (NOW + timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
    assert "back in 300s" in t3["reason"]
    by_model = {c["model"]: c for c in t3["candidates"]}
    assert by_model["model-codex-top"]["wait_seconds"] == 300
    assert by_model["model-c-top"]["wait_seconds"] == 11_000
    assert by_model["model-g-top"]["wait_seconds"] is None  # missing provider: unknown
    # with something eligible there is no wait
    assert out["tiers"]["T2"]["wait_seconds"] is None
    assert out["tiers"]["T2"]["vendor"] == "agy"


# --- HTTP ---------------------------------------------------------------------

class _StubPoller:
    def __init__(self, snaps):
        self._snaps = snaps

    def get_snapshots(self):
        return self._snaps


class _StubRepo:
    def get_history(self, **kwargs):
        return []


def _client():
    from app.main import create_app

    app: FastAPI = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.state.poller = _StubPoller(_all_ok())
    app.state.repository = _StubRepo()
    app.state.routing_feedback = FeedbackRegistry()
    return TestClient(app)


@asynccontextmanager
async def _noop_lifespan(app):
    yield


def test_feedback_endpoint_round_trip(fake_catalog, monkeypatch):
    monkeypatch.setattr("app.services.routing_view.CATALOG", fake_catalog)
    with _client() as c:
        before = c.get("/api/routing?tier=T2").json()
        assert before["vendor"] == "codex"

        r = c.post("/api/routing/feedback", json={"model": "model-codex-mid", "status": 429, "error": "rate limit"})
        assert r.status_code == 200
        assert r.json()["pool"] == "codex"
        assert r.json()["kind"] == "rate_limit"
        assert r.json()["cooldown_seconds"] == 60

        after = c.get("/api/routing?tier=T2").json()
        assert after["vendor"] != "codex"
        assert c.get("/api/routing?model=model-codex-top").json()["usable"] is False
        state = c.get("/api/routing/feedback").json()
        assert state["pools"]["codex"]["cooling"] is True

        # filters are honoured on the tier view
        filt = c.get("/api/routing?tier=T2&vendors=claude,codex&min_score=10").json()
        assert filt["vendor"] == "claude"
        assert filt["eligible_candidates"] >= 1

        ok = c.post("/api/routing/feedback", json={"model": "model-codex-mid", "ok": True})
        assert ok.json()["cooling"] is False
        assert c.get("/api/routing?tier=T2").json()["vendor"] == "codex"

        assert c.post("/api/routing/feedback", json={"model": "nope"}).status_code == 404
        assert c.post("/api/routing/feedback", json={"pool": "nope"}).status_code == 404
        assert c.post("/api/routing/feedback", json={}).status_code == 422
        assert c.post("/api/routing/feedback", json={"pool": "grok", "status": 429}).json()["pool"] == "grok"
        assert c.delete("/api/routing/feedback?pool=grok").json()["cleared"] == "grok"
        assert "grok" not in c.get("/api/routing/feedback").json()["pools"]
