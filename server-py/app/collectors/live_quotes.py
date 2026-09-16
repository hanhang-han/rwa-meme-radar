"""liveQuotes collector (90s): batched OKX price-info refresh for candidate
assets related to verified pairs or baskets, oldest-first, with a cool-down
for tokens OKX no longer prices. Ports the Node refreshLiveQuotes/refreshQuotes."""
import asyncio
import time

from web3 import Web3

from ..db import store
from ..okx_client import okx_post
from ..stream_hub import broadcast
from .. import state
from .assets import now_ms, price_changed, save_asset

_quote_miss: dict[str, int] = {}


def _cool(tokens: list[str], answered: set[str]) -> None:
    for t in tokens:
        if t in answered:
            _quote_miss.pop(t, None)
        else:
            _quote_miss[t] = _quote_miss.get(t, 0) + 1
    if len(_quote_miss) > 800:
        for t in list(_quote_miss)[: len(_quote_miss) - 600]:
            _quote_miss.pop(t, None)


async def refresh_quotes(assets: list[dict]) -> None:
    tokens: list[str] = []
    seen = set()
    for a in assets:
        t = a.get("token", "")
        if t in seen or not (t.startswith("0x") and len(t) == 42):
            continue
        seen.add(t)
        tokens.append(t)
    if not tokens:
        return
    answered: set[str] = set()
    for i in range(0, len(tokens), 50):
        batch = tokens[i : i + 50]
        try:
            data = await okx_post(
                "/api/v6/dex/market/price-info",
                [{"chainIndex": "196", "tokenContractAddress": t} for t in batch],
                {"skip_round": True, "urgent": True},
            )
            if not isinstance(data, list):
                raise RuntimeError("Invalid quote response")
        except Exception as e:
            print(f"[quotes] chunk {i}-{i+50} skipped: {e}", flush=True)
            continue
        s = await store("196")
        for row in data:
            token = (row.get("tokenContractAddress") or "").lower()
            if token:
                answered.add(token)
            if str(row.get("chainIndex")) != "196":
                continue
            old = await s.get("asset", token)
            updated = await save_asset(s, row)
            if price_changed(old, updated):
                rebase_watch(token, updated.get("price"))
                broadcast("price", {
                    "chainId": "196", "token": token,
                    "price": updated.get("price"),
                    "change24h": updated.get("change24h"),
                    "at": now_ms(),
                })
    _cool(tokens, answered)


async def refresh_live_quotes() -> None:
    s = await store("196")
    assets = await s.all("asset")
    relation_tokens = {r.get("token") for r in await s.all("relation") if r.get("status") != "invalid"}
    relation_tokens |= {r.get("stock") for r in await s.all("relation") if r.get("status") != "invalid"}
    basket_tokens: set[str] = set()
    for b in await s.all("basket"):
        basket_tokens |= {m.get("token") for m in b.get("members", [])}

    work = [
        a for a in assets
        if a.get("kind") == "candidate"
        and (a.get("token") in relation_tokens or a.get("token") in basket_tokens)
        and _quote_miss.get(a.get("token"), 0) < 3
    ]
    work.sort(key=lambda a: (a.get("fieldTimes") or {}).get("price", 0))
    if work:
        await refresh_quotes(work[:150])


# -- watched live price: on-chain reserve ratio anchored to the OKX quote ------

_watched: dict[str, float] = {}
_watch_base: dict[str, dict] = {}


def watch_asset(token: str) -> None:
    _watched[token.lower()] = time.time()


def rebase_watch(token: str, price) -> None:
    base = _watch_base.get(token)
    if base and price is not None:
        base["price"] = price


async def _get_reserves(pool: str):
    from ..registry import w3

    def call():
        return w3().eth.call({"to": Web3.to_checksum_address(pool), "data": "0x0902f1ac"})

    try:
        raw = await asyncio.wait_for(asyncio.to_thread(call), 15)
    except Exception:
        return None
    raw = raw.hex() if isinstance(raw, bytes) else str(raw)
    if not raw or raw == "0x" or len(raw) < 130:
        return None
    return int(raw[2:66], 16), int(raw[66:130], 16)


async def refresh_watched() -> None:
    now = time.time()
    for t in list(_watched):
        if now - _watched[t] > 60:
            _watched.pop(t, None)
    if not _watched:
        return
    s = await store("196")
    relations = await s.all("relation")
    for token in list(_watched):
        asset = await s.get("asset", token)
        if not asset:
            continue
        rels = [r for r in relations if r.get("token") == token and r.get("status") == "verified"]
        rels.sort(key=lambda r: -(r.get("checkedAt") or 0))
        if not rels:
            continue
        rel = rels[0]
        try:
            pair = await _get_reserves(rel["pool"])
            if not pair:
                continue
            r0, r1 = pair
            if not r0 or not r1:
                continue
            meme_is0 = rel.get("token0", "").lower() == token
            meme_reserve = float(r0 if meme_is0 else r1)
            stock_reserve = float(r1 if meme_is0 else r0)
            base = _watch_base.get(token)
            if not base:
                _watch_base[token] = {"price": asset.get("price") or 0, "mr": meme_reserve, "sr": stock_reserve}
                continue
            if not base["price"] or base["mr"] <= 0:
                base["price"] = asset.get("price") or base["price"]
                base["mr"], base["sr"] = meme_reserve, stock_reserve
                continue
            ratio = stock_reserve / meme_reserve
            ratio_base = base["sr"] / base["mr"]
            if ratio <= 0 or ratio_base <= 0:
                continue
            price = base["price"] * (ratio / ratio_base)
            if not (price > 0):
                continue
            base["mr"], base["sr"] = meme_reserve, stock_reserve
            old = await s.get("asset", token)
            updated = await save_asset(s, {"tokenContractAddress": token, "price": price, "time": now_ms()})
            if price_changed(old, updated):
                broadcast("price", {
                    "chainId": "196", "token": token,
                    "price": updated.get("price"),
                    "change24h": updated.get("change24h"),
                    "at": now_ms(),
                })
        except Exception:
            continue
