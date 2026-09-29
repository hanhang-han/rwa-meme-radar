"""Bounded list DTOs. Detailed evidence remains on token/comparison endpoints."""
ASSET_FIELDS = set('chainId token symbol name kind price priceCurrency priceScope provider venue quoteType quoteAt quoteStatus quoteReason volume24h volumeCurrency volumeScope liquidity totalLiquidityUsd totalLiquidityAt totalLiquidityStatus totalLiquidityCoverage pairLiquidityUsd pairLiquidityStatus pairLiquidityCoverage marketCap change24h txs24h buys24h sells24h holders firstSeen tradeAt tradeCoverage observedVolume5m observedVolume1h observedBuys5m observedSells5m observedBucket5mComplete activityScope activityComparable marketQuotes exchangeMarkets riskFlags riskStatus relationLevel match'.split())
STOCK_FIELDS = set('chainId tokenContractAddress assetId instrumentId assetCode tokenSymbol tokenName stockCode issuer provider providers price priceCurrency priceScope volume24h volumeCurrency volumeScope change24h exchangeTrades24h quoteAt quoteStatus quoteReason stockPrice referenceAt referenceCurrency referenceProvider referenceStatus referenceReason referenceScope referenceDelayMs referenceRealtime marketSession isTradingHalt tokenToAssetRatio ratioVerified ratioVersion marketQuotes verificationStatus'.split())
TIME_FIELDS = set('price volume24h marketCap change24h liquidity holders buys24h sells24h txs24h observedVolume5m observedVolume1h observedBuys5m observedSells5m stockPrice referenceAt'.split())
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
            'checkedAt': assessment.get('checkedAt'),
        }
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
    out = {k: v for k, v in row.items() if k not in {'amounts', 'poolMarket', 'priceComparison', 'stockBalance', 'error'} and v is not None}
    if row.get('amounts'):
        out['amounts'] = [pick(a, {'tokenContractAddress', 'tokenSymbol'}) for a in row['amounts']]
    comparison = row.get('priceComparison')
    if comparison:
        out['priceComparison'] = {'relative': {w: metric_summary(m) for w, m in (comparison.get('relative') or {}).items()},
                                  'spread': metric_summary(comparison.get('spread')),
                                  'calculatedAt': comparison.get('calculatedAt')}
    return out


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
    unified['assets'] = [asset_summary(row) for row in unified.get('assets', [])]
    unified['stockTokens'] = [stock_summary(row) for row in unified.get('stockTokens', [])]
    unified['relations'] = [relation_summary(row) for row in unified.get('relations', [])]
    unified['signals'] = [{k: v for k, v in row.items() if k != 'dataQuality'} for row in unified.get('signals', [])]
    for kind in ('assets', 'stockTokens', 'relations', 'sectors'):
        unified[kind] = [{**row, 'projectionKey': projection_key(kind, row)} for row in unified.get(kind, [])]
    return {**payload, 'unified': unified}


def overview_dashboard(payload):
    unified = dict(payload['unified'])
    assets = unified.get('assets', [])
    related = sorted([a for a in assets if (a.get('dataQuality') or {}).get('eligible', {}).get('relationRanking')],
                     key=lambda a: -(a.get('volume24h') or 0))[:12]
    ids = {(str(a.get('chainId')), a.get('token')) for a in related}
    unified['assets'] = related
    unified['relations'] = [r for r in unified.get('relations', []) if (str(r.get('chainId')), r.get('token')) in ids]
    unified['stockTokens'] = []
    unified['groups'] = []
    unified['snapshotScope'] = 'overview'
    unified['sectors'] = [{k:v for k,v in row.items() if k!='history'} for row in unified.get('sectors', [])]
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
