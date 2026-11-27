"""Standalone FastAPI entrypoint for the OAS Android control plane."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from oas_control_plane.agent_router import agent_app, agent_transport_app, get_agent_control_plane
from oas_control_plane.api_logger import ensure_api_logger
from oas_control_plane.log_service import cleanup_logs


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start only metadata scheduling; this Server never runs OAS tasks."""
    ensure_api_logger()
    cleanup_logs()
    app.state.agent_dispatch_task = asyncio.create_task(_agent_dispatch_loop())
    try:
        yield
    finally:
        app.state.agent_dispatch_task.cancel()
        try:
            await app.state.agent_dispatch_task
        except asyncio.CancelledError:
            pass


async def _agent_dispatch_loop() -> None:
    """Grant at most one due-task lease to each healthy connected Agent."""
    control_plane = get_agent_control_plane()
    while True:
        await control_plane.expire_leases()
        await control_plane.dispatch_due_tasks()
        await asyncio.sleep(2)


app = FastAPI(title="OAS Android Control Plane", version="1", lifespan=lifespan)
app.include_router(agent_app)
app.include_router(agent_transport_app)
