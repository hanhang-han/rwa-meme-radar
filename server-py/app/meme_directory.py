"""Query-scoped market reads; selectors retain the existing browser semantics.

The complete committed catalogue is evaluated before paging symbol groups.
Only representatives, requested members and compact chart observations cross
the wire. Supplier calls and mutations do not belong to these reads.
"""
import json
import math
import time

from .dashboard_projection import _market_asset

MEME_SORTS = {'volume24h', 'price', 'change24h', 'totalLiquidityUsd', 'holders', 'firstSeen'}
POOL_SORTS = {'liquidity', 'volume24h', 'createdAt'}
KNOWN_RISKS = {'wash_suspect', 'thin_spike', 'contract_risk', 'concentrated', 'holder_anomaly', 'liquidity_unlock'}
INLINE_CHART_POINTS = 128


def numeric(value):
    if value is None or value == '':
        return None
    try:
        result = float(value) if not isinstance(value, str) or value.strip() else 0.0
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def recent(at, window, now):
    value = numeric(at)
    return value is not None and value > 0 and 0 <= now-value <= window


def first(*values):
    return next((value for value in values if value is not None), None)


def asset_key(row):
    return str(first(row.get('chainId'), row.get('chain'), '')) + ':' + str(first(row.get('token'), row.get('tokenContractAddress'), '')).lower()


def ticker(value):
    value = str(value or '').strip().upper()
    return value.lstrip('0') or '0' if value.isdigit() else value


def normalize_query(query):
    """Preserve URL defaults, including the legacy filter aliases."""
    result = {key: str(value) for key, value in query.items() if value is not None}
    chain = result.get('chain', 'all').lower()
    result['chain'] = {'xlayer': '196', 'bnb': '56', 'bsc': '56', 'robinhood': '4663'}.get(chain, chain)
    if result['chain'] not in {'all', '196', '56', '4663'}:
        result['chain'] = 'all'
    result['q'] = result.get('q', '').strip().lower()
    result['ticker'] = ticker(result.get('ticker'))
    result['sort'] = result.get('sort') or 'volume24h'
    qualified = result.get('qualified') == '1'
    legacy = result.get('filter', '')
    result['rel'] = 'all' if qualified else result.get('rel', 'all' if result.get('new') == '24h' or result.get('rank') == 'volume24h' else {'name': 'B', 'verified': 'A', 'related': 'A'}.get(legacy, 'all'))
    result['fresh'] = '1' if qualified else result.get('fresh', '1' if legacy == 'related' else '0')
    result['minLiq'] = '1000' if qualified else result.get('minLiq', result.get('minLiquidity', '0'))
    result['showMissing'] = result.get('showMissing', '0' if qualified else '1')
    return {key: result.get(key, '') for key in ('chain', 'q', 'ticker', 'sort', 'rel', 'fresh', 'minLiq', 'showMissing', 'qualified', 'filter', 'risk', 'category', 'new', 'rank')}


def basic_market(row):
    price, volume, liquidity = (numeric(row.get(field)) for field in ('price', 'volume24h', 'totalLiquidityUsd'))
    return price is not None and price > 0 and volume is not None and volume >= 0 and liquidity is not None and liquidity > 0


def qualified_meme(row, now):
    price, liquidity = numeric(row.get('price')), numeric(row.get('totalLiquidityUsd'))
    return (row.get('kind') == 'candidate' and bool(row.get('token')) and price is not None and price > 0
            and recent((row.get('fieldTimes') or {}).get('price'), 900_000, now)
            and liquidity is not None and liquidity >= 1000 and row.get('totalLiquidityStatus') == 'current'
            and recent(row.get('totalLiquidityAt'), 1_800_000, now))


def current_liquidity(row, now):
    value = numeric(row.get('totalLiquidityUsd'))
    at = first(row.get('totalLiquidityAt'), (row.get('fieldTimes') or {}).get('totalLiquidityUsd'))
    return value if value is not None and row.get('totalLiquidityStatus') == 'current' and recent(at, 1_800_000, now) else None


def comparable_volume(row, now):
    value = numeric(row.get('volume24h'))
    scope = first((row.get('fieldScopes') or {}).get('volume24h'), row.get('volumeScope'))
    currency = first(row.get('volumeCurrency'), row.get('priceCurrency'))
    return (value if value is not None and value >= 0 and currency == 'USD' and scope in ('token', 'token-aggregate')
            and recent((row.get('fieldTimes') or {}).get('volume24h'), 900_000, now) else None)


def _level(row):
    value = row.get('level')
    return value if value in ('A', 'B', 'C') else None


def _sort_value(row, field):
    return -math.inf if row.get(field) is None else numeric(row.get(field)) or 0


def _text_order(value):
    # Addresses are ASCII; folded symbols retain the browser's case-insensitive
    # primary comparison rather than making uppercase addresses a new identity.
    value = str(value or '')
    return value.casefold(), value


def select_memes(payload, query, now=None):
    now = int(time.time()*1000) if now is None else now
    query = normalize_query(query)
    source = payload.get('unified') or {}
    unique = {}
    for row in source.get('assets', []):
        unique[asset_key(row)] = row
    catalog = [row for row in unique.values() if row.get('kind') == 'candidate'
               and (query['chain'] == 'all' or str(row.get('chainId')) == query['chain'])]
    relations = {}
    for row in source.get('relations', []):
        relations.setdefault(asset_key(row), []).append(row)
    selected_levels = set(query['rel'].split(','))
    floor = numeric(query['minLiq']) or 0
    qualified = query['qualified'] == '1'
    matched = []
    for row in catalog:
        if query['category'] == 'derivative' and row.get('assetCategory') != 'derivative':
            continue
        if (query['category'] == 'meme' or query['rank'] == 'volume24h') and row.get('assetCategory') == 'derivative':
            continue
        if query['rank'] == 'volume24h' and comparable_volume(row, now) is None:
            continue
        if query['new'] == '24h' and not recent(row.get('firstSeen'), 86_400_000, now):
            continue
        related = relations.get(asset_key(row), [])
        if query['ticker'] and not any(ticker(relation.get('ticker')) == query['ticker'] and _level(relation) == 'A' for relation in related):
            continue
        if query['q'] and not any(query['q'] in str(value or '').lower() for value in
                                   [row.get('name'), row.get('symbol'), row.get('token'),
                                    *(relation.get('ticker') for relation in related), (row.get('match') or {}).get('ticker')]):
            continue
        if qualified:
            if qualified_meme(row, now):
                matched.append(row)
            continue
        levels = {_level(relation) for relation in related} - {None}
        if row.get('relationLevel'):
            levels.add(row['relationLevel'])
        if query['rel'] != 'all' and not levels.intersection(selected_levels):
            continue
        liquidity = current_liquidity(row, now)
        if floor and (liquidity is None or liquidity < floor):
            continue
        if query['risk'] == 'hide' and KNOWN_RISKS.intersection(row.get('riskFlags') or []):
            continue
        if query['filter'] == 'history' and (row.get('dataQuality') or {}).get('tier') != 'historical':
            continue
        matched.append(row)
    assets = []
    for row in matched:
        if qualified:
            assets.append(row)
            continue
        price = numeric(row.get('price'))
        at = first((row.get('fieldTimes') or {}).get('price'), row.get('quoteAt'))
        if query['fresh'] == '1' and not (price is not None and price > 0 and recent(at, 900_000, now)):
            continue
        if query['showMissing'] != '1' and not basic_market(row):
            continue
        assets.append(row)
    by_symbol = {}
    for row in assets:
        symbol = str(row.get('symbol') or row.get('name') or row.get('token')).upper()
        by_symbol.setdefault(symbol, []).append(row)
    groups = []
    for symbol, members in by_symbol.items():
        members = sorted(members, key=lambda row: (-_sort_value(row, query['sort']), _text_order(row.get('token'))))
        groups.append({'symbol': symbol, 'members': members})
    groups.sort(key=lambda group: (-_sort_value(group['members'][0], query['sort']), _text_order(group['symbol'])))
    group_positions = {asset_key(row): (index, group['symbol']) for index, group in enumerate(groups) for row in group['members']}
    points = []
    volume_max = liquidity_max = 0
    for row in assets:
        volume, liquidity = comparable_volume(row, now), current_liquidity(row, now)
        volume_max = max(volume_max, volume or 0)
        liquidity_max = max(liquidity_max, liquidity or 0)
        if volume is None or liquidity is None or liquidity < 0:
            continue
        group_index, symbol = group_positions[asset_key(row)]
        points.append({'key': asset_key(row), 'chainId': str(row.get('chainId')), 'token': row.get('token'),
                       'symbol': row.get('symbol'), 'name': row.get('name'), 'volume': volume, 'liquidity': liquidity,
                       'change24h': row.get('change24h'), 'changeAt': (row.get('fieldTimes') or {}).get('change24h'),
                       'volumeAt': (row.get('fieldTimes') or {}).get('volume24h'),
                       'liquidityAt': first(row.get('totalLiquidityAt'), (row.get('fieldTimes') or {}).get('totalLiquidityUsd')),
                       'groupSymbol': symbol, 'groupIndex': group_index})
    summary = {'comparableCount': len(points), 'observedVolumeTotal': sum(point['volume'] for point in points),
               'volumeMax': volume_max, 'liquidityMax': liquidity_max,
               'plotVolumeMax': max((point['volume'] for point in points), default=0),
               'plotLiquidityMax': max((point['liquidity'] for point in points), default=0)}
    return {'payload': payload, 'query': query, 'evaluatedAt': now, 'groups': groups, 'relations': relations,
            'points': points, 'summary': summary, 'catalogTotal': len(catalog), 'filteredTotal': len(assets),
            'hiddenMissingCount': sum(not basic_market(row) for row in matched)}


def envelope(payload, evaluated_at):
    return {'now': payload.get('now'), 'snapshotAt': payload.get('now'), 'evaluatedAt': evaluated_at,
            'realtime': dict(payload.get('realtime') or {})}


def list_asset(row, symbol, index, count, member_index=0):
    out = _market_asset(row)
    for field in ('primaryQuote', 'marketQuotes', 'exchangeMarkets', 'quoteAlternatives'):
        out.pop(field, None)
    metric_fields = {'value', 'status', 'reason', 'at', 'source', 'provider', 'unit', 'validUntil'}
    if row.get('productMetrics'):
        metrics = row['productMetrics']
        out['productMetrics'] = {key: {field: value for field, value in metric.items() if field in metric_fields}
                                 for key in ('buyShare24h', 'volumeLiquidityRatio', 'fdvUsd')
                                 if isinstance((metric := metrics.get(key)), dict)}
        out['productMetrics']['changes'] = {key: {field: value for field, value in metric.items() if field in metric_fields}
                                           for key, metric in (metrics.get('changes') or {}).items() if isinstance(metric, dict)}
    # These fallback windows and observation maps are used by the full-column
    # renderer; never replace their true times with evaluation/publication time.
    for field in ('change5m', 'change1h', 'change6h', 'buyRatio', 'buyTransactions', 'sellTransactions', 'updatedAt'):
        if field in row:
            out[field] = row[field]
    for field in ('fieldTimes', 'fieldSources', 'fieldScopes'):
        if field in row:
            out[field] = {key: value for key, value in (row[field] or {}).items() if key in {
                'price', 'volume24h', 'change24h', 'holders', 'totalLiquidityUsd', 'buys24h', 'sells24h',
                'buyRatio', 'buyTransactions', 'sellTransactions', 'change5m', 'change1h', 'change6h'}}
    for field in ('price', 'volume24h', 'totalLiquidityUsd', 'change24h', 'holders'):
        if field in row:
            out[field] = row[field]
    out.update(groupSymbol=symbol, groupIndex=index, count=count, memberIndex=member_index)
    return out


def list_relations(selection, rows):
    fields = {'id', 'chainId', 'token', 'pool', 'stock', 'ticker', 'level', 'status', 'liquidityUsd',
              'liquidityAt', 'liquidityStatus', 'evidenceStatus', 'checkedAt', 'protocol', 'dex'}
    return [{key: value for key, value in relation.items() if key in fields}
            for row in rows for relation in selection['relations'].get(asset_key(row), [])]


def meme_page(selection, limit=20, offset=0):
    groups = selection['groups']
    # The browser previously clamped an out-of-range last page after a filter
    # or live removal. Preserve that behavior instead of showing a false empty.
    offset = min(offset, max(0, (len(groups)-1)//limit)*limit)
    chosen = groups[offset:offset+limit]
    representatives = [group['members'][0] for group in chosen]
    total = len(selection['points'])
    chart = {'points': selection['points'] if total <= INLINE_CHART_POINTS else [], 'total': total,
             'requiresFetch': total > INLINE_CHART_POINTS}
    result = {**envelope(selection['payload'], selection['evaluatedAt']),
            'groups': [{'symbol': group['symbol'], 'memberCount': len(group['members']),
                        'representative': list_asset(group['members'][0], group['symbol'], offset+index, len(group['members']))}
                       for index, group in enumerate(chosen)],
            'directory': {key: selection[key] for key in ('catalogTotal', 'filteredTotal', 'hiddenMissingCount')} |
                         {'groupTotal': len(groups), 'limit': limit, 'offset': offset, 'query': selection['query']},
            'summary': selection['summary'], 'chart': chart,
            'unified': {'snapshotScope': 'memes', 'assets': [], 'stockTokens': [],
                        'relations': list_relations(selection, representatives)}}
    if not chart['requiresFetch'] and total and len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()) > 80_000:
        result['chart'] = {'points': [], 'total': total, 'requiresFetch': True}
    return result


def meme_members(selection, symbol, limit=50, offset=0, focus=''):
    index, group = next(((index, group) for index, group in enumerate(selection['groups']) if group['symbol'] == symbol), (-1, None))
    members = group['members'] if group else []
    if focus:
        target = next((index for index, row in enumerate(members) if asset_key(row) == focus.lower()), None)
        if target is not None:
            offset = target//limit*limit
    chosen = members[offset:offset+limit]
    return {**envelope(selection['payload'], selection['evaluatedAt']),
            'group': {'symbol': symbol, 'memberCount': len(members), 'limit': limit, 'offset': offset, 'groupIndex': index},
            'rows': [list_asset(row, symbol, index, len(members), offset+member_index) for member_index, row in enumerate(chosen)],
            'unified': {'snapshotScope': 'memes', 'assets': [], 'stockTokens': [], 'relations': list_relations(selection, chosen)}}


def meme_chart(selection):
    return {**envelope(selection['payload'], selection['evaluatedAt']),
            'chart': {'points': selection['points'], 'total': len(selection['points']), 'requiresFetch': False}}


def selection_valid_until(payload, now):
    """A cached selection never crosses a freshness/future-time boundary."""
    until = now+2000
    for row in (payload.get('unified') or {}).get('assets', []):
        fields = row.get('fieldTimes') or {}
        for at, window in ((first(fields.get('price'), row.get('quoteAt')), 900_000),
                           (fields.get('price'), 900_000), (fields.get('volume24h'), 900_000),
                           (first(row.get('totalLiquidityAt'), fields.get('totalLiquidityUsd')), 1_800_000),
                           (row.get('firstSeen'), 86_400_000)):
            at = numeric(at)
            if at is None or at <= 0:
                continue
            boundary = at if at > now else at+window+1
            if boundary > now:
                until = min(until, boundary)
    return until


POOL_RELATION_FIELDS = {'id', 'chainId', 'token', 'pool', 'stock', 'stockSide', 'ticker', 'protocol', 'dex',
                        'level', 'status', 'liquidityUsd', 'liquidityAt', 'liquiditySource', 'liquidityProvider',
                        'provider', 'poolCreatedAt', 'checkedAt', 'feePct', 'feeBps', 'poolType'}


def pool_directory(payload, *, chain='all', q='', sort='liquidity', qualified=False, limit=15, offset=0, now=None):
    """The existing pool selector: page pair groups, never borrow token volume."""
    now = int(time.time()*1000) if now is None else now
    source = payload.get('unified') or {}
    assets = {asset_key(row): row for row in source.get('assets', [])}
    pool_index = {}
    for relation in source.get('relations', []):
        if relation.get('level') != 'A' or relation.get('status') != 'verified' or not relation.get('pool'):
            continue
        chain_id = str(first(relation.get('chainId'), relation.get('chain'), ''))
        if chain != 'all' and chain_id != chain:
            continue
        key = chain_id+':'+str(relation['pool']).lower()
        previous = pool_index.get(key)
        current_at = numeric(relation.get('liquidityAt')) or 0
        previous_at = numeric((previous or {}).get('liquidityAt')) or 0
        if (previous is None or current_at > previous_at or
                (current_at == previous_at and (numeric(relation.get('checkedAt')) or 0) > (numeric(previous.get('checkedAt')) or 0))):
            pool_index[key] = relation
    rows = []
    search = q.strip().lower()
    for key, relation in pool_index.items():
        liquidity = numeric(relation.get('liquidityUsd'))
        if qualified and not (liquidity is not None and liquidity >= 1000 and recent(relation.get('liquidityAt'), 900_000, now)):
            continue
        asset = assets.get(asset_key(relation))
        if search and not any(search in str(value or '').lower() for value in
                              [(asset or {}).get('symbol'), (asset or {}).get('name'),
                               *(relation.get(field) for field in ('token', 'pool', 'stock', 'stockSide', 'ticker', 'protocol'))]):
            continue
        market = relation.get('poolMarket') or {}
        matches = (market.get('scope') == 'pool:'+str(relation['pool']).lower()
                   and (not market.get('currency') or market['currency'] == 'USD')
                   and (not market.get('volumeCurrency') or market['volumeCurrency'] == 'USD'))
        volume = numeric(market.get('volume24h')) if matches else None
        if volume is not None and volume < 0:
            volume = None
        created = numeric(relation.get('poolCreatedAt'))
        volume_at = numeric(market.get('updatedAt')) if matches else None
        small_relation = {field: relation[field] for field in POOL_RELATION_FIELDS if field in relation}
        # The pool UI consumes liquiditySource; retain the valuation's own
        # provider rather than the independent pool-volume provider.
        liquidity_source = first(relation.get('liquiditySource'), relation.get('liquidityProvider'))
        if liquidity_source:
            small_relation['liquiditySource'] = liquidity_source
        small_relation['poolMarket'] = {field: market[field] for field in ('provider', 'source', 'scope', 'currency', 'volumeCurrency', 'updatedAt') if field in market}
        rows.append({'key': key, 'chainId': str(first(relation.get('chainId'), relation.get('chain'), '')),
                     'relation': small_relation,
                     'asset': {field: asset[field] for field in ('chainId', 'token', 'symbol', 'name') if field in asset} if asset else None,
                     'liquidity': liquidity, 'liquidityCurrent': recent(relation.get('liquidityAt'), 900_000, now),
                     'createdAt': created if created is not None and created > 0 else None,
                     'volume24h': volume, 'volumeAt': volume_at if volume_at is not None and volume_at > 0 else None,
                     'volumeCurrent': volume is not None and recent(market.get('updatedAt'), 900_000, now)})
    sort = sort if sort in POOL_SORTS else 'liquidity'
    def order(row):
        current = row['liquidityCurrent'] if sort == 'liquidity' else row['volumeCurrent'] if sort == 'volume24h' else True
        return -int(current), -int(row[sort] is not None), -(row[sort] or 0), _text_order(row['key'])
    rows.sort(key=order)
    grouped = {}
    for row in rows:
        relation = row['relation']
        key = row['chainId']+':'+str(relation.get('token')).lower()+':'+str(first(relation.get('stock'), relation.get('stockSide'), '')).lower()
        row['groupKey'] = key
        grouped.setdefault(key, []).append(row)
    groups = [{'key': key, 'groupIndex': index, 'count': len(items), 'items': items} for index, (key, items) in enumerate(grouped.items())]
    for group in groups:
        for row in group['items']:
            row['groupIndex'] = group['groupIndex']
    summary = {}
    for field, freshness in (('liquidity', 'liquidityCurrent'), ('volume24h', 'volumeCurrent')):
        values = [row[field] for row in rows if row[field] is not None and row[freshness]]
        summary[field] = {'known': len(values), 'total': sum(values), 'max': max([0, *values])}
    offset = min(offset, max(0, (len(groups)-1)//limit)*limit)
    return {**envelope(payload, now), 'groups': groups[offset:offset+limit],
            'directory': {'totalPools': len(rows), 'groupTotal': len(groups), 'limit': limit, 'offset': offset},
            'summary': summary, 'unified': {'snapshotScope': 'memes', 'assets': [], 'stockTokens': [], 'relations': []}}
