"""SSE stream placeholder (P4 wires collectors to it)."""
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
import asyncio
import json

router = APIRouter()


@router.get("/stream")
async def get_stream():
    async def frames():
        yield f"event: hello\ndata: {json.dumps({'at': 0})}\n\n"
        while True:
            await asyncio.sleep(25)
            yield "event: heartbeat\ndata: {}\n\n"

    return StreamingResponse(frames(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
