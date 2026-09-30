"""Read the durable dashboard and its exact event cursor in one database row."""
import hashlib

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response

from ..realtime_projection import read_projection_json, ProjectionUnavailable

router = APIRouter()


async def _snapshot_json(view='full') -> str:
    return await read_projection_json(view)


@router.get('/dashboard')
async def get_dashboard(request: Request, view: str = 'full'):
    if view not in ('full', 'overview', 'market'):
        raise HTTPException(status_code=400, detail='unsupported dashboard view')
    try:
        body = await _snapshot_json(view)
    except ProjectionUnavailable as exc:
        raise HTTPException(status_code=503, detail='snapshot-not-ready', headers={'Retry-After': '3'}) from exc
    tag = '"' + hashlib.sha1(body.encode()).hexdigest()[:20] + '"'
    if request.headers.get('if-none-match') == tag:
        return Response(status_code=304, headers={'ETag': tag})
    return Response(content=body, media_type='application/json',
                    headers={'ETag': tag, 'Cache-Control': 'no-cache'})
