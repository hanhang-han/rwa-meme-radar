"""RWA Meme Radar backend (FastAPI). Serves the /api/* contract previously
implemented by the Node/Hono server; collectors run as background tasks."""
import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path


async def _sync_apply(collector) -> None:
    collector.apply_live()


def _load_env() -> None:
    # Minimal dotenv: the Node stack loads .env at boot; this must too, or
    # every collector sees empty credentials.
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from .api import ai_route, candles, dashboard, stream, token


@asynccontextmanager
async def lifespan(_: FastAPI):
    from . import stream_hub
    from .collectors import binance as binance_collector
    from .collectors.live_quotes import refresh_live_quotes, refresh_watched
    from .collectors.scheduler import spawn_loop

    binance_collector.start_stream()
    spawn_loop("liveQuotes", 90, refresh_live_quotes)
    spawn_loop("watchedPrice", 15, refresh_watched)
    spawn_loop("binance", 30, binance_collector.refresh_binance)
    spawn_loop("binanceApply", 10, lambda: _sync_apply(binance_collector))

    async def heartbeat():
        stream_hub.broadcast("heartbeat", {"at": int(time.time() * 1000)})

    spawn_loop("streamHeartbeat", 25, heartbeat)

    from .registry import sync_registry
    from . import state as app_state

    async def registry_round():
        await sync_registry(app_state.DATA.relations)

    spawn_loop("registrySync", 600, registry_round)

    from . import briefing

    spawn_loop("aiBriefing", 1800, briefing.refresh_briefing)

    from .collectors.main_round import refresh_main_round

    spawn_loop("mainRound", 300, refresh_main_round)

    try:
        yield
    finally:
        from .db import close_all
        await close_all()


app = FastAPI(title="RWA Meme Radar API", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(dashboard.router, prefix="/api")
app.include_router(stream.router, prefix="/api")
app.include_router(token.router, prefix="/api")
app.include_router(candles.router, prefix="/api")
app.include_router(ai_route.router, prefix="/api")


@app.get("/api/health")
async def health():
    return {"ok": True}
