"""Invite-only developer portal. No email provider or payment service needed."""
from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import developer_access as access
from .. import user_features


router = APIRouter(prefix="/developer", tags=["developer-beta"])
COOKIE_NAME = "cx_trial_session"
_credential_slots = asyncio.Semaphore(2)


@asynccontextmanager
async def _credential_slot():
    try:
        await asyncio.wait_for(_credential_slots.acquire(), timeout=0.02)
    except TimeoutError:
        raise HTTPException(429, detail="registration-busy", headers={"Retry-After": "2"})
    try:
        yield
    finally:
        _credential_slots.release()


class RegisterBody(BaseModel):
    email: str = Field(max_length=320)
    inviteCode: str = Field(max_length=128)
    password: str = Field(max_length=256)


class LoginBody(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)


class KeyBody(BaseModel):
    name: str = Field(max_length=60)


def _raise(exc: access.AccessError):
    headers = {"Cache-Control": "no-store"}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(max(1, exc.retry_after))
    raise HTTPException(status_code=exc.status, detail=exc.code, headers=headers)


def _check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return  # non-browser clients; authenticated writes still require CSRF
    parsed = urlsplit(origin)
    if not parsed.scheme or not parsed.netloc or parsed.path not in ("", "/"):
        raise HTTPException(403, detail="invalid-origin")
    allowed = {"https://cliperx.com", "https://www.cliperx.com"}
    allowed.update(item.strip().rstrip("/") for item in
                   os.environ.get("DEVELOPER_ALLOWED_ORIGINS", "").split(",") if item.strip())
    # Same-origin localhost development works without production allowlist edits.
    host = request.headers.get("host", "").lower()
    if host.startswith(("localhost:", "127.0.0.1:", "[::1]:")) or host in ("localhost", "127.0.0.1", "[::1]"):
        allowed.update({f"http://{host}", f"https://{host}"})
        if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
            allowed.add(origin.rstrip("/"))  # Vite dev proxy may change Host/port.
    if origin.rstrip("/") not in allowed:
        raise HTTPException(403, detail="invalid-origin")


def _response(user: dict, token: str, csrf: str) -> JSONResponse:
    response = JSONResponse({"user": user, "csrfToken": csrf, "operator": user_features.is_operator(user),
                             "limits": {"perMinute": access.PER_MINUTE, "perDay": access.PER_DAY}},
                            headers={"Cache-Control": "no-store"})
    response.set_cookie(COOKIE_NAME, token, max_age=access.SESSION_SECONDS,
                        path="/", httponly=True, samesite="lax",
                        secure=os.environ.get("DEVELOPER_COOKIE_SECURE", "1") != "0")
    return response


async def _session(request: Request) -> tuple[dict, str]:
    current = await asyncio.to_thread(access.session, request.cookies.get(COOKIE_NAME))
    if current is None:
        raise HTTPException(401, detail="login-required", headers={"Cache-Control": "no-store"})
    return current


async def _write_session(request: Request) -> tuple[dict, str]:
    _check_origin(request)
    user, csrf = await _session(request)
    submitted = request.headers.get("x-csrf-token", "")
    if not submitted or not hmac.compare_digest(csrf, submitted):
        raise HTTPException(403, detail="invalid-csrf-token")
    return user, csrf


@router.post("/register")
async def register(body: RegisterBody, request: Request):
    _check_origin(request)
    try:
        async with _credential_slot():
            user, token, csrf = await asyncio.to_thread(access.register, body.email, body.inviteCode, body.password,
                                                        request.client.host if request.client else "unknown")
    except access.AccessError as exc:
        _raise(exc)
    return _response(user, token, csrf)


@router.post("/login")
async def login(body: LoginBody, request: Request):
    _check_origin(request)
    try:
        async with _credential_slot():
            user, token, csrf = await asyncio.to_thread(access.login, body.email, body.password,
                                                        request.client.host if request.client else "unknown")
    except access.AccessError as exc:
        _raise(exc)
    return _response(user, token, csrf)


@router.get("/me")
async def me(request: Request):
    user, csrf = await _session(request)
    return JSONResponse({"user": user, "csrfToken": csrf, "operator": user_features.is_operator(user),
                         "limits": {"perMinute": access.PER_MINUTE, "perDay": access.PER_DAY}},
                        headers={"Cache-Control": "no-store"})


@router.post("/logout")
async def logout(request: Request):
    await _write_session(request)
    await asyncio.to_thread(access.revoke_session, request.cookies[COOKIE_NAME])
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.get("/keys")
async def keys(request: Request):
    user, _ = await _session(request)
    items = await asyncio.to_thread(access.list_keys, user["id"])
    return JSONResponse({"items": items}, headers={"Cache-Control": "no-store"})


@router.post("/keys")
async def create_key(body: KeyBody, request: Request):
    user, _ = await _write_session(request)
    try:
        item, secret = await asyncio.to_thread(access.create_key, user["id"], body.name)
    except access.AccessError as exc:
        _raise(exc)
    return JSONResponse({"key": item, "secret": secret}, headers={"Cache-Control": "no-store"})


@router.delete("/keys/{key_id}")
async def delete_key(key_id: str, request: Request):
    user, _ = await _write_session(request)
    if not await asyncio.to_thread(access.revoke_key, user["id"], key_id):
        raise HTTPException(404, detail="key-not-found")
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@router.get("/usage")
async def usage(request: Request):
    user, _ = await _session(request)
    return JSONResponse(await asyncio.to_thread(access.usage, user["id"]),
                        headers={"Cache-Control": "no-store"})


class WatchChanges(BaseModel):
    add: list[str] = Field(default_factory=list, max_length=200)
    remove: list[str] = Field(default_factory=list, max_length=200)


class AlertPreferences(BaseModel):
    newPool: bool = True
    largeTrade: bool = False
    riskChange: bool = True
    tradeUsd: float = Field(default=10000, ge=100, le=1e9)


@router.get('/watches')
async def watches(request: Request):
    user, _ = await _session(request)
    return JSONResponse({'items': await asyncio.to_thread(user_features.list_watches, user['id']), 'userId': user['id']},
                        headers={'Cache-Control': 'no-store'})


@router.patch('/watches')
async def update_watches(body: WatchChanges, request: Request):
    user, _ = await _write_session(request)
    try:
        items = await asyncio.to_thread(user_features.change_watches, user['id'], body.add, body.remove)
    except access.AccessError as exc:
        _raise(exc)
    return JSONResponse({'items': items, 'userId': user['id']}, headers={'Cache-Control': 'no-store'})


@router.get('/alerts')
async def alerts(request: Request):
    user, _ = await _session(request)
    return JSONResponse(await asyncio.to_thread(user_features.alert_preferences, user['id']),
                        headers={'Cache-Control': 'no-store'})


@router.put('/alerts')
async def update_alerts(body: AlertPreferences, request: Request):
    user, _ = await _write_session(request)
    try:
        preferences = await asyncio.to_thread(user_features.save_alert_preferences, user['id'], body.model_dump())
    except access.AccessError as exc:
        _raise(exc)
    return JSONResponse(preferences, headers={'Cache-Control': 'no-store'})
