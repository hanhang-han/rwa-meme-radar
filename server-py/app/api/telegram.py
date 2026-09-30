"""Authenticated, CSRF-protected Telegram binding; never sends a message."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .developer import _session, _write_session, _raise
from .. import developer_access as access, telegram_alerts

router = APIRouter(prefix='/developer/telegram', tags=['telegram-alerts'])


@router.get('')
async def telegram_status(request: Request):
    user, _ = await _session(request)
    return JSONResponse(await asyncio.to_thread(telegram_alerts.status, user['id']), headers={'Cache-Control': 'no-store'})


@router.post('/bind')
async def bind_telegram(request: Request):
    user, _ = await _write_session(request)
    try:
        result = await asyncio.to_thread(telegram_alerts.create_bind_code, user['id'])
    except access.AccessError as exc:
        _raise(exc)
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.delete('')
async def unlink_telegram(request: Request):
    user, _ = await _write_session(request)
    return JSONResponse(await asyncio.to_thread(telegram_alerts.unlink, user['id']), headers={'Cache-Control': 'no-store'})
