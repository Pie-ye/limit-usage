from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request

from app import __version__
from app.models import HealthResponse, ProviderId, UsageResponse, utcnow

if TYPE_CHECKING:
    from app.services.poller import UsagePoller


def create_api_router(poller: UsagePoller, poll_interval: int) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__, server_time=utcnow())

    @router.get("/usage", response_model=UsageResponse)
    async def usage(request: Request) -> UsageResponse:
        snapshots = poller.get_snapshots()
        return UsageResponse(
            snapshots=snapshots,
            server_time=utcnow(),
            poll_interval_seconds=poll_interval,
        )

    @router.get("/usage/{provider}")
    async def usage_one(provider: str) -> dict:
        try:
            pid = ProviderId(provider)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}") from exc
        snap = next((s for s in poller.get_snapshots() if s.provider == pid), None)
        if not snap:
            raise HTTPException(status_code=404, detail="No snapshot")
        return snap.model_dump(mode="json")

    @router.post("/refresh")
    async def refresh() -> dict:
        snapshots, warning = await poller.force_refresh()
        return {
            "ok": warning is None,
            "warning": warning,
            "server_time": utcnow().isoformat(),
            "snapshots": [s.model_dump(mode="json") for s in snapshots],
        }

    @router.get("/history")
    async def history(
        request: Request,
        provider: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict:
        repo = request.app.state.repository
        if provider:
            try:
                ProviderId(provider)
            except ValueError as exc:
                raise HTTPException(
                    status_code=404, detail=f"Unknown provider: {provider}"
                ) from exc
        rows = repo.get_history(provider=provider, limit=limit)
        return {"items": rows, "count": len(rows)}

    @router.get("/providers")
    async def providers() -> dict:
        return {
            "providers": [
                {
                    "id": p.provider_id.value,
                    "display_name": getattr(p, "display_name", p.provider_id.value),
                }
                for p in poller.providers
            ]
        }

    return router
