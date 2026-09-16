"""Binance bStocks collector: shared miniTicker WebSocket for live prices plus
a 30s REST fallback for the catalogue rows and 24h statistics. Ports the Node
binance-ws/bstocks pair; state is kept in memory and snapshotted to
data/binance.json for restart continuity."""
import asyncio
import json
import os
import time

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
_started = False


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
                "tokenToAssetRatio": 1,
                "quoteAt": now if q else None,
                "exchangeTrades24h": _i(q.get("count")) if q else None,
                "volumeScope": "exchange", "onchain": True,
            })
        state["tokens"] = tokens
        state["updatedAt"] = now
        state["status"] = "ready"
        state["error"] = None
        _save()
    except Exception as e:
        state["status"] = "stale" if state.get("updatedAt") else "error"
        state["error"] = str(e)[:120]


def _f(v):
    try:
        return float(v)
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


_restored = False


def _restore_once() -> None:
    global _restored
    if not _restored:
        _restored = True
        _restore()


def start_stream() -> None:
    """Subscribe to all bStocks miniTicker streams; reconnect with backoff."""
    global _started
    if _started or os.environ.get("NODE_ENV") == "test":
        return
    _started = True
    streams = "/".join(b["symbol"].lower() + "usdt@miniTicker" for b in BSTOCKS)

    async def connect():
        retry = 0
        while True:
            try:
                async with websockets.connect(
                    f"wss://stream.binance.com:9443/stream?streams={streams}", ping_interval=20
                ) as ws:
                    async for message in ws:
                        try:
                            d = json.loads(message).get("data") or {}
                            sym = str(d.get("s", "")).replace("USDT", "")
                            close = _f(d.get("c"))
                            if sym and close is not None:
                                _live[sym] = {
                                    "price": close,
                                    "open24h": _f(d.get("o")) or 0,
                                    "quoteVol24h": _f(d.get("q")),
                                    "at": int(time.time() * 1000),
                                }
                                retry = 0
                        except Exception:
                            continue
            except Exception:
                pass
            retry = min(retry + 1, 6)
            await asyncio.sleep(2 ** retry)
    asyncio.create_task(connect(), name="binance-ws")
