"""SSE stream: one queue per client; collectors broadcast through the hub."""
import asyncio
import json
import time

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..stream_hub import clients, hello

router = APIRouter()


@router.get("/stream")
async def get_stream():
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    clients().add(q)
    hello(q)

    async def frames():
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(q.get(), timeout=25)
                    yield frame
                except asyncio.TimeoutError:
                    yield f"event: heartbeat\ndata: {json.dumps({'at': int(time.time() * 1000)})}\n\n".encode()
        finally:
            clients().discard(q)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
