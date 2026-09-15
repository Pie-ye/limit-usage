"""Integration and contract tests for the routing policy FastAPI service.

Validates the complete HTTP boundary including parameter validation, response schema
sanitization, vendor namespace normalization, and failure containment without
initiating external network calls or spinning up background containers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.policy import load_policy
from app.signals import SignalResult


class StubSignalSource:
    """Configurable in-memory signal source to isolate API tests from network I/O."""

    def __init__(
        self,
        result: SignalResult | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._result = result
        self._raise_exc = raise_exc

    async def get(self, *, now: datetime | None = None) -> SignalResult:
        if self._raise_exc is not None:
            raise self._raise_exc
        if self._result is not None:
            return self._result
        return SignalResult(
            signals={
                "claude": {
                    "usable": True,
                    "score": 85.0,
                    "level": "ok",
                    "cooling": False,
                    "seconds_until_reset": 3600,
                    "stale": False,
                },
                "codex": {
                    "usable": True,
                    "score": 90.0,
                    "level": "ok",
                    "cooling": False,
                    "seconds_until_reset": 3600,
                    "stale": False,
                },
                "grok": {
                    "usable": True,
                    "score": 75.0,
                    "level": "ok",
                    "cooling": False,
                    "seconds_until_reset": 3600,
                    "stale": False,
                },
                "agy": {
                    "usable": True,
                    "score": 80.0,
                    "level": "ok",
                    "cooling": False,
                    "seconds_until_reset": 3600,
                    "stale": False,
                },
                "agy-3p": {
                    "usable": True,
                    "score": 70.0,
                    "level": "ok",
                    "cooling": False,
                    "seconds_until_reset": 3600,
                    "stale": False,
                },
            },
            stale=False,
            degraded=False,
            fetched_at=datetime.now(timezone.utc),
        )


@pytest.fixture
def client() -> TestClient:
    """Provide a TestClient with a healthy stub signal source."""
    with TestClient(app) as test_client:
        test_client.app.state.signal_source = StubSignalSource()
        yield test_client


def test_health_endpoint_exact_keys(client: TestClient) -> None:
    """Case a: GET /v1/health must return exactly two keys (status and policy_version)."""
    response = client.get("/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == {"status", "policy_version"}
    assert len(data) == 2
    assert data["status"] == "ok"
    assert isinstance(data["policy_version"], str)


def test_policy_endpoint_no_internal_model_or_scoring_keys(
    client: TestClient,
) -> None:
    """Case b: GET /v1/policy must not leak model IDs, bench, cost_rank, pool, or provider keys."""
    response = client.get("/v1/policy")
    assert response.status_code == 200
    text = response.text

    policy_dir = Path(__file__).parent.parent / "policy"
    policy = load_policy(policy_dir)

    for model_id in policy.models.keys():
        assert model_id not in text, f"Found leaked model id: {model_id}"

    forbidden_keys = ("bench", "cost_rank", "pool", "provider")
    for key in forbidden_keys:
        assert key not in text, f"Found leaked policy attribute: {key}"

    data = response.json()
    assert data["schema_version"] == 1
    assert "T0" in data["tiers"]
    assert "review" in data
    assert data["review"]["cross_vendor"] is True


def test_recommend_normal_path_ttl_difference(client: TestClient) -> None:
    """Case c: POST /v1/recommend returns complete fields and expires_at - generated_at == 300s."""
    payload = {"tier": "T2", "role": "implement"}
    response = client.post("/v1/recommend", json=payload)
    assert response.status_code == 200
    data = response.json()

    expected_keys = {
        "policy_version",
        "generated_at",
        "expires_at",
        "tier",
        "role",
        "recommended",
        "alternatives",
        "reason_codes",
        "wait_seconds",
    }
    assert set(data.keys()) == expected_keys

    gen_dt = datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00"))
    exp_dt = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    assert (exp_dt - gen_dt).total_seconds() == 300


def test_recommend_invalid_tier_or_role_422(client: TestClient) -> None:
    """Case d: tier: 'T9' -> 422; role: 'deploy' -> 422."""
    resp_tier = client.post(
        "/v1/recommend",
        json={"tier": "T9", "role": "implement"},
    )
    assert resp_tier.status_code == 422

    resp_role = client.post(
        "/v1/recommend",
        json={"tier": "T2", "role": "deploy"},
    )
    assert resp_role.status_code == 422


def test_vendor_alias_mapping_anthropic_and_claude(client: TestClient) -> None:
    """Case e: available_vendors ['anthropic'] and ['claude'] yield identical recommendations."""
    resp_anthropic = client.post(
        "/v1/recommend",
        json={
            "tier": "T2",
            "role": "implement",
            "client": {"available_vendors": ["anthropic"]},
        },
    )
    resp_claude = client.post(
        "/v1/recommend",
        json={
            "tier": "T2",
            "role": "implement",
            "client": {"available_vendors": ["claude"]},
        },
    )

    assert resp_anthropic.status_code == 200
    assert resp_claude.status_code == 200

    data_a = resp_anthropic.json()
    data_c = resp_claude.json()

    assert data_a["recommended"] == data_c["recommended"]
    assert data_a["alternatives"] == data_c["alternatives"]
    assert data_a["recommended"]["vendor"] == "claude"


def test_cross_vendor_review_different_vendor(client: TestClient) -> None:
    """Case f: role='review' with implemented_by_vendor='codex' never recommends codex."""
    response = client.post(
        "/v1/recommend",
        json={
            "tier": "T3",
            "role": "review",
            "implemented_by_vendor": "codex",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["recommended"] is not None
    assert data["recommended"]["vendor"] != "codex"
    assert "cross_vendor_review" in data["reason_codes"]


def test_signals_unavailable_returns_200_null_recommended(
    client: TestClient,
) -> None:
    """Case g: When signals are all unavailable, recommended is null, reason_codes has fallback_static, HTTP is 200."""
    client.app.state.signal_source = StubSignalSource(
        result=SignalResult(signals={}, stale=True, degraded=True, fetched_at=None)
    )
    response = client.post(
        "/v1/recommend",
        json={"tier": "T2", "role": "implement"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["recommended"] is None
    assert "fallback_static" in data["reason_codes"]
    assert "no_eligible_candidate" in data["reason_codes"]


def test_extra_unknown_fields_accepted(client: TestClient) -> None:
    """Case h: Unknown additional fields must not cause 422 validation errors."""
    response = client.post(
        "/v1/recommend",
        json={
            "tier": "T2",
            "role": "implement",
            "zzz": 1,
            "extra_field": "future_client_metadata",
            "client": {
                "available_vendors": ["openai"],
                "unknown_nested_setting": True,
            },
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["recommended"] is not None


def test_signal_exception_returns_500_generic_error(client: TestClient) -> None:
    """Case i: SignalSource.get throwing an exception returns 500 with exactly {'detail': 'internal error'}."""
    client.app.state.signal_source = StubSignalSource(
        raise_exc=RuntimeError("simulated database disconnect with secret_token_xyz")
    )
    response = client.post(
        "/v1/recommend",
        json={"tier": "T2", "role": "implement"},
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert "simulated database disconnect" not in response.text
    assert "secret_token_xyz" not in response.text
    assert "traceback" not in response.text.lower()


def test_recommend_response_does_not_leak_internal_keys(
    client: TestClient,
) -> None:
    """Case j: Recommend response serialization must not leak score, remaining_percent, cooldown, pool, bench, cost_rank."""
    response = client.post(
        "/v1/recommend",
        json={"tier": "T2", "role": "implement"},
    )
    assert response.status_code == 200
    text = response.text.lower()

    forbidden_substrings = (
        "score",
        "remaining_percent",
        "cooldown",
        "pool",
        "bench",
        "cost_rank",
    )
    for term in forbidden_substrings:
        assert term not in text, f"Leaked internal term in recommend response: {term}"


def test_unknown_vendor_in_available_vendors_ignored(
    client: TestClient,
) -> None:
    """Unrecognized vendor names are ignored while valid names in the same list are preserved."""
    response = client.post(
        "/v1/recommend",
        json={
            "tier": "T2",
            "role": "implement",
            "client": {"available_vendors": ["unsupported_cloud_ai", "anthropic"]},
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["recommended"] is not None
    assert data["recommended"]["vendor"] == "claude"
