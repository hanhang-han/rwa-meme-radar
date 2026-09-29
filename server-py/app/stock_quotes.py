"""Unified stock-quote overlays: directory rows (stock facts) carry identity
only; quotes live here keyed by venue and are merged into the payload at read
time, so dashboard, detail and SSE all read the same numbers.

Ownership during the migration window (P0-3): Node still runs EODHD reference
enrichment and the Robinhood catalogue, snapshotting to data/*.json; Python
reads those snapshots. Binance quotes are read from the worker-owned durable snapshot."""
import json
import os
import time
from .comparisons import stock_premium

EODHD_FILE = "data/market-enrichment.json"
ROBINHOOD_FILE = "data/robinhood.json"

_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 10.0


def _read_snapshot(path: str) -> dict:
    from .source_snapshots import pinned_snapshot
    pinned = pinned_snapshot(path)
    if pinned is not None:
        return pinned
    hit = _cache.get(path)
    now = time.time()
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    data: dict = {}
    try:
        if os.path.exists(path):
            mtime = os.path.getmtime(path)
            if hit and mtime <= hit[1].get("_mtime", 0) and now - hit[0] < CACHE_TTL * 4:
                return hit[1]
            with open(path) as source:
                data = json.load(source)
            data["_mtime"] = mtime
    except Exception:
        pass
    _cache[path] = (now, data)
    return data


def _f(v):
    if isinstance(v, bool):
        return None
    try:
        n = float(v)
        return n if n == n and n not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _now() -> int:
    return int(time.time() * 1000)


def binance_live() -> dict[str, dict]:
    """address -> latest Binance exchange quote for bStock tokens."""
    from .collectors import binance

    out: dict[str, dict] = {}
    for t in binance_snapshot().get("tokens") or []:
        addr = (t.get("tokenContractAddress") or "").lower()
        if not addr:
            continue
        out[addr] = t
    return out


def binance_token(address: str) -> dict | None:
    return binance_live().get(address.lower())


def robinhood_live() -> dict[str, dict]:
    saved = _read_snapshot(ROBINHOOD_FILE)
    out: dict[str, dict] = {}
    for t in saved.get("tokens") or []:
        addr = (t.get("tokenContractAddress") or "").lower()
        if addr:
            out[addr] = t
    return out


def robinhood_token(address: str) -> dict | None:
    return robinhood_live().get(address.lower())


def robinhood_status() -> dict:
    saved = _read_snapshot(ROBINHOOD_FILE)
    return {"provider": "Robinhood", "status": saved.get("status"),
            "updatedAt": saved.get("updatedAt"), "error": saved.get("error")}


def _norm_code(code: str) -> str:
    code = code.upper().strip()
    return code.lstrip("0") if code.isdigit() else code


US_UNDERLYINGS = frozenset("AAPL TSLA NVDA GOOG GOOGL AMZN META MSFT AMD INTC COIN PLTR SOFI HIMS GME AMC DJT MSTR RIVN LCID OPEN AI NKE DIS NFLX BA GM SBUX MCD NIO XPEV LI BABA JD PDD BIDU NTES TME IQ HOOD QQQ SLV SPY GLD TSM NOK MRNA SNDK CRCL SOXL SOXS TQQQ SQQQ BMNR".split())


def valid_reference(q: dict, identity: dict | None = None) -> bool:
    """Fail closed on ticker collisions and foreign cross-listings."""
    symbol = str(q.get("symbol") or "").upper()
    code, _, exchange = symbol.rpartition(".")
    currency = str(q.get("currency") or "").upper()
    identity = identity or {}
    expected = _norm_code(str(identity.get("code") or ""))
    if expected and _norm_code(code) != expected:
        return False
    if exchange == "US" and currency == "USD" and code in US_UNDERLYINGS:
        return True
    identity = identity or {}
    return exchange == "HK" and currency == "HKD" and (
        str(q.get("id") or "").startswith("XHKG:") or identity.get("market") == "HKEX") and code.isdigit()


def eodhd_quotes() -> dict[str, dict]:
    """Verified security identity -> quote. Invalid legacy cache stays on disk
    for audit, but cannot enter the public read model during migration."""
    saved = _read_snapshot(EODHD_FILE)
    out: dict[str, dict] = {}
    for original in (saved.get("stocks") or {}).values():
        if not valid_reference(original):
            continue
        q = {**original, "identityVerified": True, "realtime": False,
             "delayMs": 1_200_000, "marketSession": original.get("marketSession", "unknown")}
        if q.get("id"):
            out[q["id"]] = q
        if q.get("symbol"):
            out[q["symbol"].upper()] = q
    return out


def eodhd_status() -> dict:
    saved = _read_snapshot(EODHD_FILE)
    providers = saved.get("providers") or {}
    eod = providers.get("EODHD") or {}
    return {"provider": "EODHD", "status": eod.get("status"),
            "updatedAt": eod.get("updatedAt"), "error": eod.get("error"),
            "symbolsUsed": (saved.get("eodUsage") or {}).get("symbols"),
            "entitlement": saved.get("eodEntitlement")}


def quote_for_stock(stock_code: str | None) -> dict | None:
    if not stock_code:
        return None
    return eodhd_quotes().get(_norm_code(str(stock_code)))


def premium(token_price, stock_price, ratio, at, reference_at, **metadata) -> dict:
    return stock_premium({**metadata, "price": token_price, "stockPrice": stock_price,
                          "tokenToAssetRatio": ratio, "quoteAt": at, "referenceAt": reference_at})


def robinhood_ratio_evidence(observation: dict, address: str, now: int) -> dict:
    """Verify the producer's deployment evidence; old snapshots stay unverified."""
    evidence = observation.get("ratioEvidence")
    fields = {key: observation.get(key) for key in (
        "ratioSource", "ratioAt", "ratioTimeKind", "ratioVersion", "ratioValidUntil", "ratioEvidence")}
    ratio = _f(observation.get("tokenToAssetRatio"))
    at, until = _f(observation.get("ratioAt")), _f(observation.get("ratioValidUntil"))
    source_id = observation.get("sourceAssetId")
    # The version's numeric suffix is canonicalised by JS; checking identity
    # fields separately avoids float formatting differences across runtimes.
    version_prefix = f"rh:{source_id}:4663:{address}:"
    version_text = str(observation.get("ratioVersion") or "")
    suffix = _f(version_text[len(version_prefix):]) if version_text.startswith(version_prefix) else None
    valid = (
        observation.get("ratioVerified") is True and isinstance(evidence, dict)
        and observation.get("ratioSource") == "https://api.robinhood.com/rhj/assets"
        and bool(source_id) and ratio is not None and ratio > 0
        and evidence.get("sourceAssetId") == source_id
        and str(evidence.get("chainId")) == "4663"
        and str(evidence.get("tokenContractAddress") or "").lower() == address
        and evidence.get("stockCode") == str(observation.get("stockCode") or "").strip().upper()
        and _f(evidence.get("currentMultiplier")) == ratio
        and _f(observation.get("currentMultiplier")) == ratio
        and suffix == ratio and at is not None and until is not None
        and _f(evidence.get("observedAt")) == at and 0 < at <= now+1000
        and at < until <= at+600_000 and until >= now
        and observation.get("ratioTimeKind") == "observed"
    )
    if valid:
        return {**fields, "ratioVerified": True, "ratioReason": None}
    reason = "stale-ratio" if at and until and until < now else "issuer-ratio-evidence-unverified"
    return {**fields, "ratioVerified": False, "ratioVersion": None, "ratioValidUntil": None, "ratioReason": reason}


def apply_stock_overlays(stock_tokens: list[dict], market_quotes: list[dict] | None = None) -> list[dict]:
    """Select a display venue, preserving other observations and their scope.

    The issuer equity reference and independent exchange reference are separate
    observations. A slower equity feed never overwrites a newer issuer quote.
    Comparisons decide eligibility using the selected reference's provenance.
    """
    live_binance = dict(binance_live())
    # Durable observations arrive before the compatibility JSON checkpoint.
    # Prefer only the same venue/currency; never turn a DEX or equity quote
    # into a Binance token quote.
    for quote in market_quotes or []:
        if str(quote.get("chainId")) != "56" or quote.get("venue") != "binance" or quote.get("priceCurrency") != "USDT":
            continue
        addr = str(quote.get("token") or "").lower()
        previous = live_binance.get(addr) or {}
        if addr and (quote.get("quoteAt") or 0) >= (previous.get("quoteAt") or 0):
            live_binance[addr] = {**previous, **quote}
    live_robinhood = robinhood_live()
    quotes_eod = eodhd_quotes()
    out: list[dict] = []
    now = _now()
    reference_source = eodhd_status()
    for st in stock_tokens:
        row = dict(st)
        chain = str(row.get("chainId") or "196")
        addr = (row.get("tokenContractAddress") or "").lower()
        field_times = dict(row.get("fieldTimes") or {})
        sources = dict(row.get("fieldSources") or {})
        row.update(fieldTimes=field_times, fieldSources=sources)
        row.setdefault("provider", "OKX")
        if not row.get("priceScope") and not field_times.get("price"):
            if chain == "4663" and addr in live_robinhood:
                row.update(priceScope="issuer-derived", priceCurrency="USD", provider="Robinhood")
            elif chain == "56" and addr in live_binance:
                row.update(priceScope="exchange", priceCurrency="USDT", provider="Binance")
        row.setdefault("priceScope", "dex")
        row.setdefault("volumeScope", "dex")
        if row["priceScope"] == "dex":
            row.setdefault("priceCurrency", "USD")
        # Never manufacture verified ratio metadata from a provider's name.
        markets = dict(row.get("marketQuotes") or {})
        if row.get("price") is not None:
            markets[row["priceScope"]] = {
                "price": row["price"], "quoteAt": row.get("quoteAt") or field_times.get("price"),
                "provider": row["provider"], "scope": row["priceScope"], "currency": row.get("priceCurrency"),
                "volume24h": row.get("volume24h"), "volumeScope": row.get("volumeScope"),
            }
        references = {}
        if chain == "56" and addr in live_binance:
            b = live_binance[addr]
            if b.get("price") is not None:
                row.update(price=b["price"], quoteAt=b.get("quoteAt"), provider="Binance",
                           priceScope="exchange", priceCurrency="USDT", volumeScope="exchange",
                           venue="binance", quoteType="exchange-token")
                field_times["price"] = b.get("quoteAt")
                sources["price"] = "Binance"
                row["priceProvenance"] = {"timeKind": "market", "venue": "binance", "dependencies": [], "dependenciesComplete": True}
                for f in ("change24h", "volume24h"):
                    # A venue switch must not retain a DEX field under an
                    # exchange scope when this venue did not provide it.
                    row[f] = b.get(f)
                    field_times[f] = b.get("quoteAt") if b.get(f) is not None else None
                    sources[f] = "Binance" if b.get(f) is not None else None
                row["exchangeTrades24h"] = b.get("exchangeTrades24h")
                markets["exchange"] = {"price": b["price"], "quoteAt": b.get("quoteAt"), "provider": "Binance",
                                       "scope": "exchange", "currency": "USDT", "venue": "binance",
                                       "volume24h": b.get("volume24h"), "volumeScope": "exchange"}

        if chain == "4663" and addr in live_robinhood:
            r = live_robinhood[addr]
            if r.get("stockCode"):
                issuer_code = str(r["stockCode"]).strip().upper()
                if issuer_code != str(row.get("stockCode") or "").strip().upper():
                    row["stockIdentity"] = {"id": issuer_code, "code": issuer_code, "market": None, "status": "provider-code", "source": "Robinhood"}
                row["stockCode"] = issuer_code
            if r.get("tokenToAssetRatio") is not None and r.get("tokenToAssetRatio") != row.get("tokenToAssetRatio"):
                row.update(ratioVerified=False, ratioVersion=None, ratioAt=None, ratioValidUntil=None)
            for f in ("tokenToAssetRatio", "sourceAssetId", "currentMultiplier", "assetStatus", "dailyTradingVolume", "isTradingHalt"):
                if r.get(f) is not None:
                    row[f] = r[f]
            row.update(robinhood_ratio_evidence(r, addr, now))
            issuer = {"price": r.get("stockPrice"), "marketAt": r.get("quoteAt"),
                      "provider": "Robinhood", "currency": "USD", "scope": "issuer-reference",
                      "symbol": r.get("stockCode") or row.get("stockCode"), "realtime": False,
                      "marketSession": "halted" if r.get("isTradingHalt") else "unknown"}
            if issuer["price"] is not None:
                references["issuer"] = issuer
                row["issuerReference"] = issuer
            markets["issuer-derived"] = {"price": r.get("price"), "quoteAt": r.get("quoteAt"),
                                         "provider": "Robinhood", "scope": "issuer-derived", "currency": "USD"}
            row["issuerDerivedQuote"] = markets["issuer-derived"]
            # Keep a real DEX quote available to comparisons. Only use the
            # issuer calculation as the display fallback when DEX is absent.
            if (row.get("price") is None or row.get("priceScope") == "issuer-derived") and r.get("price") is not None:
                row.update(price=r["price"], quoteAt=r.get("quoteAt"), provider="Robinhood",
                           priceScope="issuer-derived", priceCurrency="USD", quoteType="issuer-derived", venue="robinhood")
                sources["price"] = "Robinhood"
                field_times["price"] = r.get("quoteAt")
                row["priceProvenance"] = {"timeKind": "market", "dependenciesComplete": False, "dependencies": ["issuer-reference"]}

        identity = dict(row.get("stockIdentity") or {})
        code = _norm_code(str(row.get("stockCode") or identity.get("code") or ""))
        if code in US_UNDERLYINGS:
            identity = {**identity, "id": identity.get("id") or code, "code": code, "market": "US", "currency": "USD", "status": "identified", "source": "curated-underlying-market-v1"}
            row["stockIdentity"] = identity
        q = quotes_eod.get(identity.get("id")) or quotes_eod.get(row.get("referenceSymbol")) or quotes_eod.get(str(row.get("stockCode") or ""))
        if not q and identity.get("market") == "HKEX" and code.isdigit():
            q = quotes_eod.get(code.zfill(4) + ".HK")
        if q and valid_reference(q, identity):
            references["equity"] = {**q, "provider": "EODHD", "scope": "equity-exchange", "realtime": False, "delayMs": 1_200_000}
        row["referenceObservations"] = references
        row["independentReferenceStatus"] = reference_source.get("status")
        row["independentReferenceReason"] = reference_source.get("error")
        row["marketQuotes"] = markets
        if references:
            selected = max(references.values(), key=lambda q: q.get("marketAt") or 0)
            row.update(stockPrice=selected.get("price"), referenceAt=selected.get("marketAt"),
                       referenceObservedAt=selected.get("observedAt"), referenceProvider=selected.get("provider"),
                       referenceSymbol=selected.get("symbol"), referenceCurrency=selected.get("currency"),
                       referenceVolume=selected.get("volume"), referenceChange24h=selected.get("change24h"),
                       referenceDelayMs=selected.get("delayMs"), referenceRealtime=selected.get("realtime") is True,
                       referenceScope=selected.get("scope"), referenceIdentityVerified=selected.get("identityVerified") is True,
                       referenceAdjustmentVersion=selected.get("adjustmentVersion"),
                       marketSession=selected.get("marketSession", "unknown"))
            field_times["stockPrice"] = selected.get("marketAt")
            sources["stockPrice"] = selected.get("provider")
            age = now - (selected.get("marketAt") or 0)
            row["referenceStatus"] = "stale" if age > (selected.get("delayMs") or 0)+900_000 else "delayed" if selected.get("delayMs") else "issuer-reference"
            row["referenceReason"] = "delayed-provider-endpoint" if selected.get("delayMs") else "issuer-reference-not-independent-exchange"
        else:
            # Old catalogue stockPrice is not an independently timestamped
            # security reference; do not silently keep a quarantined value.
            row.update(stockPrice=None, referenceAt=None, referenceProvider=None, referenceCurrency=None,
                       referenceRealtime=False, referenceSymbol=None, referenceScope=None, referenceIdentityVerified=False,
                       referenceAdjustmentVersion=None, referenceDelayMs=None, referenceObservedAt=None, referenceVolume=None,
                       referenceChange24h=None, marketSession="unknown", referenceStatus="missing", referenceReason="identity-unverified" if not identity.get("market") else "reference-not-observed")
            if identity.get("market") and reference_source.get("status") in ("quota-exhausted", "entitlement-required"):
                row["referenceStatus"] = reference_source["status"]
                row["referenceReason"] = reference_source["status"]
            field_times["stockPrice"] = None
            sources["stockPrice"] = None
        row["quoteAt"] = row.get("quoteAt") or field_times.get("price")
        quote_at = row.get("quoteAt")
        row["quoteStatus"] = "missing" if row.get("price") is None else "unknown" if not quote_at else "realtime" if row.get("priceScope") == "exchange" and 0 <= now-quote_at <= 30_000 else "scheduled" if 0 <= now-quote_at <= 3_600_000 else "stale"
        row["premium"] = stock_premium(row)
        if row.get("updatedAt") is None:
            row["updatedAt"] = quote_at
        out.append(row)
    return out


def binance_snapshot() -> dict:
    from .source_snapshots import pinned_snapshot
    from .collectors import binance
    pinned = pinned_snapshot(binance.SNAPSHOT)
    return pinned if pinned is not None else binance.status_snapshot()
