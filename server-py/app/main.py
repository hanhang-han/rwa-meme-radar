"""RWA Meme Radar backend (FastAPI). Serves the /api/* contract previously
implemented by the Node/Hono server; collectors run as background tasks."""
import os
from contextlib import asynccontextmanager
from pathlib import Path


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

from .api import candles, dashboard, stream, token


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Background collectors start here in later phases.
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


@app.get("/api/health")
async def health():
    return {"ok": True}
