"""Worker-owned comparison evidence, using public FX and already collected markets.

DexScreener pair-native prices retain the exact quote-token contract as their
unit. No opaque USD aggregate or stablecoin peg is promoted to an independent
price. Two legs must use the same contract/unit and exclude the target pool.
Coinbase exchange rates have no market timestamp: they are observed snapshots,
never realtime quotes, and are used only with a compatible observation window.
"""
import time
import asyncio
import json
import httpx

from ..comparisons import MAX_AGE, positive
from ..db import store, ResearchStore
from ..stock_quotes import _read_snapshot, EODHD_FILE
from .binance import status_snapshot

CHAINS = ("196", "56", "4663")
FX_URL = "https://api.coinbase.com/v2/exchange-rates?currency=USD"


def normalize_fx(payload, now):
    data = payload.get("data") or {}
    if data.get("currency") != "USD":
        raise ValueError("unexpected FX base currency")
    out = {}
    for currency in ("HKD", "USDT", "USDC", "EUR", "GBP", "JPY", "KRW"):
        rate = positive((data.get("rates") or {}).get(currency))
        if rate:
            out[currency] = {"fromCurrency": currency, "toCurrency": "USD", "rate": 1 / rate,
                             "provider": "Coinbase", "at": now, "receivedAt": now,
                             "timeKind": "observed", "scope": "indicative-exchange-rate",
                             "endpoint": "/v2/exchange-rates?currency=USD"}
    if not out:
        raise ValueError("no supported FX observations")
    return out


def native_pool_quotes(snapshot):
    """Native quote, not priceUsd: dependency path is precisely this pair."""
    out = {}
    for key, providers in (snapshot.get("pools") or {}).items():
        q = providers.get("DexScreener") or {}
        chain = str(q.get("chainId") or key.split(":")[0])
        pool = str(q.get("pool") or "").lower()
        base = str((q.get("baseToken") or {}).get("address") or "").lower()
        counter = str((q.get("quoteToken") or {}).get("address") or "").lower()
        price, at = positive(q.get("priceNative")), positive(q.get("observedAt"))
        if chain not in CHAINS or not pool or not base or not counter or base == counter or not price or not at:
            continue
        quote = {"chainId": chain, "token": base, "price": price, "at": at,
                 "currency": f"asset:{chain}:{counter}", "provider": "DexScreener",
                 "scope": "pool", "pool": pool, "dependencies": [pool],
                 "dependenciesComplete": True, "timeKind": "observed",
                 "receivedAt": at, "quoteToken": counter, "method": "pair-native-v1",
                 "evidence": {"field": "priceNative", "baseToken": base, "quoteToken": counter, "pool": pool}}
        out.setdefault((chain, base), []).append(quote)
    return out


def exchange_quotes(snapshot, fx, now):
    """Binance token contracts come from the explicit bStocks catalogue."""
    out = {}
    for row in snapshot.get("tokens") or []:
        chain = str(row.get("chainIndex") or "56")
        token = str(row.get("tokenContractAddress") or "").lower()
        price, at = positive(row.get("price")), positive(row.get("quoteAt"))
        conversion = fx.get("USDT")
        if not token or not price or not at or not conversion or now - conversion["at"] > MAX_AGE:
            continue
        # FX is sampled, so the resulting USD observation cannot be realtime.
        if abs(at - conversion["at"]) > 60_000:
            continue
        out[(chain, token)] = [{"chainId": chain, "token": token, "price": price * conversion["rate"],
                              "at": min(at, conversion["at"]), "currency": "USD", "provider": "Binance + Coinbase",
                              "scope": "exchange-token", "venue": "binance", "dependencies": [],
                              "dependenciesComplete": True, "timeKind": "observed",
                              "method": "exchange-with-observed-fx-v1",
                              "evidence": {"market": str(row.get("tokenSymbol")) + "USDT", "marketAt": at,
                                           "nativePrice": price, "fx": conversion}}]
    return out


async def refresh_comparison_inputs():
    now = int(time.time() * 1000)
    stores = {chain: await store(chain) for chain in CHAINS}
    updates={chain:[] for chain in CHAINS}
    fx = {}
    error = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(FX_URL, headers={"Accept": "application/json"})
            response.raise_for_status()
            now = int(time.time() * 1000)
            fx = normalize_fx(response.json(), now)
        for chain in stores:
            for currency, quote in fx.items():
                updates[chain].append(('fx-quote',currency,quote))
    except Exception as exc:
        # Keep prior observations with their original times. Failure is visible
        # and must never advance lastSuccessAt or make old rates look current.
        error = type(exc).__name__
        for q in await stores["196"].all("fx-quote"):
            if q.get("fromCurrency") and now - (q.get("at") or 0) <= MAX_AGE:
                fx[q["fromCurrency"]] = q
    quotes = native_pool_quotes(_read_snapshot(EODHD_FILE))
    for key, rows in exchange_quotes(status_snapshot(), fx, now).items():
        quotes.setdefault(key, []).extend(rows)
    accepted = 0
    for (chain, token), rows in quotes.items():
        rows = [q for q in rows if 0 <= now - q["at"] <= MAX_AGE]
        if not rows:
            continue
        updates[chain].append(('independent-quote',token,{"chainId":chain,"token":token,"quotes":rows,
                                                        "updatedAt":max(q['at'] for q in rows)}))
        accepted += len(rows)
    state = {"lastAttemptAt": now, "accepted": accepted, "fxAccepted": len(fx),
             "status": "partial" if error else ("current" if accepted else "no-independent-markets"),
             "reason": "fx-unavailable" if error else (None if accepted else "no-compatible-pair-native-quotes"),
             "error": error}
    for chain,s in stores.items():
        prior = await s.get("collector", "comparison-inputs") or {}
        state["lastSuccessAt"] = now if not error else prior.get("lastSuccessAt")
        updates[chain].append(('collector','comparison-inputs',dict(state)))
        await _put_facts(s,updates[chain])
    return {"requested": 1, "accepted": accepted + (len(fx) if not error else 0),
            "failed": 1 if error else 0, "reason": state["reason"]}


async def _put_facts(s,rows):
    if not isinstance(s,ResearchStore):
        for kind,ident,value in rows:await s.put(kind,ident,value)
        return
    encoded=[(s.key(kind),ident,json.dumps(value,ensure_ascii=False,allow_nan=False)) for kind,ident,value in rows]
    for offset in range(0,len(encoded),200):
        async with s._guard_write():
            await s.db.executemany('''INSERT INTO facts VALUES (?,?,?)
                ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body WHERE facts.body IS NOT excluded.body''',encoded[offset:offset+200])
            await s.db.commit()
        await asyncio.sleep(0)
