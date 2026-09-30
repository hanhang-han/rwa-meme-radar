"""Bounded list DTOs. Detailed evidence remains on token/comparison endpoints."""
ASSET_FIELDS = set('chainId token symbol name kind price priceCurrency priceScope provider venue quoteType quoteAt quoteStatus quoteReason volume24h volumeCurrency volumeScope liquidity totalLiquidityUsd totalLiquidityAt totalLiquidityStatus totalLiquidityCoverage pairLiquidityUsd pairLiquidityStatus pairLiquidityCoverage marketCap change24h txs24h buys24h sells24h holders firstSeen tradeAt tradeCoverage observedVolume5m observedVolume1h observedBuys5m observedSells5m observedBucket5mComplete activityScope activityComparable marketQuotes exchangeMarkets riskFlags riskStatus relationLevel match primaryQuote quoteAlternatives assetCategory derivative'.split())
STOCK_FIELDS = set('chainId tokenContractAddress assetId instrumentId assetCode tokenSymbol tokenName stockCode issuer provider providers price priceCurrency priceScope volume24h volumeCurrency volumeScope change24h exchangeTrades24h quoteAt quoteStatus quoteReason stockPrice referenceAt referenceCurrency referenceProvider referenceStatus referenceReason referenceScope referenceDelayMs referenceRealtime marketSession isTradingHalt tokenToAssetRatio ratioVerified ratioVersion marketQuotes verificationStatus'.split())
TIME_FIELDS = set('price volume24h marketCap change24h liquidity totalLiquidityUsd holders buys24h sells24h txs24h observedVolume5m observedVolume1h observedBuys5m observedSells5m stockPrice referenceAt'.split())
METRIC_FIELDS = set('value status reason method at validUntil realtimeUntil unit stockReturn memeReturn from to windowMs delayMs'.split())


def pick(row, fields):
    return {k: v for k, v in row.items() if k in fields and v is not None}


def metric_summary(metric):
    if not isinstance(metric, dict):
        return metric
    return {**pick(metric, METRIC_FIELDS), 'value': metric.get('value')}


def common(row, fields):
    out = pick(row, fields)
    times = pick(row.get('fieldTimes') or {}, TIME_FIELDS)
    if times:
        out['fieldTimes'] = times
    sources = pick(row.get('fieldSources') or {}, TIME_FIELDS)
    if sources:
        out['fieldSources'] = sources
    for field in ('fieldScopes', 'fieldTimeKinds', 'fieldStatus'):
        if row.get(field):
            out[field] = {k: v for k, v in row[field].items() if k in TIME_FIELDS}
    if row.get('priceProvenance'):
        out['priceProvenance'] = row['priceProvenance']
    return out


def asset_summary(row):
    out = common(row, ASSET_FIELDS)
    # These are evaluation clocks, not observations. The enclosing snapshot's
    # `now` dates the evaluation; publishing them per row makes every idle
    # expiry sweep resend the entire universe. Keep all evidence timestamps
    # and status transitions, including checks which become stale.
    if isinstance(out.get('pairLiquidityCoverage'), dict):
        out['pairLiquidityCoverage'] = {k: v for k, v in out['pairLiquidityCoverage'].items() if k != 'asOf'}
    # Explicit nulls distinguish unavailable aggregate/pair TVL from a real
    # zero in the same projection revision and survive the realtime delta.
    out['totalLiquidityUsd'] = row.get('totalLiquidityUsd')
    out['pairLiquidityUsd'] = row.get('pairLiquidityUsd')
    quality = row.get('dataQuality') or {}
    out['dataQuality'] = pick(quality, {'tier', 'eligible'})
    # List cards need only triggered evidence for their risk badges. Unknown
    # checks remain visible via riskStatus and the full token-detail endpoint.
    assessment = row.get('riskAssessment') or {}
    checks = assessment.get('checks') or {}
    if checks and row.get('riskFlags'):
        out['riskAssessment'] = {
            'checks': {flag: checks[flag] for flag in row['riskFlags'] if flag in checks},
        }
    if assessment.get('safety'):
        out.setdefault('riskAssessment', {})['safety'] = assessment['safety']
    return out


def stock_summary(row):
    out = common(row, STOCK_FIELDS)
    if row.get('issuerIdentity'):
        out['issuerIdentity'] = pick(row['issuerIdentity'], {
            'verificationStatus', 'eligibleForPair', 'underlyingId', 'ticker',
            'tokenSymbol', 'issuer', 'tokenKind', 'version', 'sourceUrl',
            'sourceAt', 'manifestVersion',
        })
    if row.get('stockIdentity'):
        out['stockIdentity'] = pick(row['stockIdentity'], {'id', 'code', 'market', 'currency', 'status', 'nameZh', 'nameEn'})
    out['premium'] = metric_summary(row.get('premium'))
    return out


def relation_summary(row):
    out = {k: v for k, v in row.items() if k not in {'amounts', 'poolMarket', 'priceComparison', 'stockBalance', 'error', 'evaluatedAt'} and v is not None}
    if row.get('amounts'):
        out['amounts'] = [pick(a, {'tokenContractAddress', 'tokenSymbol'}) for a in row['amounts']]
    if row.get('poolMarket'):
        out['poolMarket'] = pick(row['poolMarket'], {'volume24h', 'buys24h', 'sells24h', 'txs24h', 'provider', 'updatedAt', 'scope', 'currency', 'volumeCurrency'})
    comparison = row.get('priceComparison')
    if comparison:
        out['priceComparison'] = {'relative': {w: metric_summary(m) for w, m in (comparison.get('relative') or {}).items()},
                                  'spread': metric_summary(comparison.get('spread')),
                                  'calculatedAt': comparison.get('calculatedAt')}
    return out


def _current_usd_volume(row, now):
    import math
    value = row.get('volume24h')
    at = (row.get('fieldTimes') or {}).get('volume24h')
    scope = (row.get('fieldScopes') or {}).get('volume24h')
    currency = row.get('volumeCurrency') or row.get('priceCurrency')
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
            and currency == 'USD' and scope in ('token', 'token-aggregate')
            and isinstance(at, (int, float)) and 0 <= now-at <= 900_000)


def discovery_rankings(assets, relations, stocks, now, chain=None):
    """Rank the complete canonical universe before any preview truncation."""
    rows = {(str(a.get('chainId')), str(a.get('token') or '').lower()): a for a in assets
            if a.get('assetCategory') != 'derivative' and (chain is None or str(a.get('chainId')) == chain)}
    themes = {}
    tickers = {}
    for relation in relations:
        key = (str(relation.get('chainId')), str(relation.get('token') or '').lower())
        ticker = (relation.get('stockIdentity') or {}).get('ticker') or relation.get('ticker')
        if key in rows and ticker and relation.get('level') == 'A' and relation.get('status') == 'verified':
            themes.setdefault(ticker, set()).add(key)
            tickers.setdefault(key, set()).add(ticker)
    stock_index = {}
    for stock in stocks:
        if chain is not None and str(stock.get('chainId')) != chain:
            continue
        ticker = (stock.get('stockIdentity') or {}).get('code') or stock.get('stockCode')
        quote_at = (stock.get('fieldTimes') or {}).get('price') or stock.get('quoteAt') or 0
        prior = stock_index.get(ticker) or {}
        prior_at = (prior.get('fieldTimes') or {}).get('price') or prior.get('quoteAt') or 0
        if ticker and (ticker not in stock_index or (stock.get('price') is not None, quote_at) > (prior.get('price') is not None, prior_at)):
            stock_index[ticker] = stock
    hot = []
    for ticker, identities in themes.items():
        known = [rows[key]['volume24h'] for key in identities if _current_usd_volume(rows[key], now)]
        hot.append({'ticker': ticker, 'stock': _market_stock(stock_index[ticker]) if ticker in stock_index else None,
                    'assetCount': len(identities), 'volume24h': sum(known) if known else None,
                    'volumeKnown': len(known), 'volumeTotal': len(identities), 'window': '24h', 'scope': 'theme-assets'})
    hot.sort(key=lambda row: (row['volume24h'] is None, -(row['volume24h'] or 0), -row['assetCount'], row['ticker']))
    eligible = [(key, row) for key, row in rows.items() if _current_usd_volume(row, now)]
    eligible.sort(key=lambda item: (-item[1]['volume24h'], item[0]))
    leaders = []
    for key, row in eligible[:10]:
        item = pick(row, {'chainId', 'token', 'symbol', 'name', 'price', 'change24h', 'volume24h', 'volumeCurrency', 'priceCurrency', 'riskFlags'})
        item['fieldTimes'] = pick(row.get('fieldTimes') or {}, {'price', 'volume24h', 'change24h'})
        item['fieldSources'] = pick(row.get('fieldSources') or {}, {'price', 'volume24h'})
        item['fieldScopes'] = pick(row.get('fieldScopes') or {}, {'price', 'volume24h', 'change24h'})
        item['tickers'] = sorted(tickers.get(key, []))
        leaders.append(item)
    return hot[:6], {'items': leaders, 'total': len(eligible), 'window': '24h', 'scope': 'fresh-usd-token-volume', 'asOf': now}


def compact_dashboard(payload):
    """Only list evidence is reduced; no values are reinterpreted or filled."""
    from .stock_identity import manifest_status
    unified = dict(payload['unified'])
    unified['snapshotScope'] = 'full'
    unified['identityCatalog'] = manifest_status()
    # Current views group the authoritative assets locally. Sending the same
    # rows a second time made every quote emit a large replacement meta field.
    unified['groups'] = []
    unified['totals'] = {key: len(unified.get(key, [])) for key in ('assets', 'stockTokens', 'relations')}
    quality = {**(unified.get('quality') or {}), 'assets': dict((unified.get('quality') or {}).get('assets') or {})}
    for field in ('price', 'volume24h', 'observedVolume5m', 'holders'):
        rows = unified.get('assets', [])
        quality['assets'][field] = {'total': len(rows), 'known': sum(r.get(field) is not None for r in rows),
            'fresh': sum(r.get(field) is not None and (r.get('fieldTimes') or {}).get(field) is not None and 0 <= payload['now'] - r['fieldTimes'][field] <= 900000 for r in rows)}
    unified['quality'] = quality
    unified['hotStocks'], unified['memeLeaders'] = discovery_rankings(unified.get('assets', []), unified.get('relations', []), unified.get('stockTokens', []), payload['now'])
    unified['hotStocksByChain'], unified['memeLeadersByChain'] = {}, {}
    for chain in ('196', '56', '4663'):
        unified['hotStocksByChain'][chain], unified['memeLeadersByChain'][chain] = discovery_rankings(unified.get('assets', []), unified.get('relations', []), unified.get('stockTokens', []), payload['now'], chain)
    unified['assets'] = [asset_summary(row) for row in unified.get('assets', [])]
    unified['stockTokens'] = [stock_summary(row) for row in unified.get('stockTokens', [])]
    unified['relations'] = [relation_summary(row) for row in unified.get('relations', [])]
    unified['signals'] = [{k: v for k, v in row.items() if k != 'dataQuality'} for row in unified.get('signals', [])]
    for kind in ('assets', 'stockTokens', 'relations', 'sectors'):
        unified[kind] = [{**row, 'projectionKey': projection_key(kind, row)} for row in unified.get(kind, [])]
    return {**payload, 'unified': unified}


def _overview_theme_map(value):
    value = value or {}
    out = pick(value, set('version asOf window scope sizeMetric colorMetric totalAssets'.split()))
    themes = value.get('themes') or []
    out['totalThemes'] = len(themes)
    out['themes'] = [{**pick(t, {'ticker', 'nameEn', 'nameZh'}),
                      'breadth': pick(t.get('breadth') or {}, {'rising', 'falling', 'flat', 'unknown', 'valid', 'total', 'window'}),
                      } for t in themes[:6]]
    # Represent each visible theme before giving any theme a second bubble.
    # A volume-sorted slice otherwise spends the entire first paint on one theme.
    by_theme = {t.get('ticker'): [] for t in out['themes']}
    for bubble in value.get('bubbles') or []:
        if bubble.get('primaryTicker') in by_theme:
            by_theme[bubble['primaryTicker']].append(bubble)
    bubbles = [rows[index] for index in range(2) for rows in by_theme.values() if len(rows) > index]
    out['bubbles'] = []
    for bubble in bubbles:
        row = pick(bubble, {'key', 'chainId', 'token', 'symbol', 'name', 'primaryTicker'})
        row['otherTickers'] = (bubble.get('otherTickers') or [])[:4]
        for field in ('price', 'volume24h', 'change24h'):
            row[field] = pick(bubble.get(field) or {}, {'value', 'unit', 'currency', 'scope', 'source', 'observedAt', 'status'})
        row['relation'] = pick(bubble.get('relation') or {}, {'id', 'pool', 'stock', 'checkedAt', 'poolCreatedAt', 'discoveredAt', 'liquidityUsd', 'liquidityAt'})
        out['bubbles'].append(row)
    out['displayedAssets'] = len(out['bubbles'])
    return out


def _overview_metrics(value):
    value = value or {}
    out = pick(value, {'verifiedPools', 'freshVerifiedPools', 'verifiedAssets', 'actionableAssets',
                       'pairedLiquidityUsd', 'pairedVolume24hUsd', 'newPair24h', 'newRelations24h',
                       'activeMemeCount', 'newAssets24h'})
    for field in ('pairedLiquidityUsd', 'pairedVolume24hUsd', 'newPair24h'):
        if field in value:
            out[field] = value[field]
    for field, keys in (('liquidityCoverage', {'known', 'valued', 'total', 'fresh', 'complete', 'volumeKnown', 'scope', 'asOf'}),
                        ('newPair24hCoverage', {'known', 'total'})):
        if value.get(field):
            out[field] = pick(value[field], keys)
    return out


def overview_dashboard(payload):
    """A first-paint DTO with an enforced 50 KB uncompressed JSON budget.

    Counts and coverage always describe the complete canonical revision. Lists
    are explicit previews; heavy evidence is fetched by the destination page.
    """
    import json
    source = payload['unified']
    unified = pick(source, {'version'})
    if 'totals' in source:
        unified['totals'] = pick(source['totals'], {'assets', 'stockTokens', 'relations'})
    unified['metrics'] = _overview_metrics(source.get('metrics'))
    unified['metricsByChain'] = {chain: _overview_metrics((source.get('metricsByChain') or {}).get(chain)) for chain in ('196', '56', '4663')}
    if source.get('newAssets24h'):
        unified['newAssets24h'] = pick(source['newAssets24h'], {'value', 'window', 'from', 'to', 'scope', 'knownFirstSeen', 'timeField'})
        unified['newAssets24h']['byChain'] = pick(source['newAssets24h'].get('byChain') or {}, {'196', '56', '4663'})
    unified['collection'] = pick(source.get('collection') or {}, {'updatedAt'})
    unified['distribution'] = []
    for distribution in source.get('distribution', [])[:3]:
        row = pick(distribution, {'chainId', 'name', 'assets'})
        for field in ('volume', 'liquidity'):
            row[field] = pick(distribution.get(field) or {}, {'value', 'known', 'fresh', 'total', 'scope', 'asOf'})
        unified['distribution'].append(row)
    # These lists were ranked against all rows by the publisher, never against
    # this page's bounded preview. Copy before the wire-budget trim.
    import copy
    for field in ('hotStocks', 'hotStocksByChain', 'memeLeaders', 'memeLeadersByChain'):
        if field in source:
            unified[field] = copy.deepcopy(source[field])
    unified['hotStocks'] = unified.get('hotStocks', [])[:3]
    unified['hotStocksByChain'] = {chain: rows[:3] for chain, rows in unified.get('hotStocksByChain', {}).items()}
    if unified.get('memeLeaders'):
        unified['memeLeaders']['items'] = unified['memeLeaders'].get('items', [])[:6]
    for ranking in unified.get('memeLeadersByChain', {}).values():
        ranking['items'] = ranking.get('items', [])[:4]
    quality = source.get('quality') or {}
    unified['quality'] = {kind: {field: pick(coverage, {'total', 'known', 'fresh'})
                                for field, coverage in (quality.get(kind) or {}).items()
                                if isinstance(coverage, dict)} for kind in ('assets', 'stocks')}
    unified['quality']['summary'] = pick(quality.get('summary') or {}, {'total', 'current', 'historical', 'missing', 'eligible', 'tiers'})
    unified['sources'] = [pick(row, {'id', 'provider', 'chainId', 'chainName', 'kind', 'status', 'updatedAt'})
                          for row in source.get('sources', [])[:16]]
    assets = source.get('assets', [])
    related = sorted([a for a in assets if (a.get('dataQuality') or {}).get('eligible', {}).get('relationRanking')],
                     key=lambda a: -(a.get('volume24h') or 0))[:2]
    unified['assets'] = []
    for row in related:
        small = _market_asset(row)
        assessment = row.get('riskAssessment') or {}
        small.pop('riskAssessment', None)
        if assessment.get('safety'):
            small['riskAssessment'] = {'safety': assessment['safety']}
        small['dataQuality'] = pick(row.get('dataQuality') or {}, {'tier', 'eligible'})
        unified['assets'].append(small)
    ids = {(str(a.get('chainId')), a.get('token')) for a in related}
    relations = [r for r in source.get('relations', []) if (str(r.get('chainId')), r.get('token')) in ids][:12]
    unified['relations'] = [_market_relation(r) for r in relations]
    for row in unified['relations']:
        row.pop('priceComparison', None)
        row.pop('amounts', None)
    stock_ids = {(str(r.get('chainId')), str(r.get('stock') or '').lower())
                 for r in relations if r.get('level') == 'A' and r.get('status') == 'verified'}
    unified['stockTokens'] = [_market_stock(s) for s in source.get('stockTokens', [])
                              if (str(s.get('chainId')), str(s.get('tokenContractAddress') or '').lower()) in stock_ids
                              and (s.get('issuerIdentity') or {}).get('verificationStatus') == 'official'][:12]
    unified['groups'] = []
    unified['sectors'] = []
    unified['signals'] = [pick(s, {'id', 'chainId', 'asset', 'symbol', 'kind', 't', 'pool', 'qualified'})
                          for s in source.get('signals', [])[:6]]
    unified['themeMap'] = _overview_theme_map(source.get('themeMap'))
    changes = source.get('importantChanges') or {}
    unified['importantChanges'] = {**pick(changes, {'version', 'asOf', 'scope'}),
        'items': [pick(r, {'id', 'type', 'chainId', 'token', 'symbol', 'ticker', 'occurredAt', 'discoveredAt', 'verifiedAt', 'coverage'})
                  | {'relation': pick(r.get('relation') or {}, {'id', 'pool', 'stock', 'checkedAt', 'poolCreatedAt', 'discoveredAt'})}
                  for r in changes.get('items', [])[:6]]}
    unified['snapshotScope'] = 'overview'
    result = {**pick(payload, {'now', 'realtime'}), 'unified': unified}
    # A pathological source label or large registry cannot defeat the wire
    # budget. Trim preview rows, never alter global totals or quote values.
    def encoded_size():
        return len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode())
    trim = [unified['themeMap']['bubbles'], unified['assets'], unified['relations'], unified['stockTokens'],
            unified['importantChanges']['items'], unified['signals'], unified['themeMap']['themes']]
    trim += [unified.get('hotStocks', []), (unified.get('memeLeaders') or {}).get('items', [])]
    trim += list((unified.get('hotStocksByChain') or {}).values())
    trim += [row.get('items', []) for row in (unified.get('memeLeadersByChain') or {}).values()]
    while encoded_size() > 50_000 and any(trim):
        max((rows for rows in trim if rows), key=lambda rows: len(json.dumps(rows))).pop()
    unified['themeMap']['displayedAssets'] = len(unified['themeMap']['bubbles'])
    return result


# The durable full projection retains evidence for many fields on every row.
# Market pages need every identity, but only the columns they actually render;
# token-detail and comparison endpoints remain the source of detailed evidence.
MARKET_ASSET_FIELDS = set('chainId token symbol name kind price priceCurrency priceScope quoteType marketId provider venue quoteAt quoteStatus quoteReason volume24h volumeCurrency volumeScope totalLiquidityUsd totalLiquidityAt totalLiquidityStatus totalLiquidityCoverage pairLiquidityUsd marketCap change24h txs24h buys24h sells24h holders firstSeen riskFlags riskStatus relationLevel match projectionKey primaryQuote quoteAlternatives assetCategory derivative'.split())
MARKET_STOCK_FIELDS = set('chainId tokenContractAddress assetId instrumentId stockCode tokenSymbol tokenName issuer price priceCurrency priceScope provider volume24h volumeCurrency change24h quoteAt quoteStatus quoteReason stockPrice referenceAt referenceCurrency referenceProvider referenceStatus referenceReason referenceScope referenceDelayMs referenceRealtime marketSession verificationStatus projectionKey'.split())
MARKET_RELATION_FIELDS = set('id chainId token stock stockSide ticker pool token0 token1 wrapper protocol feePct firstSeen poolCreatedAt discoveredAt checkedAt block status liquidityUsd liquidityAt level evidenceStatus verificationStatus projectionKey'.split())


def _market_nested(row, field, fields):
    value = pick(row.get(field) or {}, fields)
    return {field: value} if value else {}


def _market_asset(row):
    out = pick(row, MARKET_ASSET_FIELDS)
    out.pop('quoteAlternatives', None)
    # Volume/liquidity comparability depends on provenance, not merely values.
    if isinstance(out.get('totalLiquidityCoverage'), dict):
        out['totalLiquidityCoverage'] = pick(out['totalLiquidityCoverage'], {'scope', 'coverage', 'provider'})
    for field, keys in (
        ('fieldTimes', {'price', 'volume24h', 'change24h', 'holders', 'buys24h', 'sells24h'}),
        ('fieldSources', {'price', 'volume24h', 'change24h', 'holders'}),
        ('fieldScopes', {'volume24h'}),
        ('priceProvenance', {'timeKind'}),
    ):
        out.update(_market_nested(row, field, keys))
    quality = row.get('dataQuality') or {}
    if quality.get('tier'):
        out['dataQuality'] = {'tier': quality['tier']}
    if row.get('riskAssessment'):
        out['riskAssessment'] = row['riskAssessment']
    return out


def _market_stock(row):
    out = pick(row, MARKET_STOCK_FIELDS)
    for field, keys in (
        ('fieldTimes', {'price', 'stockPrice', 'volume24h', 'change24h'}),
        ('fieldSources', {'price'}),
        ('priceProvenance', {'timeKind'}),
        ('issuerIdentity', {'verificationStatus', 'eligibleForPair'}),
        ('stockIdentity', {'id', 'code', 'status', 'nameZh', 'nameEn'}),
    ):
        out.update(_market_nested(row, field, keys))
    # ComparisonMetric uses validity boundaries to avoid presenting an expired
    # premium as current. Preserve them even though input evidence is omitted.
    if row.get('premium') is not None:
        out['premium'] = pick(row['premium'], {'value', 'status', 'reason', 'at', 'validUntil', 'realtimeUntil'})
        out['premium']['value'] = row['premium'].get('value')
    return out


def _market_relation(row):
    out = pick(row, MARKET_RELATION_FIELDS)
    for field in ('stockIdentity', 'sideIdentity'):
        out.update(_market_nested(row, field, {'verificationStatus', 'eligibleForPair'}))
    if row.get('priceComparison'):
        comparison = row['priceComparison']
        relative = (comparison.get('relative') or {}).get('1h')
        out['priceComparison'] = {'relative': {'1h': relative} if relative else {}}
    if row.get('amounts'):
        out['amounts'] = row['amounts']
    if row.get('poolMarket'):
        out['poolMarket'] = pick(row['poolMarket'], {'volume24h', 'provider', 'updatedAt', 'scope', 'currency', 'volumeCurrency'})
    return out


def market_dashboard(payload):
    """All market identities with list evidence; full/overview remain intact."""
    unified = dict(payload['unified'])
    unified['snapshotScope'] = 'market'
    unified['assets'] = [_market_asset(row) for row in unified.get('assets', [])]
    unified['stockTokens'] = [_market_stock(row) for row in unified.get('stockTokens', [])]
    unified['relations'] = [_market_relation(row) for row in unified.get('relations', [])]
    return {**payload, 'unified': unified}


def projection_key(kind, row):
    chain = str(row.get('chainId') or '196')
    if kind == 'assets':
        identity = str(row.get('token') or '').lower()
    elif kind == 'stockTokens':
        identity = str(row.get('tokenContractAddress') or row.get('assetId') or row.get('stockCode') or '').lower()
    elif kind == 'relations':
        identity = str(row.get('id') or '|'.join(str(row.get(k) or '').lower() for k in ('pool', 'token', 'stock')))
    else:
        identity = str(row.get('sector') or '')
    return chain + ':' + identity
