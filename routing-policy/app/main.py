"""FastAPI service and public HTTP API boundary for the routing policy engine.

This service exposes the routing policy decision layer over a minimal, strictly
sanitized HTTP interface designed for public Internet accessibility through a
Cloudflare Tunnel. It encapsulates model ranking, tier boundaries, and upstream
quota signals without leaking any internal provider credentials, account emails,
raw quota numbers, benchmark statistics, or server tracebacks.

Endpoints:
* GET /v1/health: Public liveness check returning status and policy_version.
* GET /v1/policy: Architectural summary of tier complexity windows and review rules.
* POST /v1/recommend: Evaluate task requirements and capacity signals to select the
  optimal model for a given tier and role.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import (
    Settings,
    get_settings,
    normalize_vendor,
    normalize_vendor_list,
)
from app.engine import Recommendation, recommend
from app.policy import Policy, load_policy
from app.signals import SignalResult, SignalSource

# Configure only this service's logger hierarchy so dependency INFO logs cannot
# disclose request destinations. The named handler guard also prevents duplicate
# lines when ``python -m app.main`` causes uvicorn to import this module by name.
logging.Formatter.converter = time.gmtime
_SERVICE_LOGGER = logging.getLogger("app")
_SERVICE_LOGGER.setLevel(logging.INFO)
_SERVICE_LOG_HANDLER_NAME = "routing-policy-stderr"
if not any(
    handler.get_name() == _SERVICE_LOG_HANDLER_NAME
    for handler in _SERVICE_LOGGER.handlers
):
    _service_handler = logging.StreamHandler()
    _service_handler.set_name(_SERVICE_LOG_HANDLER_NAME)
    _service_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
    )
    _SERVICE_LOGGER.addHandler(_service_handler)
_SERVICE_LOGGER.propagate = False
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request & Response Schemas
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Minimal liveness response exposing zero internal operational details."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    policy_version: str


class PolicyTierInfo(BaseModel):
    """Complexity interval assigned to one orchestration tier."""

    model_config = ConfigDict(extra="forbid")

    complexity: list[int]


class PolicyReviewInfo(BaseModel):
    """Cross-vendor enforcement policy for code and plan reviews."""

    model_config = ConfigDict(extra="forbid")

    cross_vendor: bool


class PolicyResponse(BaseModel):
    """Public methodology summary explaining active tiers and review rules."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    policy_version: str
    generated_at: str
    tiers: dict[str, PolicyTierInfo]
    review: PolicyReviewInfo


class ClientInfo(BaseModel):
    """Client operational constraints supplied by remote dispatchers."""

    model_config = ConfigDict(extra="ignore")

    available_vendors: list[str] | None = None


class RecommendRequest(BaseModel):
    """Recommendation query specifying task difficulty, role, and caller filters."""

    model_config = ConfigDict(extra="ignore")

    tier: Literal["T0", "T1", "T2", "T3"]
    role: Literal["implement", "review", "orchestrate"]
    implemented_by_vendor: str | None = None
    client: ClientInfo | None = None
    min_score: float | None = None


class ModelTarget(BaseModel):
    """Public model identifier paired with its owning vendor pool."""

    model_config = ConfigDict(extra="forbid")

    vendor: str
    model: str


class RecommendResponse(BaseModel):
    """Sanitized recommendation output exposing only public targets and reason codes."""

    model_config = ConfigDict(extra="forbid")

    policy_version: str
    generated_at: str
    expires_at: str
    tier: str
    role: str
    recommended: ModelTarget | None
    alternatives: list[ModelTarget]
    reason_codes: list[str]
    wait_seconds: int | None


# ---------------------------------------------------------------------------
# Lifespan and Dependencies
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize singleton policy and signal source instances on server startup."""
    settings = get_settings()
    policy = load_policy(settings.policy_dir)
    signal_source = SignalSource(
        url=settings.limit_usage_routing_url,
        ttl_seconds=settings.signal_ttl_seconds,
        timeout=settings.signal_timeout_seconds,
    )
    app.state.settings = settings
    app.state.policy = policy
    app.state.signal_source = signal_source
    yield
    # Gracefully shut down active upstream HTTP client connections on exit.
    # Note: signals.py does not expose a public aclose method and task constraints
    # strictly forbid modifying signals.py, necessitating access to _client.
    if signal_source._client is not None and not signal_source._client.is_closed:
        await signal_source._client.aclose()


def get_policy(request: Request) -> Policy:
    """Retrieve policy instance from application state, loading lazily if necessary."""
    if not hasattr(request.app.state, "policy") or request.app.state.policy is None:
        settings = get_settings()
        request.app.state.policy = load_policy(settings.policy_dir)
    return request.app.state.policy


def get_signal_source(request: Request) -> SignalSource:
    """Retrieve signal source instance from application state, initializing lazily if necessary."""
    if (
        not hasattr(request.app.state, "signal_source")
        or request.app.state.signal_source is None
    ):
        settings = get_settings()
        request.app.state.signal_source = SignalSource(
            url=settings.limit_usage_routing_url,
            ttl_seconds=settings.signal_ttl_seconds,
            timeout=settings.signal_timeout_seconds,
        )
    return request.app.state.signal_source


def get_app_settings(request: Request) -> Settings:
    """Retrieve settings instance from application state or global cache."""
    return getattr(request.app.state, "settings", get_settings())


# ---------------------------------------------------------------------------
# Application Instance & Middleware
# ---------------------------------------------------------------------------

# Explicitly disable OpenAPI/Swagger/ReDoc endpoints: the service is exposed to the
# public Internet via Cloudflare Tunnel and must strictly expose only the three
# authorized API endpoints (/v1/health, /v1/policy, /v1/recommend).
app = FastAPI(
    title="Routing Policy Service",
    description="Declarative routing policy decision layer for AI agent workflows",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def audit_and_exception_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Intercept unhandled exceptions and record §42 audit log entries for every recommend request.

    Rationale for using HTTP middleware instead of an `@app.exception_handler(Exception)`:
    In Starlette/FastAPI, ServerErrorMiddleware wraps around the routing stack and unconditionally
    re-raises unhandled exceptions after invoking exception handlers. Middleware placed inside
    ServerErrorMiddleware intercepts any unhandled exception before it reaches ServerErrorMiddleware,
    allowing the service to reliably return a clean, sanitized HTTP 500 response without letting
    exception traces leak into ASGI hosts or test clients.
    """
    start_time = time.monotonic()
    response: Response | None = None
    try:
        response = await call_next(request)
        return response
    except Exception as exc:
        # Sanitize all unexpected errors to prevent tracebacks, tokens, or URLs from leaking.
        logger.error("Unhandled error: %s", type(exc).__name__)
        response = JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "internal error"},
        )
        return response
    finally:
        # §42 Audit Logging: every request to /v1/recommend must write exactly one line
        # covering all outcomes (200, 422, 500) without leaking body contents or headers.
        if request.url.path == "/v1/recommend":
            elapsed_ms = (time.monotonic() - start_time) * 1000
            status_code = response.status_code if response is not None else 500
            tier = getattr(request.state, "tier", "unknown")
            role = getattr(request.state, "role", "unknown")
            model = getattr(request.state, "model", "none")
            policy_obj = getattr(request.app.state, "policy", None)
            policy_ver = getattr(request.state, "policy_version", None) or (
                policy_obj.policy_version if policy_obj else "unknown"
            )

            logger.info(
                "recommend: tier=%s role=%s model=%s policy_version=%s status=%d elapsed=%.1fms",
                tier,
                role,
                model,
                policy_ver,
                status_code,
                elapsed_ms,
            )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Pass standard request validation errors through to indicate caller-side schema errors."""
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Preserve standard HTTP exceptions like 404 Not Found without exposing server internals."""
    return await http_exception_handler(request, exc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/v1/health", response_model=HealthResponse)
async def health(policy: Policy = Depends(get_policy)) -> HealthResponse:
    """Return public service health and loaded policy version."""
    return HealthResponse(status="ok", policy_version=policy.policy_version)


@app.get("/v1/policy", response_model=PolicyResponse)
async def get_policy_overview(
    policy: Policy = Depends(get_policy),
) -> PolicyResponse:
    """Expose active tier complexity ranges and cross-vendor review policy."""
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    generated_at_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    tiers_summary = {
        tier_name: PolicyTierInfo(complexity=tier.complexity)
        for tier_name, tier in policy.tiers.items()
    }

    return PolicyResponse(
        schema_version=policy.schema_version,
        policy_version=policy.policy_version,
        generated_at=generated_at_str,
        tiers=tiers_summary,
        review=PolicyReviewInfo(cross_vendor=policy.review_cross_vendor),
    )


@app.post("/v1/recommend", response_model=RecommendResponse)
async def recommend_model(
    request: Request,
    body: RecommendRequest,
    policy: Policy = Depends(get_policy),
    signal_source: SignalSource = Depends(get_signal_source),
    settings: Settings = Depends(get_app_settings),
) -> RecommendResponse:
    """Select the optimal model for a requested tier and role using real-time capacity signals."""
    # Stash request audit context on request.state for middleware logging across all outcomes.
    request.state.tier = body.tier
    request.state.role = body.role
    request.state.policy_version = policy.policy_version
    request.state.model = "none"

    # Normalize vendor identifiers to internal pool namespace.
    raw_vendors = body.client.available_vendors if body.client else None
    normalized_available_vendors = normalize_vendor_list(raw_vendors)

    normalized_implemented_by = None
    if body.implemented_by_vendor:
        normalized_implemented_by = (
            normalize_vendor(body.implemented_by_vendor)
            or body.implemented_by_vendor
        )

    # Fetch capacity signals and evaluate recommendation.
    signal_result: SignalResult = await signal_source.get()

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    rec: Recommendation = recommend(
        policy,
        signal_result.signals,
        tier=body.tier,
        role=body.role,
        implemented_by_vendor=normalized_implemented_by,
        available_vendors=normalized_available_vendors,
        min_score=body.min_score,
        now=now_utc,
    )

    # Format ISO 8601 UTC timestamps with explicit Z suffix.
    generated_at_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at_dt = now_utc + timedelta(seconds=settings.recommend_ttl_seconds)
    expires_at_str = expires_at_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    recommended_target = (
        ModelTarget(
            vendor=rec.recommended["vendor"],
            model=rec.recommended["model"],
        )
        if rec.recommended is not None
        else None
    )

    alternative_targets = [
        ModelTarget(vendor=alt["vendor"], model=alt["model"])
        for alt in rec.alternatives
    ]

    chosen_model = rec.recommended["model"] if rec.recommended else "none"
    request.state.model = chosen_model

    return RecommendResponse(
        policy_version=policy.policy_version,
        generated_at=generated_at_str,
        expires_at=expires_at_str,
        tier=rec.tier,
        role=rec.role,
        recommended=recommended_target,
        alternatives=alternative_targets,
        reason_codes=rec.reason_codes,
        wait_seconds=rec.wait_seconds,
    )


if __name__ == "__main__":
    import uvicorn

    server_settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=server_settings.host,
        port=server_settings.port,
    )
