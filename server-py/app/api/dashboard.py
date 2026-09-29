"""Read the durable dashboard and its exact event cursor in one database row."""
import hashlib
import json

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response

from ..realtime_projection import read_projection_json
from ..dashboard_projection import market_dashboard, overview_dashboard

router = APIRouter()


async def _snapshot_json(view='full') -> str:
    body = await read_projection_json()
    if view == 'full':
        return body
    projection = json.loads(body)
    if view == 'overview':
        projection = overview_dashboard(projection)
    elif view == 'market':
        projection = market_dashboard(projection)
    return json.dumps(projection, ensure_ascii=False, separators=(',', ':'))


@router.get('/dashboard')
async def get_dashboard(request: Request, view: str = 'full'):
    if view not in ('full', 'overview', 'market'):
        raise HTTPException(status_code=400, detail='unsupported dashboard view')
    body = await _snapshot_json(view)
    tag = '"' + hashlib.sha1(body.encode()).hexdigest()[:20] + '"'
    if request.headers.get('if-none-match') == tag:
        return Response(status_code=304, headers={'ETag': tag})
    return Response(content=body, media_type='application/json',
                    headers={'ETag': tag, 'Cache-Control': 'no-cache'})
