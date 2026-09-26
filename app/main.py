from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import __version__
from app.api.routes import create_api_router
from app.config import get_settings
from app.db.repository import Repository
from app.providers.registry import build_providers
from app.services.poller import UsagePoller
from app.services.remote_claude import RemoteClaudeRegistry
from app.services.routing_feedback import FeedbackRegistry
from catalog.tiering import Catalog, load_catalog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("limit-usage")

WEB_DIR = Path(__file__).resolve().parent / "web"


class RoutingFeedback(BaseModel):
    """One dispatch outcome. ``model`` resolves the pool; ``pool`` is the
    fallback when the model is not in the catalog (unknown ids are rejected)."""

    model: str | None = None
    pool: str | None = None
    ok: bool = False
    status: int | None = None
    error: str | None = None
    retry_after_seconds: float | None = None


class ClaudeIngest(BaseModel):
    """One machine's copy of the two files ClaudeProvider already reads.

    The wire format is the files themselves, so a push agent is a `cat` and the
    server reuses its existing parsers — there is no third schema to keep in
    sync. Both are optional: a host may have a statusline capture but no usage
    cache, or vice versa.
    """

    host: str
    statusline: dict | None = None
    usage_cache: dict | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    catalog = load_catalog()
    settings = get_settings()
    repo = Repository(settings.db_path)
    remote_claude = RemoteClaudeRegistry()
    providers = build_providers(settings, remote_claude=remote_claude)
    poller = UsagePoller(
        providers,
        repo,
        interval_seconds=settings.poll_interval_seconds,
        max_backoff_seconds=settings.max_backoff_seconds,
        refresh_min_interval_seconds=settings.refresh_min_interval_seconds,
    )
    app.state.catalog = catalog
    app.state.settings = settings
    app.state.repository = repo
    app.state.poller = poller
    app.state.routing_feedback = FeedbackRegistry()
    app.state.remote_claude = remote_claude
    poller.start()
    logger.info(
        "limit-usage v%s listening config port=%s db=%s catalog=%s",
        __version__,
        settings.port,
        settings.db_path,
        catalog.catalog_version,
    )
    yield
    await poller.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="limit-usage",
        version=__version__,
        lifespan=lifespan,
    )

    # Poller is set in lifespan; router uses a thin proxy via app.state after startup.
    # We attach routes that read from request.app.state.poller.
    # For OpenAPI at import time, build a temporary poller-less wrapper:
    from fastapi import APIRouter

    api = APIRouter(prefix="/api")

    @api.get("/health")
    async def health():
        from app.models import HealthResponse, utcnow

        return HealthResponse(status="ok", version=__version__, server_time=utcnow())

    @api.get("/usage")
    async def usage(request: Request):
        from app.models import UsageResponse, utcnow

        poller: UsagePoller = request.app.state.poller
        return UsageResponse(
            snapshots=poller.get_snapshots(),
            server_time=utcnow(),
            poll_interval_seconds=request.app.state.settings.poll_interval_seconds,
        )

    @api.get("/usage/{provider}")
    async def usage_one(provider: str, request: Request):
        from fastapi import HTTPException

        from app.models import ProviderId

        try:
            pid = ProviderId(provider)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}") from exc
        poller: UsagePoller = request.app.state.poller
        snap = next((s for s in poller.get_snapshots() if s.provider == pid), None)
        if not snap:
            raise HTTPException(status_code=404, detail="No snapshot")
        return snap.model_dump(mode="json")

    @api.post("/refresh")
    async def refresh(request: Request):
        from app.models import utcnow

        poller: UsagePoller = request.app.state.poller
        snapshots, warning = await poller.force_refresh()
        return {
            "ok": warning is None,
            "warning": warning,
            "server_time": utcnow().isoformat(),
            "snapshots": [s.model_dump(mode="json") for s in snapshots],
        }

    @api.get("/history")
    async def history(
        request: Request,
        provider: str | None = None,
        limit: int = 50,
    ):
        from fastapi import HTTPException

        from app.models import ProviderId

        if provider:
            try:
                ProviderId(provider)
            except ValueError as exc:
                raise HTTPException(
                    status_code=404, detail=f"Unknown provider: {provider}"
                ) from exc
        repo: Repository = request.app.state.repository
        rows = repo.get_history(provider=provider, limit=min(max(limit, 1), 20000))
        return {"items": rows, "count": len(rows)}

    @api.get("/trends")
    async def trends(request: Request, days: int = 7):
        from datetime import timedelta

        from app.models import utcnow
        from app.services.analytics import build_trends_payload

        days = max(1, min(days, 30))
        repo: Repository = request.app.state.repository
        poller: UsagePoller = request.app.state.poller
        since = utcnow() - timedelta(days=days)
        snaps = poller.get_snapshots()
        history_by: dict = {}
        for s in snaps:
            history_by[s.provider.value] = repo.get_history(
                provider=s.provider.value,
                limit=20000,
                since=since,
            )
        return build_trends_payload(history_by, snaps, days=days)

    @api.get("/providers")
    async def providers_list(request: Request):
        poller: UsagePoller = request.app.state.poller
        return {
            "providers": [
                {
                    "id": p.provider_id.value,
                    "display_name": getattr(p, "display_name", p.provider_id.value),
                }
                for p in poller.providers
            ]
        }

    @api.get("/homepage")
    async def homepage_flat(request: Request):
        """Flat quota summary for gethomepage customapi (no nested arrays)."""
        from app.services.homepage_view import build_homepage_payload

        poller: UsagePoller = request.app.state.poller
        return build_homepage_payload(poller.get_snapshots())

    def _routing_payload(
        request: Request,
        stale_after: int,
        *,
        avoid_vendor: str | None = None,
        vendors: list[str] | None = None,
        min_score: float | None = None,
    ):
        from datetime import timedelta

        from app.models import utcnow
        from app.services.cli_models import load_cli_models
        from app.services.routing_view import LOOKBACK_HOURS, build_routing_payload

        repo: Repository = request.app.state.repository
        poller: UsagePoller = request.app.state.poller
        feedback: FeedbackRegistry = request.app.state.routing_feedback
        now = utcnow()
        snaps = poller.get_snapshots()
        since = now - timedelta(hours=max(LOOKBACK_HOURS.values()))
        history_by: dict = {
            s.provider.value: repo.get_history(provider=s.provider.value, limit=20000, since=since)
            for s in snaps
        }
        settings = get_settings()
        cli_models = load_cli_models(settings.cli_models_path, now=now)
        missing_models = feedback.missing_models(now=now)
        return build_routing_payload(
            snaps,
            history_by,
            now=now,
            stale_after_seconds=stale_after,
            cooldowns=feedback.active(now=now),
            missing_models=missing_models,
            cli_models=cli_models,
            avoid_vendor=avoid_vendor,
            vendors=vendors,
            min_score=min_score,
            catalog=request.app.state.catalog,
        )

    @api.get("/routing")
    async def routing(
        request: Request,
        model: str | None = None,
        tier: str | None = None,
        stale_after: int = 900,
        avoid_vendor: str | None = None,
        vendors: str | None = None,
        min_score: float | None = None,
    ):
        """Quota-aware routing view for dispatchers: per-pool remaining %, reset
        countdown, burn rate, and a recommended model per tier.

        ``?model=ID`` returns just that model's row; ``?tier=T2`` just that tier.
        Filters (affect ``eligible`` / ``recommended`` only): ``avoid_vendor=claude``,
        ``vendors=claude,codex``, ``min_score=20``.
        """
        from fastapi import HTTPException

        payload = _routing_payload(
            request,
            max(0, stale_after),
            avoid_vendor=avoid_vendor or None,
            vendors=vendors.split(",") if vendors else None,
            min_score=min_score,
        )
        if model:
            row = payload["models"].get(model)
            if row is None:
                raise HTTPException(status_code=404, detail=f"Unknown model: {model}")
            return {"server_time": payload["server_time"], "model": model, **row}
        if tier:
            row = payload["tiers"].get(tier.upper() if tier.lower() != "review" else "review")
            if row is None:
                raise HTTPException(status_code=404, detail=f"Unknown tier: {tier}")
            return {"server_time": payload["server_time"], "tier": tier, **row}
        return payload

    @api.get("/routing/feedback")
    async def routing_feedback_state(request: Request):
        """Cooldown state per pool as fed by dispatch (see POST)."""
        feedback: FeedbackRegistry = request.app.state.routing_feedback
        return feedback.snapshot()

    @api.post("/routing/feedback")
    async def routing_feedback(request: Request, body: RoutingFeedback):
        """Record one dispatch outcome. A failure puts the model's pool on a
        cooldown (exponential for rate limits, fixed for auth/transient errors,
        ``retry_after_seconds`` wins, all capped at 30 min); a success clears it.
        """
        from fastapi import HTTPException

        catalog: Catalog = request.app.state.catalog
        pool = body.pool
        if body.model:
            derived = catalog.models.get(body.model)
            if derived is None:
                raise HTTPException(status_code=404, detail=f"Unknown model: {body.model}")
            pool = derived.pool
        elif pool and pool not in catalog.pools:
            raise HTTPException(status_code=404, detail=f"Unknown pool: {pool}")
        if not pool:
            raise HTTPException(status_code=422, detail="model or pool is required")
        feedback: FeedbackRegistry = request.app.state.routing_feedback
        result = feedback.report(
            pool,
            ok=body.ok,
            status=body.status,
            error=body.error,
            retry_after_seconds=body.retry_after_seconds,
            model=body.model,
        )
        logger.info(
            "routing feedback %s model=%s ok=%s status=%s kind=%s cooldown=%ss",
            pool, body.model, body.ok, body.status, result["kind"], result["cooldown_seconds"],
        )
        return result

    @api.delete("/routing/feedback")
    async def routing_feedback_clear(request: Request, pool: str | None = None):
        """Drop cooldown state (one pool, or all)."""
        feedback: FeedbackRegistry = request.app.state.routing_feedback
        feedback.clear(pool)
        return {"cleared": pool or "all"}

    @api.post("/ingest/claude")
    async def ingest_claude(request: Request, body: ClaudeIngest):
        """Accept another machine's Claude usage files.

        Claude quota is account-wide but every source of it is per-machine, so a
        host doing the actual work holds a fresher reading than this one. The
        payload is merged by observation time, not arrival time, so an out-of-order
        or delayed push cannot overwrite a newer reading.
        """
        from fastapi import HTTPException

        registry: RemoteClaudeRegistry = request.app.state.remote_claude
        try:
            result = registry.report(
                body.host,
                {"statusline": body.statusline, "usage_cache": body.usage_cache},
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        logger.info("claude ingest host=%s kinds=%s", result["host"], result["accepted"])
        return result

    @api.get("/ingest/claude")
    async def ingest_claude_state(request: Request):
        """Which hosts have pushed, and when — for debugging a silent push agent."""
        registry: RemoteClaudeRegistry = request.app.state.remote_claude
        return registry.snapshot()

    @api.delete("/ingest/claude")
    async def ingest_claude_clear(request: Request, host: str | None = None):
        """Drop pushed readings (one host, or all). A decommissioned machine
        otherwise keeps a reading alive until it ages out."""
        registry: RemoteClaudeRegistry = request.app.state.remote_claude
        return {"cleared": host or "all", "removed": registry.clear(host)}

    @api.get("/host/ups")
    async def host_ups():
        """UPS status via upower (limit-usage must run on the host)."""
        from app.services.ups import read_ups

        return read_ups()

    @api.get("/card-spend")
    async def card_spend():
        """Cathay monthly card total (flat JSON for dashboard + homepage customapi)."""
        from app.services.card_spend import read_card_spend

        return read_card_spend()

    @api.get("/concerts")
    async def concerts():
        """Taiwan concert-watch cache (flat JSON for dashboard + homepage customapi)."""
        from app.services.concerts import read_concerts

        return read_concerts()

    @api.get("/system/health")
    async def system_health():
        """Three-host system health and backup status."""
        from app.services.system_health import read_system_health

        return read_system_health()

    app.include_router(api)

    static_dir = WEB_DIR / "static"
    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            request,
            "index.html",
            {"version": __version__, "port": settings.port},
        )

    return app


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
