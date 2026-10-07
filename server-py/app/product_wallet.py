"""Read-only public wallets. Profile requests never persist an address."""
from __future__ import annotations
import asyncio
from decimal import Decimal, InvalidOperation
import json
import math
import os
import re
import time
from urllib.parse import urlsplit
import httpx
from .developer_access import AccessError
from .realtime_projection import read_projection_json

ADDRESS = re.compile(r'^0x[a-fA-F0-9]{40}$')
CHAINS = {'196': 'X Layer', '56': 'BNB Chain', '4663': 'Robinhood', '5042': 'Arc'}
CHAIN_SEMAPHORE = asyncio.Semaphore(3)
MAX_PAGES = 5


def normalize_address(address):
    if not isinstance(address, str) or not ADDRESS.fullmatch(address):
        raise AccessError('invalid-wallet-address')
    return address.lower()


def sources():
    result = {}
    for chain, label in CHAINS.items():
        base = os.environ.get('BLOCKSCOUT_BASE_'+chain, '').strip().rstrip('/')
        parts = urlsplit(base)
        valid = bool(parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password and not parts.query and not parts.fragment)
        result[chain] = {'available': valid, 'label': label, 'base': base if valid else None,
                         'reason': None if valid else 'chain-not-covered'}
    return result


def capabilities():
    return {'available': any(source['available'] for source in sources().values()),
            'chains': [{'chainId': cid, 'label': s['label'], 'available': s['available'], 'reason': s['reason']} for cid, s in sources().items()],
            'sessionOnly': True, 'signingRequired': False, 'maxPagesPerChain': MAX_PAGES}


async def fetch_balances(chain, address, client):
    source = sources().get(chain) or {}
    if not source.get('available'):
        return {'chainId': chain, 'status': 'not-covered', 'items': []}
    items, params = [], {}
    key = os.environ.get('BLOCKSCOUT_API_KEY', '').strip()
    if key:
        params['apikey'] = key
    try:
        async with CHAIN_SEMAPHORE:
            for page in range(MAX_PAGES):
                response = await client.get(source['base']+'/api/v2/addresses/'+address+'/token-balances', params=params)
                response.raise_for_status()
                packet = response.json()
                values = packet if isinstance(packet, list) else packet.get('items') if isinstance(packet, dict) else None
                if not isinstance(values, list) or len(values) > 10_000:
                    raise ValueError('malformed-balances')
                items.extend(values)
                next_page = packet.get('next_page_params') if isinstance(packet, dict) else None
                if not next_page:
                    return {'chainId': chain, 'status': 'complete', 'items': items, 'source': source['base']}
                if not isinstance(next_page, dict) or len(next_page) > 20:
                    raise ValueError('malformed-pagination')
                params = {str(k): str(v) for k, v in next_page.items() if isinstance(v, (str, int))}
                if key:
                    params['apikey'] = key
            return {'chainId': chain, 'status': 'partial', 'reason': 'pagination-limit', 'items': items, 'source': source['base']}
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return {'chainId': chain, 'status': 'unavailable', 'reason': 'source-unavailable', 'items': []}


def _quantity(item):
    token = item.get('token') or {}
    if str(token.get('type') or 'ERC-20').upper() not in {'ERC-20', 'ERC20'}:
        return None
    try:
        decimals = int(token.get('decimals'))
        if not 0 <= decimals <= 36 or isinstance(token.get('decimals'), bool):
            return None
        value = Decimal(str(item['value'])) / Decimal(10)**decimals
        return value if value.is_finite() and value > 0 else None
    except (ValueError, TypeError, InvalidOperation, KeyError):
        return None


def _price(asset, now):
    value = asset.get('price')
    at = (asset.get('fieldTimes') or {}).get('price') or asset.get('priceAt')
    state = (asset.get('fieldStates') or {}).get('price')
    valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
    if state and state not in {'current', 'fresh', 'ready', 'available'}:
        return None
    if not valid or asset.get('priceCurrency') != 'USD' or not isinstance(at, (int, float)) or not 0 <= now-at <= 180_000:
        return None
    return Decimal(str(value))


def build_profile(address, chains, payload, now):
    unified = payload.get('unified') or payload
    assets = {(str(a.get('chainId')), str(a.get('token') or a.get('tokenContractAddress') or a.get('address')).lower()): a for a in unified.get('assets') or []}
    stocks = {(str(a.get('chainId')), str(a.get('token') or a.get('tokenContractAddress') or a.get('address')).lower()): a for a in unified.get('stockTokens') or []}
    relations, theme_universe = {}, {str(s.get('stockCode') or s.get('ticker')) for s in stocks.values() if (s.get('verificationStatus') == 'official' or (s.get('issuerIdentity') or {}).get('verificationStatus') == 'official') and (s.get('stockCode') or s.get('ticker'))}
    for relation in unified.get('relations') or []:
        ticker = str((relation.get('stockIdentity') or {}).get('ticker') or relation.get('ticker') or '')
        if relation.get('level') == 'A' and relation.get('status') == 'verified' and ticker:
            theme_universe.add(ticker)
            relations.setdefault((str(relation.get('chainId')), str(relation.get('token')).lower()), []).append(relation)
    holdings, unvalued, seen_holdings = [], [], set()
    for chain in chains:
        for item in chain['items']:
            if not isinstance(item, dict):
                continue
            token = item.get('token') or {}
            address_token = str(token.get('address_hash') or token.get('address') or '').lower()
            if not ADDRESS.fullmatch(address_token):
                continue
            holding_key = chain['chainId'], address_token
            if holding_key in seen_holdings:
                continue
            seen_holdings.add(holding_key)
            quantity = _quantity(item)
            if quantity is None:
                unvalued.append({'chainId': chain['chainId'], 'token': address_token, 'symbol': token.get('symbol'), 'quantity': None, 'reason': 'quantity-not-readable'})
                continue
            key = chain['chainId'], address_token
            asset = assets.get(key) or stocks.get(key) or {}
            price = _price(asset, now)
            row = {'chainId': chain['chainId'], 'token': address_token, 'symbol': asset.get('symbol') or asset.get('tokenSymbol') or token.get('symbol'),
                   'name': asset.get('name') or asset.get('tokenName') or token.get('name'), 'quantity': str(quantity), 'source': chain.get('source'),
                   'explorerUrl': (chain.get('source') or '')+'/address/'+address_token}
            if price is None:
                unvalued.append({**row, 'reason': 'station-price-unavailable'}); continue
            value = float(quantity*price)
            if not math.isfinite(value):
                unvalued.append({**row, 'reason': 'value-out-of-range'}); continue
            rels = relations.get(key, [])
            tickers = sorted({str((r.get('stockIdentity') or {}).get('ticker') or r.get('ticker')) for r in rels})
            stock_ticker = str((stocks.get(key) or {}).get('stockCode') or (stocks.get(key) or {}).get('ticker') or '')
            if stock_ticker and stock_ticker in theme_universe and stock_ticker not in tickers:
                tickers.append(stock_ticker)
            from .product_metrics import v2_exit_impact
            impact = v2_exit_impact(max(rels, key=lambda r: r.get('liquidityUsd') or 0), value, now) if rels else {'status': 'unsupported', 'valuePercent': None}
            safety = (asset.get('riskAssessment') or {}).get('safety') or {}
            risk = any(r.get('status') == 'triggered' for r in safety.values())
            # Unknown risk data is retained separately; it does not mean safe.
            unknown_risk = not safety or any(r.get('status') not in {'clear', 'triggered'} for r in safety.values())
            holdings.append({**row, 'valueUsd': value, 'priceUsd': float(price), 'stockToken': key in stocks and ((stocks[key].get('verificationStatus') == 'official') or ((stocks[key].get('issuerIdentity') or {}).get('verificationStatus') == 'official')),
                             'themes': tickers, 'meme': bool(rels) and key not in stocks, 'exitImpact': impact,
                             'riskFlag': risk, 'riskUnknown': unknown_risk})
    holdings.sort(key=lambda row: row['valueUsd'], reverse=True)
    total = sum(r['valueUsd'] for r in holdings)
    themes, by_chain = {}, {}
    for row in holdings:
        row['share'] = row['valueUsd']/total if total else None
        by_chain[row['chainId']] = by_chain.get(row['chainId'], 0)+row['valueUsd']
        # Multi-theme holdings are split equally to avoid double counting HHI.
        for ticker in row['themes']:
            themes[ticker] = themes.get(ticker, 0)+row['valueUsd']/len(row['themes'])
    ratio = lambda value: value/total if total else None
    stock_rows, meme_rows = [r for r in holdings if r['stockToken']], [r for r in holdings if r['meme']]
    risk_rows = [r for r in holdings if r['riskFlag']]
    metrics = {'totalValueUsd': total if holdings else None, 'valuedCount': len(holdings), 'unvaluedCount': len(unvalued),
               'stockCount': len(stock_rows), 'stockShare': ratio(sum(r['valueUsd'] for r in stock_rows)),
               'memeCount': len(meme_rows), 'memeShare': ratio(sum(r['valueUsd'] for r in meme_rows)),
               'themeCoverage': len(themes), 'effectiveThemeCount': len(theme_universe),
               'themeHHI': sum((v/total)**2 for v in themes.values()) if total and themes else None,
               'maxHoldingShare': holdings[0]['share'] if holdings else None,
               'effectiveHoldingCount': sum(r['valueUsd'] >= 10 for r in holdings),
               'riskCount': len(risk_rows), 'riskValueShare': ratio(sum(r['valueUsd'] for r in risk_rows)),
               'riskUnknownCount': sum(r['riskUnknown'] for r in holdings)}
    summary = []
    if total:
        summary.append(f'站内可估值持仓 ${total:,.2f}，覆盖 {len(themes)} 个股票主题。')
        if holdings:
            first = holdings[0]
            summary.append(f"{first['symbol'] or first['token'][:8]} 占 {first['share']:.1%}。")
        summary.append(f'{len(risk_rows)} 个持仓有风险提示；{metrics["riskUnknownCount"]} 个风险数据不完整。')
    return {'address': address, 'at': now, 'sessionOnly': True, 'saved': False, 'summary': summary,
            'holdings': holdings, 'unvalued': unvalued, 'metrics': metrics,
            'themes': [{'ticker': k, 'valueUsd': v, 'share': ratio(v)} for k, v in sorted(themes.items(), key=lambda item: -item[1])],
            'chains': [{'chainId': c['chainId'], 'label': CHAINS[c['chainId']], 'status': c['status'], 'reason': c.get('reason'),
                        'valueUsd': by_chain.get(c['chainId']) if c['status'] in {'complete', 'partial'} else None,
                        'share': ratio(by_chain.get(c['chainId'], 0)) if c['status'] in {'complete', 'partial'} and total else None} for c in chains],
            'valuationScope': 'station-current-usd-prices', 'incomplete': bool(unvalued or any(c['status'] != 'complete' for c in chains)),
            'hhiMethod': 'theme-value/total-valued; multi-theme value split equally'}


async def wallet_profile(address):
    address = normalize_address(address)
    now = int(time.time()*1000)
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        chains = await asyncio.gather(*(fetch_balances(chain, address, client) for chain in CHAINS))
    payload = json.loads(await read_projection_json('full'))
    return build_profile(address, chains, payload, now)
