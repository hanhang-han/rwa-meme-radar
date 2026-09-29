"""Scheduled bilingual market briefing from typed dashboard facts."""
import asyncio
import hashlib
import json
import os
import math
import time
from datetime import datetime, timezone

from .db import store
from .realtime_projection import read_projection

STREAK_FILE = "data/ai-streaks.json"
CHAIN_NAMES = {'196': 'X Layer', '56': 'BNB Smart Chain', '4663': 'Robinhood Chain'}
PROMPT_VERSION = 3

def _load_streaks() -> dict:
    try:
        return json.load(open(STREAK_FILE))
    except Exception:
        return {}


def _save_streaks(hist: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STREAK_FILE), exist_ok=True)
        json.dump(hist, open(STREAK_FILE, "w"), ensure_ascii=False)
    except Exception:
        pass


def streak_of(hist: dict, asset_id: str, current: list[str]) -> int:
    def has(i: int) -> bool:
        d = datetime.fromtimestamp(time.time() - i * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
        return asset_id in (hist.get(d) or []) or (i == 0 and asset_id in current)

    n = 0
    for i in range(15):
        if has(i):
            n += 1
        elif i != 0:
            break
    return n


def update_streaks(asset_ids: list[str]) -> None:
    hist = _load_streaks()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hist[today] = asset_ids
    cutoff = datetime.now(timezone.utc).timestamp() - 15 * 86400
    for d in list(hist):
        try:
            if datetime.fromisoformat(d).timestamp() < cutoff:
                del hist[d]
        except ValueError:
            del hist[d]
    _save_streaks(hist)


def _num(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _asset_id(asset):
    chain, address = str(asset.get('chainId') or ''), str(asset.get('token') or '').lower()
    if chain not in CHAIN_NAMES or not (address.startswith('0x') and len(address) == 42):
        return None
    return f'{chain}:{address}'


def _money(value):
    number = _num(value)
    if number is None:
        return '—'
    if abs(number) >= 1_000_000_000:
        return f'${number / 1_000_000_000:.2f}B'
    if abs(number) >= 1_000_000:
        return f'${number / 1_000_000:.2f}M'
    if abs(number) >= 1_000:
        return f'${number / 1_000:.1f}K'
    return f'${number:.2f}'


def briefing_items(unified, snapshot_id):
    """Select facts with exact identity and risk evidence from one snapshot.

    The legacy relation status only proves pool structure. A-level eligibility
    is required before a relation can be described as a stock pairing.
    """
    a_tokens = {(str(r.get('chainId')), str(r.get('token') or '').lower())
                for r in unified.get('relations', []) if r.get('level') == 'A'}
    candidates = []
    for asset in unified.get('assets', []):
        identity = _asset_id(asset)
        if not identity or (str(asset.get('chainId')), str(asset.get('token')).lower()) not in a_tokens:
            continue
        if not ((asset.get('dataQuality') or {}).get('eligible') or {}).get('marketRanking'):
            continue
        volume = _num(asset.get('volume24h'))
        liquidity = _num(asset.get('totalLiquidityUsd'))
        change = _num(asset.get('change24h'))
        risk_flags = set(asset.get('riskFlags') or [])
        if volume is None:
            continue
        risk_checks = (asset.get('riskAssessment') or {}).get('checks') or {}
        risk_type = next((flag for flag in ('wash_suspect', 'thin_spike') if flag in risk_flags), None)
        fields = {'volume24hUsd': volume, 'totalLiquidityUsd': liquidity,
                  'change24hPercent': change}
        if risk_type == 'wash_suspect':
            evidence = (risk_checks.get('wash_suspect') or {}).get('evidence') or {}
            fields['volumeLiquidityRatio'] = _num(evidence.get('volumeLiquidityRatio'))
            fields['transactionsPerHolder'] = _num(evidence.get('transactionsPerHolder'))
        row = {
            'id': f'{risk_type or "market_activity"}:{identity}',
            'type': risk_type or 'market_activity',
            'asset': {'chainId': str(asset['chainId']), 'address': str(asset['token']).lower(),
                      'name': asset.get('name') or asset.get('symbol') or str(asset['token'])[:8],
                      'symbol': asset.get('symbol') or '',
                      'chain': CHAIN_NAMES[str(asset['chainId'])]},
            'fields': fields, 'snapshotId': snapshot_id,
        }
        score = (1_000_000_000 if risk_type else 0) + volume
        candidates.append((score, row))
    candidates.sort(key=lambda pair: -pair[0])
    items, seen = [], set()
    for _, row in candidates:
        identity = f"{row['asset']['chainId']}:{row['asset']['address']}"
        if identity in seen:
            continue
        seen.add(identity)
        items.append(row)
        if len(items) >= 3:
            break
    return items


def render_briefing(items, lang):
    if not items:
        return 'No qualifying changes in this snapshot.' if lang == 'en' else '当前快照暂无符合条件的异动。'
    lines = []
    for item in items:
        asset, fields = item['asset'], item['fields']
        name = asset['name'] if lang == 'zh' else (asset['symbol'] or asset['address'][:8])
        if lang == 'en' and any(ord(char) > 127 for char in str(name)):
            name = asset['address'][:8] + '…' + asset['address'][-4:]
        name = f"{name} ({asset['chain']})"
        volume = _money(fields['volume24hUsd'])
        liquidity_value = fields.get('totalLiquidityUsd')
        liquidity = _money(liquidity_value) if liquidity_value is not None else None
        if item['type'] == 'wash_suspect':
            ratio = fields.get('volumeLiquidityRatio')
            tx_ratio = fields.get('transactionsPerHolder')
            if ratio is not None:
                line = (f'{name}: 24h volume {volume}, {ratio:.0f}x comparable liquidity; unusual turnover.'
                        if lang == 'en' else f'{name}：24h 成交 {volume}，为同口径流动性的 {ratio:.0f} 倍；周转异常待核验。')
            elif tx_ratio is not None:
                line = (f'{name}: {tx_ratio:.0f} transactions per holder in 24h; unusual activity.'
                        if lang == 'en' else f'{name}：24h 成交笔数是持币地址数的 {tx_ratio:.0f} 倍；活动异常待核验。')
            else:
                line = (f'{name}: unusual turnover flag; review the risk evidence.'
                        if lang == 'en' else f'{name}：出现异常周转标记，请查看风险依据。')
        elif item['type'] == 'thin_spike' and fields.get('change24hPercent') is not None and liquidity is not None:
            change = fields['change24hPercent']
            line = (f'{name}: 24h change {change:+.1f}%, liquidity {liquidity}; thin-market spike.'
                    if lang == 'en' else f'{name}：24h 涨跌 {change:+.1f}%，流动性 {liquidity}；低深度波动。')
        elif liquidity is not None:
            line = (f'{name}: 24h volume {volume}, observed liquidity {liquidity}.'
                    if lang == 'en' else f'{name}：24h 成交 {volume}，已观测流动性 {liquidity}。')
        else:
            line = (f'{name}: 24h volume {volume}; total liquidity is unavailable.'
                    if lang == 'en' else f'{name}：24h 成交 {volume}；总流动性暂无可核对数据。')
        lines.append(line)
    return '\n'.join(lines)


async def briefing_base() -> dict:
    # The published dashboard row is the atomic source for both page numbers
    # and briefing items. A page read never starts a new generation.
    snapshot = await read_projection()
    u = snapshot["unified"]
    data_as_of = snapshot.get('now')
    realtime = snapshot.get('realtime') or {}
    snapshot_id = f"{realtime.get('revision', 'unknown')}:{realtime.get('cursor', 'unknown')}"
    day_ago = time.time() * 1000 - 86_400_000
    verified_relations = [r for r in u["relations"] if r.get("level") == "A"]
    verified_tokens = {(str(r.get("chainId") or "196"), str(r["token"]).lower()) for r in verified_relations}

    new_verified_raw = [
        {"ticker": r.get("ticker"), "chain": CHAIN_NAMES.get(str(r.get('chainId')), 'Unknown'),
         "poolLiquidityUsd": r.get("liquidityUsd") if (r.get("liquidityUsd") or 0) > 0 else None}
        for r in verified_relations if (r.get("firstSeen") or 0) >= day_ago
    ][:8]

    movers_candidates = [
        a for a in u["assets"]
        if (str(a.get("chainId") or "196"), str(a.get("token") or "").lower()) in verified_tokens and a.get("change24h") is not None
    ]
    try:
        movers_candidates.sort(key=lambda a: -abs(float(a["change24h"])))
    except (TypeError, ValueError):
        pass
    mover_ids = [identity for a in movers_candidates[:4] if (identity := _asset_id(a))]
    hist = _load_streaks()
    movers = [
        {
            "symbol": a.get("symbol"), "chain": CHAIN_NAMES.get(str(a.get('chainId')), 'Unknown'),
            "change24hPercent": a.get("change24h"), "volume24hUsd": a.get("volume24h"),
            "liquidityUsd": a.get("liquidity"),
            "streakDays": streak_of(hist, _asset_id(a), mover_ids),
            "priceAnomalySuspected": float(a.get("change24h") or 0) <= -50,
            "note": "跌幅远超正股市场正常范围，价格数据待核查" if float(a.get("change24h") or 0) <= -50 else None,
        }
        for a in movers_candidates[:4] if _asset_id(a)
    ]

    premiums = [
        {"symbol": s.get("tokenSymbol") or s.get("stockCode"), "chain": CHAIN_NAMES.get(str(s.get('chainId')), 'Unknown'),
         "premiumPercent": s["premium"]["value"], "referenceValueUsd": s["premium"].get("referenceValueUsd"),
         "quoteStatus": s["premium"].get("status")}
        for s in u["stockTokens"] if isinstance(s.get("premium"), dict) and s["premium"].get("value") is not None
    ]
    try:
        premiums.sort(key=lambda x: -abs(float(x["premiumPercent"])))
    except (TypeError, ValueError):
        pass
    premiums = premiums[:4]

    name_leads = [
        r for r in u["relations"]
        if r.get("status") != "verified" and (r.get("firstSeen") or 0) >= day_ago and r.get("ticker")
    ]
    by_ticker = {}
    for relation in name_leads:
        ticker = str(relation["ticker"])
        by_ticker[ticker] = by_ticker.get(ticker, 0) + 1
    m = u["metrics"]
    return {
        "metrics": {
            "verifiedPools": m.get("verifiedPools"),
            "verifiedAssets": m.get("verifiedAssets"),
            "newRelationRecords24h": m.get("newRelations24h"),
            "newRelationRecordsNote": "含未核验的名称线索记录，不等于已核验关系",
        },
        "distribution": u["distribution"],
        "newNameLeads24h": {
            "total": len(name_leads),
            "byTicker": [
                {"ticker": ticker, "count": count}
                for ticker, count in sorted(by_ticker.items(), key=lambda item: (-item[1], item[0]))[:10]
            ],
            "status": "available",
        },
        "newVerifiedPairs24h": new_verified_raw,
        "verifiedAssetMovers24h": movers,
        "moverIds": mover_ids,
        "crossMarketPremiumTop": premiums,
        "dataAsOf": data_as_of,
        "snapshotId": snapshot_id,
        "items": briefing_items(u, snapshot_id),
        "coverage": {
            "assets": len(u["assets"]),
            "relations": len(u["relations"]),
            "verifiedRelations": len(verified_relations),
        },
    }


def briefing_for(base: dict, lang: str) -> dict:
    # Kept as a compatibility adapter for callers that expected a language
    # projection. No model receives this input after promptVersion 3.
    return base


INTERVAL_MS = 60_000
_refresh_lock = asyncio.Lock()
_generating: set[str] = set()
_errors: set[str] = set()
_retry_after: dict[str, float] = {}


async def refresh_briefing() -> None:
    # Only the scheduler generates. Reads and language switches never call AI.
    if _refresh_lock.locked():
        return
    async with _refresh_lock:
        db = await store("ai")
        now = time.time() * 1000
        published = await db.get("briefing", "latest")
        previous = {lang: published.get(lang) if isinstance(published, dict) else None
                    for lang in ("zh", "en")}
        need_refresh = any(not saved or saved.get('promptVersion') != PROMPT_VERSION
                           or now - saved.get('at', 0) >= INTERVAL_MS
                           for saved in previous.values())
        if not need_refresh or any(now < _retry_after.get(lang, 0) for lang in ('zh', 'en')):
            return
        due = ('zh', 'en')
        try:
            base = await briefing_base()
        except Exception as exc:
            for lang in due:
                _errors.add(lang)
                _retry_after[lang] = now + 300_000
            await db.put('briefing-job', 'latest', {
                'status': 'generation_failed', 'reason': type(exc).__name__,
                'failedAt': int(now), 'nextRetryAt': int(now + 300_000),
            })
            return
        if not (base.get("coverage") or {"assets": 1}).get("assets"):
            for lang in due:
                _errors.add(lang)
                _retry_after[lang] = now + 300_000
            await db.put("briefing-job", "latest", {
                "status": "no_data", "failedAt": int(now), "nextRetryAt": int(now + 300_000)
            })
            return
        input_json = json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        input_hash = hashlib.sha256(input_json.encode()).hexdigest()
        generation_id = f"{int(now)}:{input_hash[:12]}"
        generated = {}
        for lang in due:
            _generating.add(lang)
            try:
                generated[lang] = {
                    "text": render_briefing(base['items'], lang),
                    "items": base['items'],
                    "snapshotId": base['snapshotId'],
                    "at": int(now), "lang": lang,
                    "generationId": generation_id, "inputHash": input_hash,
                    "dataAsOf": base.get("dataAsOf"), "coverage": base.get("coverage"),
                    "promptVersion": PROMPT_VERSION, "model": "template-v3",
                }
                _errors.discard(lang)
                _retry_after.pop(lang, None)
            except Exception:
                _errors.add(lang)
                _retry_after[lang] = time.time() * 1000 + 300_000
            finally:
                _generating.discard(lang)
        # One fact is the publication boundary. Separate per-language puts can
        # expose different generations if a reader arrives between commits or
        # the worker stops after the first commit.
        if len(generated) == len(due):
            await db.put("briefing", "latest", generated)
            await db.put("briefing-job", "latest", {
                "status": "success", "generationId": generation_id,
                "inputHash": input_hash, "dataAsOf": base.get("dataAsOf"),
                "finishedAt": int(time.time() * 1000),
            })
            update_streaks(base["moverIds"])
        else:
            await db.put("briefing-job", "latest", {
                "status": "upstream_failed", "generationId": generation_id,
                "failedAt": int(time.time() * 1000),
                "nextRetryAt": int(min((_retry_after.get(x, time.time() * 1000 + 300_000) for x in due))),
                "errors": {x: "generation_failed" for x in due},
            })


async def briefing_endpoint(lang: str) -> dict:
    # Persisted last success remains readable during generation and after restart.
    lang = "en" if lang == "en" else "zh"
    db = await store("ai")
    published = await db.get("briefing", "latest")
    if isinstance(published, dict):
        saved = published.get(lang)
    else:
        # Read pre-migration rows until the next scheduled generation publishes
        # the bilingual fact. Reads still never trigger generation.
        saved = await db.get("briefing", lang)
    status = ("generating" if lang in _generating else
              "upstream_failed" if lang in _errors else "scheduled")
    if saved:
        return {**saved, "cached": True, "status": status,
                "stale": time.time() * 1000 - saved["at"] >= INTERVAL_MS,
                "refreshIntervalMs": INTERVAL_MS}
    return {"text": None, "status": status, "reason": status,
            "refreshIntervalMs": INTERVAL_MS}
