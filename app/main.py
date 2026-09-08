from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__
from app.api.routes import create_api_router
from app.config import get_settings
from app.db.repository import Repository
from app.providers.registry import build_providers
from app.services.poller import UsagePoller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("limit-usage")

WEB_DIR = Path(__file__).resolve().parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    repo = Repository(settings.db_path)
    providers = build_providers(settings)
    poller = UsagePoller(
        providers,
        repo,
        interval_seconds=settings.poll_interval_seconds,
        max_backoff_seconds=settings.max_backoff_seconds,
        refresh_min_interval_seconds=settings.refresh_min_interval_seconds,
    )
    app.state.settings = settings
    app.state.repository = repo
    app.state.poller = poller
    poller.start()
    logger.info(
        "limit-usage v%s listening config port=%s db=%s",
        __version__,
        settings.port,
        settings.db_path,
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

    def _routing_payload(request: Request, stale_after: int):
        from datetime import timedelta

        from app.models import utcnow
        from app.services.routing_view import LOOKBACK_HOURS, build_routing_payload

        repo: Repository = request.app.state.repository
        poller: UsagePoller = request.app.state.poller
        snaps = poller.get_snapshots()
        since = utcnow() - timedelta(hours=max(LOOKBACK_HOURS.values()))
        history_by: dict = {
            s.provider.value: repo.get_history(provider=s.provider.value, limit=20000, since=since)
            for s in snaps
        }
        return build_routing_payload(snaps, history_by, stale_after_seconds=stale_after)

    @api.get("/routing")
    async def routing(
        request: Request,
        model: str | None = None,
        tier: str | None = None,
        stale_after: int = 900,
    ):
        """Quota-aware routing view for dispatchers: per-pool remaining %, reset
        countdown, burn rate, and a recommended model per tier.

        ``?model=ID`` returns just that model's row; ``?tier=T2`` just that tier.
        """
        from fastapi import HTTPException

        payload = _routing_payload(request, max(0, stale_after))
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
