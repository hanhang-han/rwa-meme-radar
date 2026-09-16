"""Aggregated snapshot endpoint backed by the research store."""
import hashlib
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..state import DATA, reload_data

router = APIRouter()

_cache: dict[str, float | str] = {"at": 0.0, "body": ""}


async def _snapshot_json() -> str:
    if time.time() - _cache["at"] > 10:
        await reload_data()
        _cache["body"] = json.dumps(DATA.payload(), ensure_ascii=False, separators=(",", ":"))
        _cache["at"] = time.time()
    return _cache["body"]


@router.get("/dashboard")
async def get_dashboard(request: Request):
    body = await _snapshot_json()
    tag = '"' + hashlib.sha1(body.encode()).digest().hex()[:20] + '"'
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    return JSONResponse(
        content=json.loads(body),
        headers={"ETag": tag, "Cache-Control": "no-cache"},
    )
