"""RWA Meme Radar backend (FastAPI). Serves the /api/* contract previously
implemented by the Node/Hono server; collectors run as background tasks."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from .api import dashboard, stream


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Background collectors start here in later phases.
    yield


app = FastAPI(title="RWA Meme Radar API", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(dashboard.router, prefix="/api")
app.include_router(stream.router, prefix="/api")


@app.get("/api/health")
async def health():
    return {"ok": True}
