"""Reviewable filters, explicit wallet saves and allowlisted social records."""
from __future__ import annotations
import asyncio
import json
import time
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict
from .developer import _session, _write_session, _raise, _check_origin
from .. import developer_access as access, user_features, product_filters as filters, product_wallet as wallets, product_social as social
from ..realtime_projection import read_projection_json, ProjectionUnavailable

router = APIRouter(prefix='/product', tags=['product-services'])
_slots = asyncio.Semaphore(4)
_rates = {}


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ParseBody(Body):
    text: str = Field(min_length=1, max_length=600)


class ExecuteBody(Body):
    filters: list[dict] = Field(max_length=12)
    confirmed: bool
    limit: int = Field(default=50, ge=1, le=100)


class WalletBody(Body):
    address: str = Field(min_length=42, max_length=42)


class SaveWalletBody(WalletBody):
    label: str = Field(default='', max_length=60)
    consent: bool


class PostBody(Body):
    url: str = Field(max_length=250)


class AccountBody(Body):
    handle: str = Field(max_length=16)
    active: bool = True


class AliasBody(Body):
    ticker: str = Field(max_length=24)
    sourceText: str = Field(max_length=600)
    alias: str | None = Field(default=None, max_length=60)


class ReviewBody(Body):
    approved: bool


def response(value):
    return JSONResponse(value, headers={'Cache-Control': 'no-store'})


def rate(request, category, interval=3):
    # Counts only, no wallet/address/text retained. Per-process, supplementary
    # to the edge's rate limit and the provider's persisted spending cap.
    now = time.monotonic()
    key = (category, request.client.host if request.client else 'unknown')
    if now-_rates.get(key, -interval) < interval:
        raise HTTPException(429, detail='request-rate-limit', headers={'Retry-After': str(interval)})
    if len(_rates) > 2000:
        for old_key in [k for k, at in _rates.items() if now-at > 600]:
            _rates.pop(old_key, None)
    _rates[key] = now


async def operator(request, *, write=False):
    user, csrf = await (_write_session(request) if write else _session(request))
    if not user_features.is_operator(user):
        raise HTTPException(403, detail='operator-required')
    return user


@router.get('/capabilities')
async def capabilities():
    return response({'wallet': wallets.capabilities(), 'kol': await asyncio.to_thread(social.accounts),
                     'naturalFilters': {'available': True, 'modelEnabled': filters.model_ready(), 'schema': filters.FILTER_SCHEMA},
                     'aliases': {'requiresOperator': True, 'modelEnabled': filters.model_ready()}, 'assetFacts': {'modelEnabled': False, 'maxSentences': 3}})


@router.post('/filters/parse')
async def parse(body: ParseBody, request: Request):
    _check_origin(request); rate(request, 'parse')
    try:
        async with _slots:
            return response(await filters.parse_filters(body.text))
    except access.AccessError as exc:
        _raise(exc)


@router.post('/filters/execute')
async def execute(body: ExecuteBody, request: Request):
    _check_origin(request); rate(request, 'execute', 1)
    try:
        return response(await filters.execute_filters(body.filters, body.confirmed, body.limit))
    except access.AccessError as exc:
        _raise(exc)
    except ProjectionUnavailable:
        raise HTTPException(503, detail='snapshot-not-ready')


@router.post('/wallet/profile')
async def wallet(body: WalletBody, request: Request):
    _check_origin(request); rate(request, 'wallet', 10)
    try:
        async with _slots:
            return response(await wallets.wallet_profile(body.address))
    except access.AccessError as exc:
        _raise(exc)
    except ProjectionUnavailable:
        raise HTTPException(503, detail='snapshot-not-ready')


@router.get('/wallet/saved')
async def saved_wallets(request: Request):
    user, _ = await _session(request)
    await asyncio.to_thread(social.init)
    with access.connection() as db:
        items = [dict(r) for r in db.execute('SELECT address,label,created_at FROM product_wallet_saved WHERE user_id=? ORDER BY created_at', (user['id'],))]
    return response({'items': items})


@router.post('/wallet/saved')
async def save_wallet(body: SaveWalletBody, request: Request):
    user, _ = await _write_session(request)
    if body.consent is not True:
        raise HTTPException(400, detail='wallet-save-consent-required')
    try:
        address = wallets.normalize_address(body.address)
    except access.AccessError as exc:
        _raise(exc)
    await asyncio.to_thread(social.init)
    with access.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        count = db.execute('SELECT count(*) FROM product_wallet_saved WHERE user_id=?', (user['id'],)).fetchone()[0]
        existing = db.execute('SELECT 1 FROM product_wallet_saved WHERE user_id=? AND address=?', (user['id'], address)).fetchone()
        if count >= 10 and not existing:
            raise HTTPException(409, detail='saved-wallet-limit')
        db.execute('INSERT INTO product_wallet_saved VALUES (?,?,?,?) ON CONFLICT(user_id,address) DO UPDATE SET label=excluded.label', (user['id'], address, body.label.strip(), social.now_ms()))
        db.commit()
    return await saved_wallets(request)


@router.delete('/wallet/saved/{address}')
async def delete_wallet(address: str, request: Request):
    user, _ = await _write_session(request)
    await asyncio.to_thread(social.init)
    with access.connection() as db:
        db.execute('DELETE FROM product_wallet_saved WHERE user_id=? AND address=?', (user['id'], address.lower()))
    return await saved_wallets(request)


@router.get('/kol/accounts')
async def kol_accounts():
    return response(await asyncio.to_thread(social.accounts))


@router.post('/kol/accounts')
async def add_kol(body: AccountBody, request: Request):
    await operator(request, write=True)
    try:
        return response(await asyncio.to_thread(social.save_account, body.handle, body.active))
    except access.AccessError as exc:
        _raise(exc)


@router.get('/kol/mentions')
async def kol_mentions(handle: str | None = None):
    try:
        return response(await social.mentions(handle=handle))
    except access.AccessError as exc:
        _raise(exc)


@router.post('/kol/lookup')
async def kol_lookup(body: PostBody, request: Request):
    await _write_session(request); rate(request, 'kol', 5)
    try:
        return response(await social.lookup(body.url))
    except access.AccessError as exc:
        _raise(exc)
    except ProjectionUnavailable:
        raise HTTPException(503, detail='snapshot-not-ready')


@router.get('/aliases')
async def aliases(request: Request):
    await operator(request)
    return response({'items': await asyncio.to_thread(social.alias_candidates)})


@router.post('/aliases')
async def propose_alias(body: AliasBody, request: Request):
    await operator(request, write=True); rate(request, 'aliases')
    try:
        return response(await social.propose_alias(body.ticker, body.sourceText, body.alias))
    except access.AccessError as exc:
        _raise(exc)


@router.post('/aliases/{ident}/confirm')
async def confirm_alias(ident: str, body: ReviewBody, request: Request):
    await operator(request, write=True)
    try:
        return response(await asyncio.to_thread(social.review_alias, ident, body.approved))
    except access.AccessError as exc:
        _raise(exc)


@router.post('/aliases/backtest')
async def backtest(request: Request):
    await operator(request, write=True)
    try:
        return response(await asyncio.to_thread(social.backtest_aliases, json.loads(await read_projection_json('full'))))
    except ProjectionUnavailable:
        raise HTTPException(503, detail='snapshot-not-ready')
