"""bStocks ticker, individual trade and official open-kline streams.
A shared bounded writer persists each observation before publishing it.
The JSON snapshot remains a compatibility overlay, not the stream ledger.
"""
import asyncio
import json
import os
import time
import math

import httpx
import websockets

# (symbol, name, bsc address) — the deployable subset, as in bstocks.ts.
RAW = [
    ("NVDAB", "NVIDIA", "0x02Fca66C1D1aFB4E2A7884261eB00F63598a7436"),
    ("SPCXB", "SpaceX", "0xbe9D156892E55e7154BcD3cB0FEA677F9D3103E1"),
    ("QQQB", "Invesco QQQ", "0x205812CdBed920aFf76C6580abD681a46D11efc7"),
    ("SKHYB", "SK Hynix", "0xCA750eF65f295BBECd685Abf54e82CAf297BDB61"),
    ("BABAB", "Alibaba", "0x4eF9d3062c7F6ebA4AAE4990c5036598C6eff4ec"),
    ("INTCB", "Intel", "0xe614E2fc6C787035FF51f452e8E826Bfd32D5283"),
    ("MSTRB", "Strategy Inc.", "0xE87afb3076AeB0f9B14E368DE8145ae6a2826A14"),
    ("AAPLB", "Apple", "0x431a3BEE82E2ca41e49895CbECE5bB0F76A89b7A"),
    ("TSLAB", "Tesla", "0x5b1910eAaD6450E50f816082Aa078C41F10C292f"),
    ("SPYB", "SPY", "0x7138b48df7D98D7e3cc221BfE7192D0a178182D8"),
    ("GMEB", "GameStop", "0x46cEeFDa28Dd7207059ed19B0acdc026955bb15C"),
    ("HOODB", "Robinhood", "0xA394dCEa3fd3847fD793afBFd163E2e3858B7c65"),
    ("DJTB", "Trump Media & Technology Group", "0xF2ec508422174Ee564de98187db9359D318AFB6b"),
    ("MRNAB", "Moderna", "0x5fd86da9B05abE396fe9d02a4A213A7c00556503"),
    ("SOXLB", "Semicon Bull 3X ETF", "0xd97d097a89113fa59b76c572E5b2Eb647E8eefaf"),
    ("SNDKB", "Sandisk", "0x3eE4dF61bd4F867E349BEaE8bFE07bc31b4850fb"),
    ("CRCLB", "Circle Internet Group", "0x80f3D493EBCe97e343c53D29a137942416B4ffC0"),
    ("NFLXB", "Netflix", "0xD6829Ea836b6FA224d099D40E54B31262f874631"),
    ("MSFTB", "Microsoft", "0x80106cb3EAD06659A5ad19DF39D9b4733863B9b0"),
    ("SQQQB", "ProShares UltraPro Short QQQ", "0x25e572B466D152604D9E6C3e53B432B978825342"),
    ("GOOGLB", "Alphabet", "0x3F53De71c126BdaBAe20f9cD64848d317f6C3238"),
    ("SOXSB", "Direxion Semiconductor Bear 3X", "0xE28Cd11C99AF2df76bb8aDA4Cd0ef3904378280F"),
    ("TQQQB", "ProShares UltraPro QQQ", "0x462B5F13B7C7748279358962925c5De83BB9E598"),
    ("METAB", "Meta Platforms", "0x7425889FE94F9d693E8daefE88BCCed6AcFEf4c0"),
    ("TSMB", "TSMC", "0xAB78b89B5bb00236Be0B4B20704cBfa04EfC711c"),
    ("NOKB", "Nokia", "0x7c4d7a180D737Dd5A70d8065a90E6746a69C37EA"),
    ("BMNRB", "BitMine Immersion", "0x3548Da95a9eFFE481e8604664d75e95821e557F5"),
    ("SNXXB", "Tradr 2X Long SNDK ETF", "0x9e82e3da8f1115B73d24bB24113ab836FfDAb6b6"),
    ("DRAMB", "Roundhill Memory ETF", "0x93862d63fd9Fd488B1328E9b47717d75e994a84B"),
]
BSTOCKS = [{"symbol": s, "name": n, "addr": a, "underlying": s[:-1]} for s, n, a in RAW]

UA = {"User-Agent": "curl/8.7.1", "Accept": "application/json"}
SNAPSHOT = "data/binance.json"

state: dict = {"status": "starting", "updatedAt": None, "tokens": [], "error": None}
_live: dict[str, dict] = {}
_last_bcast: dict[str, float] = {}
_started = False

BSTOCK_BY_SYMBOL = {b["symbol"]: b for b in BSTOCKS}


def _restore() -> None:
    if os.environ.get("NODE_ENV") == "test" or not os.path.exists(SNAPSHOT):
        return
    try:
        saved = json.load(open(SNAPSHOT))
        if saved.get("updatedAt") and isinstance(saved.get("tokens"), list):
            state.update(saved)
            state["status"] = "stale"
    except Exception:
        pass


def _save() -> None:
    if os.environ.get("NODE_ENV") == "test":
        return
    try:
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        json.dump(state, open(SNAPSHOT + ".tmp", "w"))
        os.replace(SNAPSHOT + ".tmp", SNAPSHOT)
    except Exception:
        pass


def status_snapshot() -> dict:
    """Read worker-owned status from disk in the separate API process."""
    current = dict(state)
    if os.environ.get("NODE_ENV") != "test" and os.path.exists(SNAPSHOT):
        try:
            saved = json.load(open(SNAPSHOT))
            if (saved.get("updatedAt") or 0) >= (current.get("updatedAt") or 0):
                current = saved
        except Exception:
            pass
    updated = current.get("updatedAt")
    if updated and int(time.time() * 1000) - updated > 120_000:
        current["status"] = "stale"
    elif updated and current.get("status") in (None, "starting", "stale"):
        current["status"] = "ready"
    return current


async def refresh_binance() -> None:
    """REST fallback: 24h statistics for the known bStocks pairs."""
    _restore_once()
    pairs = [b["symbol"] + "USDT" for b in BSTOCKS]
    url = "https://api.binance.com/api/v3/ticker/24hr?symbols=" + json.dumps(pairs, separators=(",", ":"))
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url.replace(",", "%2C").replace("[", "%5B").replace("]", "%5D").replace('"', "%22"), headers=UA)
            resp.raise_for_status()
            rows = resp.json()
        quotes = {}
        if not isinstance(rows,list):
            raise ValueError('Invalid Binance response schema')
        for t in rows:
            quotes[t["symbol"].replace("USDT", "")] = t
        now = int(time.time() * 1000)
        tokens = []
        for b in BSTOCKS:
            q = quotes.get(b["symbol"])
            tokens.append({
                "chainIndex": "56", "tokenContractAddress": b["addr"],
                "assetCode": b["symbol"], "tokenSymbol": b["symbol"], "tokenName": b["name"] + " bStock",
                "stockCode": b["underlying"], "issuer": "BTech Holdings Limited",
                "price": _f(q.get("lastPrice")) if q else None,
                "stockPrice": None,
                "volume24h": _f(q.get("quoteVolume")) if q else None,
                "marketCap": None,
                "change24h": _f(q.get("priceChangePercent")) if q else None,
                "tokenToAssetRatio": None,
                "quoteAt": _i(q.get("closeTime")) if q else None,
                "exchangeTrades24h": _i(q.get("count")) if q else None,
                "volumeScope": "exchange", "onchain": True,
            })
        # A slower REST response must not overwrite the just-committed WS quote.
        previous={t.get('tokenContractAddress','').lower():t for t in state.get('tokens',[])}
        for token in tokens:
            live=previous.get(token['tokenContractAddress'].lower())
            if live and (live.get('quoteAt') or 0)>(token.get('quoteAt') or 0):
                token.update({key:live[key] for key in ('price','quoteAt','change24h','volume24h','exchangeTrades24h') if key in live})
        state["tokens"] = tokens
        state["updatedAt"] = now
        state["status"] = "ready"
        state["error"] = None
        _save()
        accepted=sum(t.get('price') is not None for t in tokens)
        return {'requested':1,'accepted':accepted,'failed':int(not accepted)}
    except Exception as e:
        state["status"] = "stale" if state.get("updatedAt") else "error"
        state["error"] = type(e).__name__
        _save()
        return {'requested':1,'accepted':0,'failed':1}



def _f(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def apply_live() -> None:
    """Fold stream prices into the shared state (10s cadence from the loop)."""
    if not _live or not state["tokens"]:
        return
    changed=False
    for t in state["tokens"]:
        hit = _live.get(str(t.get("tokenSymbol", "")).upper())
        if not hit:
            continue
        if t.get("quoteAt") is not None and hit["at"] <= t["quoteAt"]:
            continue
        t["price"] = hit["price"]
        if hit.get("open24h", 0) > 0:
            t["change24h"] = (hit["price"] / hit["open24h"] - 1) * 100
        if hit.get("quoteVol24h") is not None:
            t["volume24h"] = hit["quoteVol24h"]
        t["quoteAt"] = hit["at"]
        changed=True
    if changed:
        state.update(updatedAt=int(time.time()*1000),status='ready',error=None)
        _save()


_restored = False


def _restore_once() -> None:
    global _restored
    if not _restored:
        _restored = True
        _restore()


_feed = None
_last_save_at = 0


async def markets():
    from .market_streams import Market
    return [Market('56',b['addr'].lower(),'binance',b['symbol']+'USDT','USDT',
                   b['symbol'],underlying=b['underlying']) for b in BSTOCKS]


def _committed_quote(market, quote):
    global _last_save_at
    # Called only after the transaction containing quote + SSE outbox commits.
    _restore_once()
    if not state.get('tokens'):
        state['tokens']=[{'chainIndex':'56','tokenContractAddress':b['addr'],
            'assetCode':b['symbol'],'tokenSymbol':b['symbol'],'tokenName':b['name']+' bStock',
            'stockCode':b['underlying'],'issuer':'BTech Holdings Limited',
            'stockPrice':None,'tokenToAssetRatio':None,'volumeScope':'exchange','onchain':True}
            for b in BSTOCKS]
    at=quote['marketAt']
    for token in state['tokens']:
        if token.get('tokenContractAddress','').lower()!=market.token:
            continue
        if (token.get('quoteAt') or 0)>at:
            return
        token.update(price=quote['price'],quoteAt=at,priceCurrency='USDT',volumeCurrency='USDT',
                     venue='binance',marketId=market.market_id)
        for field in ('change24h','volume24h','exchangeTrades24h'):
            if quote.get(field) is not None:
                token[field]=quote[field]
        break
    state.update(status='ready',updatedAt=int(time.time()*1000),error=None)
    if state['updatedAt']-_last_save_at>=1000:
        _save()
        _last_save_at=state['updatedAt']


def start_stream():
    """Worker lifecycle hook: all 29 assets, regardless of page watches."""
    global _feed, _started
    if os.environ.get('NODE_ENV')=='test':
        return
    from .exchange_stream import BinanceMarketFeed
    if _feed is None:
        _feed=BinanceMarketFeed('binance','wss://stream.binance.com:9443/stream',
                                markets,on_quote=_committed_quote)
    _started=True
    return _feed.start()


async def stop_stream():
    global _started, _feed
    if _feed:
        await _feed.stop()
        _feed=None
    _started=False
    _save()
