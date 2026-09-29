"""Comparable observations, not trading returns. All times are source times in ms.

No network calls here. Unknown units, derived quotes and opaque pool dependencies
fail closed. Changing the methodology requires a new VERSION and new history.
"""
import math
import time

VERSION = "spread-v2"
MAX_AGE = 300_000
MAX_SKEW = 60_000
LIVE_AGE = 30_000
LIVE_SKEW = 5_000


def number(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def positive(value):
    n = number(value)
    return n if n is not None and n > 0 else None


def unavailable(reason, inputs=None):
    return {"value": None, "status": "unavailable", "reason": reason,
            "method": VERSION, "inputs": inputs or {}}


def timing(times, now, live=True, max_age=MAX_AGE):
    times = [positive(t) for t in times]
    if not times or any(t is None for t in times):
        return unavailable("missing-time")
    if max(times) > now + 1000:
        return unavailable("future-quote")
    if now - min(times) > max_age:
        return unavailable("stale")
    if max(times) - min(times) > MAX_SKEW:
        return unavailable("unaligned")
    realtime = live and now - min(times) <= LIVE_AGE and max(times) - min(times) <= LIVE_SKEW
    return {"status": "realtime" if realtime else "snapshot", "reason": None,
            "at": min(times), "validUntil": min(times) + max_age,
            "realtimeUntil": min(times) + LIVE_AGE if live and max(times)-min(times) <= LIVE_SKEW else None,
            "method": VERSION}


def reference(row, *, independent=False):
    """Return stock reference in USD/share and its FX evidence; never assume HKD=USD."""
    price = positive(row.get("stockPrice"))
    currency = str(row.get("referenceCurrency") or "").upper()
    if independent and row.get("referenceScope") == "issuer-reference":
        return None, [], "issuer-reference"
    if price is None:
        return None, [], row.get("referenceReason") or ("entitlement-required" if row.get("referenceStatus") == "entitlement-required" else "missing-reference")
    if not row.get("referenceProvider"):
        return None, [], "missing-reference-source"
    if currency == "USD":
        return price, [row.get("referenceAt")], None
    fx = row.get("referenceFx") or {}
    if not currency or fx.get("fromCurrency") != currency or fx.get("toCurrency") != "USD" or not fx.get("provider") or positive(fx.get("rate")) is None:
        return None, [], "missing-fx"
    converted = positive(price * float(fx["rate"]))
    if converted is None:
        return None, [], "invalid-price"
    return converted, [row.get("referenceAt"), fx.get("at")], None


def token_usd(row):
    price = positive(row.get("price"))
    if price is None:
        return None, [], "missing-price"
    currency = str(row.get("priceCurrency") or "").upper()
    if currency == "USD":
        return price, [row.get("quoteAt")], None
    fx = row.get("quoteFx") or {}
    if (not currency or fx.get("fromCurrency") != currency or fx.get("toCurrency") != "USD"
            or not fx.get("provider") or positive(fx.get("rate")) is None):
        return None, [], "missing-token-currency"
    converted = positive(price * float(fx["rate"]))
    if converted is None:
        return None, [], "invalid-price"
    return converted, [row.get("quoteAt"), fx.get("at")], None


def ratio_problem(row, now):
    if positive(row.get("tokenToAssetRatio")) is None:
        return "missing-ratio"
    if (row.get("ratioVerified") is not True or not row.get("ratioSource")
            or not row.get("ratioVersion") or row.get("multiplierValid") is False):
        return "unverified-ratio"
    at, until = positive(row.get("ratioAt")), positive(row.get("ratioValidUntil"))
    if at is None or until is None or at > now + 1000 or until < now:
        return "stale-ratio"
    return None


def independent_stock_row(row):
    """Display may prefer a recent issuer reference; premium uses the separate equity leg."""
    q = (row.get("referenceObservations") or {}).get("equity")
    if not q:
        return row
    return {**row, "stockPrice": q.get("price"), "referenceAt": q.get("marketAt"),
            "referenceCurrency": q.get("currency"), "referenceProvider": q.get("provider"),
            "referenceSymbol": q.get("symbol"), "referenceScope": q.get("scope"),
            "referenceDelayMs": q.get("delayMs"), "referenceRealtime": q.get("realtime") is True,
            "referenceIdentityVerified": q.get("identityVerified") is True,
            "referenceAdjustmentVersion": q.get("adjustmentVersion"),
            "marketSession": q.get("marketSession", "unknown")}


def stock_premium(row, now=None):
    now = now if now is not None else int(time.time() * 1000)
    row = independent_stock_row(row)
    inputs = {k: row.get(k) for k in (
        "price", "quoteAt", "priceScope", "provider", "priceCurrency", "stockPrice",
        "referenceAt", "referenceCurrency", "referenceProvider", "referenceSymbol",
        "referenceFx", "quoteFx", "referenceDelayMs", "marketSession", "tokenToAssetRatio",
        "ratioSource", "ratioAt", "ratioVersion", "ratioVerified", "ratioValidUntil")}
    bad = lambda reason: unavailable(reason, inputs)
    if row.get("priceScope") not in ("dex", "exchange", "exchange-token"):
        return bad("derived-price" if row.get("priceScope") == "issuer-derived" else "unknown-market")
    if row.get("isTradingHalt") or row.get("marketSession") in ("closed", "halted"):
        return bad("market-closed")
    token_price, token_times, reason = token_usd(row)
    if reason:
        return bad(reason)
    stock_price, times, reason = reference(row, independent=True)
    if reason:
        return bad(reason)
    reason = ratio_problem(row, now)
    if reason:
        return bad(reason)
    ratio = positive(row.get("tokenToAssetRatio"))
    if row.get("referenceIdentityVerified") is not True:
        return bad("identity-unverified")
    delayed = positive(row.get("referenceDelayMs")) or 0
    market_time = (row.get("priceProvenance") or {}).get("timeKind") == "market"
    # Delayed references compare only with a captured token observation from
    # the same time. Increasing the age allowance does not relax time alignment.
    clock = timing([*token_times, *times], now, max_age=MAX_AGE + min(delayed, 1_200_000),
                   live=market_time and not row.get("quoteFx") and row.get("marketSession") in ("regular", "extended") and row.get("referenceRealtime") is True)
    if not clock.get("reason") and delayed:
        clock.update(status="delayed", realtimeUntil=None, delayMs=delayed)
    if clock.get("reason"):
        return bad(clock["reason"])
    reference_value = positive(ratio * stock_price)
    if reference_value is None:
        return bad("invalid-price")
    value = (token_price / reference_value - 1) * 100
    if not math.isfinite(value):
        return bad("invalid-price")
    return {**clock, "value": value, "inputs": inputs, "referenceValueUsd": reference_value}


def independent_quote(quote, target_pool, token, chain):
    """Opaque aggregator paths cannot prove independence just by changing provider."""
    if not quote or positive(quote.get("price")) is None:
        return "missing-independent-quote"
    if str(quote.get("chainId")) != str(chain) or str(quote.get("token", "")).lower() != token.lower():
        return "wrong-instrument"
    currency = str(quote.get("currency") or "")
    if not quote.get("provider") or not (currency == "USD" or currency.startswith(f"asset:{chain}:0x")):
        return "missing-token-currency"
    if quote.get("derived") or quote.get("scope") == "issuer-derived":
        return "derived-price"
    dependencies = quote.get("dependencies")
    if quote.get("dependenciesComplete") is not True or not isinstance(dependencies, list):
        return "unknown-price-path"
    if target_pool.lower() in {str(p).lower() for p in dependencies}:
        return "circular-price"
    return None


def pool_spread(relation, pool_quote, meme_quote, stock_quote, now=None):
    now = now if now is not None else int(time.time()*1000)
    inputs = {"pool": pool_quote, "meme": meme_quote, "stock": stock_quote}
    bad = lambda reason: unavailable(reason, inputs)
    if relation.get("status") != "verified":
        return bad("unverified-pair")
    if not pool_quote or positive(pool_quote.get("memePerStock")) is None:
        return bad("missing-pool-quote")
    chain, pool = str(relation.get("chainId")), relation.get("pool", "").lower()
    # Compare the actual stock-side instrument, including wrappers. No implicit 1:1 conversion.
    side = (relation.get("stockSide") or relation.get("stock") or "").lower()
    if (str(pool_quote.get("chainId")) != chain or pool_quote.get("pool", "").lower() != pool
            or pool_quote.get("stockSide", "").lower() != side
            or pool_quote.get("token", "").lower() != relation.get("token", "").lower()):
        return bad("wrong-instrument")
    for quote, token in ((meme_quote, relation["token"]), (stock_quote, side)):
        reason = independent_quote(quote, pool, token, chain)
        if reason:
            return bad(reason)
    if meme_quote.get("currency") != stock_quote.get("currency"):
        return bad("currency-mismatch")
    clock = timing([pool_quote.get("at"), meme_quote.get("at"), stock_quote.get("at")], now,
                   live=all(q.get("timeKind") == "market" for q in (pool_quote, meme_quote, stock_quote)))
    if clock.get("reason"):
        return bad(clock["reason"])
    implied = float(pool_quote["memePerStock"]) * float(meme_quote["price"])
    value = (implied / float(stock_quote["price"]) - 1) * 100
    if not math.isfinite(value):
        return bad("invalid-price")
    return {**clock, "value": value, "inputs": inputs, "impliedPrice": implied,
            "comparisonCurrency": meme_quote["currency"],
            "impliedPriceUsd": implied if meme_quote["currency"] == "USD" else None}


def relative_point(stock, meme, now):
    """Aligned observed USD prices for relative performance, not unrelated 24h percentages."""
    if stock.get("isTradingHalt") or stock.get("marketSession") in ("closed", "halted"):
        return unavailable("market-closed")
    price, times, reason = reference(stock)
    if reason:
        return unavailable(reason)
    meme_price, meme_times, reason = token_usd(meme)
    if reason:
        return unavailable(reason)
    if meme.get("priceScope") == "issuer-derived":
        return unavailable("derived-price")
    at = meme.get("quoteAt")
    delayed = positive(stock.get("referenceDelayMs")) or 0
    clock = timing([*meme_times, *times], now, live=False, max_age=MAX_AGE + min(delayed, 1_200_000))
    if not clock.get("reason") and delayed:
        clock.update(status="delayed", delayMs=delayed)
    if clock.get("reason"):
        return clock
    return {**clock, "stock": price, "meme": meme_price,
            "inputs": {"stockAt": stock.get("referenceAt"), "memeAt": at,
                       "stockSource": stock.get("referenceProvider"), "memeSource": meme.get("provider"),
                       "referenceSymbol": stock.get("referenceSymbol"), "fx": stock.get("referenceFx"),
                       "adjustmentVersion": stock.get("referenceAdjustmentVersion"),
                       "memeCurrency": meme.get("priceCurrency"), "memeScope": meme.get("priceScope")}}


def relative_return(current, history, window):
    if current.get("reason"):
        return current
    if not current.get("inputs", {}).get("adjustmentVersion"):
        return unavailable("adjustment-unverified")
    target = current["at"] - window
    candidates = [p for p in history if p.get("stock") and p.get("meme") and abs(p["at"] - target) <= MAX_SKEW
                  and p.get("inputs", {}).get("referenceSymbol") == current.get("inputs", {}).get("referenceSymbol")
                  and p.get("inputs", {}).get("stockSource") == current.get("inputs", {}).get("stockSource")
                  and p.get("inputs", {}).get("adjustmentVersion") == current.get("inputs", {}).get("adjustmentVersion")
                  and p.get("inputs", {}).get("memeSource") == current.get("inputs", {}).get("memeSource")
                  and p.get("inputs", {}).get("memeCurrency") == current.get("inputs", {}).get("memeCurrency")
                  and p.get("inputs", {}).get("memeScope") == current.get("inputs", {}).get("memeScope")]
    if not candidates:
        return unavailable("insufficient-history")
    start = min(candidates, key=lambda p: abs(p["at"]-target))
    sr, mr = (current["stock"]/start["stock"]-1)*100, (current["meme"]/start["meme"]-1)*100
    if not all(math.isfinite(v) for v in (sr, mr, mr-sr)):
        return unavailable("invalid-price")
    return {**{k: current[k] for k in ("at", "validUntil", "status", "method")},
            "value": mr-sr, "stockReturn": sr, "memeReturn": mr, "unit": "pp", "reason": None,
            "from": start["at"], "to": current["at"], "windowMs": window,
            "inputs": {"start": start, "end": current}}
