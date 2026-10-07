"""Evidence-bound facts and short asset readouts from a published revision.

The readout deliberately has no generated numbers.  An optional model may
rephrase these three lines only after its output passes the numeric and wording
checks below.  Unknown data stays null in the fact packet.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo


MARKET_MAX_AGE_MS = 15 * 60_000
READOUT_MAX_AGE_MS = 3 * 60_000
CHAIN_NAMES = {'196': 'X Layer', '56': 'BNB Chain', '4663': 'Robinhood Chain'}
LABELS = {'zh': ('关系', '行情', '风险'),
          'en': ('Pair', 'Market', 'Risk')}
RISK_LABELS = {
    'zh': {'tax': '税费', 'permissions': '合约权限', 'liquidityLock': 'LP 状态',
           'concentration': '前 10 持仓', 'creatorHolding': '创建者持仓', 'turnover': '成交异常', 'thinSpike': '低深度波动'},
    'en': {'tax': 'tax', 'permissions': 'permissions', 'liquidityLock': 'LP',
           'concentration': 'top holders', 'creatorHolding': 'creator', 'turnover': 'turnover', 'thinSpike': 'thin spike'},
}
BLOCKED = ('建议', '应该', '买入', '必涨', '值得关注', '看涨', '看跌', '机会', '暴雷',
           '稳了', '可能会', '稳赚', '抄底', '快跑', '推荐', '预测',
           'should', 'recommend', 'buy now', 'guarantee', 'will rise',
           'bullish', 'bearish', 'moon', 'opportunity')
NUMERIC = re.compile(r'(?<!\d)[-+]?\d+(?:[.,]\d+)?%?')


def _number(value, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if minimum is not None and value < minimum:
        return None
    return value


def _time(value, now):
    at = _number(value, 1)
    return at if at is not None and 0 <= now - at <= MARKET_MAX_AGE_MS else None


def _market_field(asset, field, now, *, currency=None):
    value = _number(asset.get(field))
    at = _time((asset.get('fieldTimes') or {}).get(field), now)
    scope = (asset.get('fieldScopes') or {}).get(field)
    observed_currency = asset.get('volumeCurrency' if field == 'volume24h' else 'priceCurrency')
    if (value is None or at is None or scope not in ('token', 'token-aggregate')
            or (currency and observed_currency != currency)):
        return None
    return {'value': value, 'source': (asset.get('fieldSources') or {}).get(field)
            or asset.get('provider'), 'observedAt': at, 'scope': scope,
            'currency': observed_currency if currency else None}


def _pool_fact(relation, now):
    liquidity = _number(relation.get('liquidityUsd'), 0)
    liquidity_at = _time(relation.get('liquidityAt'), now)
    if liquidity is None or liquidity_at is None:
        return None, None
    pool = str(relation.get('pool') or '').lower()
    pool_market = relation.get('poolMarket') or {}
    volume = _number(pool_market.get('volume24h'), 0)
    volume_at = _time(pool_market.get('updatedAt'), now)
    volume_currency = pool_market.get('volumeCurrency') or pool_market.get('currency')
    comparable = (volume is not None and volume_at is not None and liquidity > 0
                  and volume_currency == 'USD'
                  and pool_market.get('scope') == 'pool:' + pool
                  and abs(volume_at - liquidity_at) <= 300_000)
    pool_liquidity = {'value': liquidity, 'source': relation.get('liquidityProvider')
                      or relation.get('provider'), 'observedAt': liquidity_at,
                      'scope': 'pool:' + pool, 'currency': 'USD'}
    if not comparable:
        return pool_liquidity, None
    return pool_liquidity, {'value': volume, 'source': pool_market.get('provider'),
                            'observedAt': volume_at, 'scope': 'pool:' + pool,
                            'currency': 'USD'}


def _risks(asset):
    assessment = asset.get('riskAssessment') or {}
    safety = assessment.get('safety') or {}
    checks = assessment.get('checks') or {}
    raw = {
        'tax': safety.get('tax'), 'permissions': safety.get('permissions'),
        'liquidityLock': safety.get('liquidityLock'),
        'concentration': safety.get('concentration'), 'creatorHolding': safety.get('creatorHolding'),
        'turnover': checks.get('wash_suspect'),
        'thinSpike': checks.get('thin_spike'),
    }
    result = {}
    for key, check in raw.items():
        state = (check or {}).get('status')
        result[key] = {'status': state if state in ('clear', 'triggered') else 'unknown',
                       'source': (check or {}).get('provider'),
                       'observedAt': (check or {}).get('checkedAt'), 'evidence': (check or {}).get('evidence') or {},
                       'severity': (check or {}).get('severity')}
    return result


def build_fact_packet(snapshot, chain, address):
    """Return null unless the published revision proves an A-grade stock pool."""
    asset = snapshot.get('asset') or {}
    now = _number(snapshot.get('snapshotAt'), 1)
    if now is None or not asset:
        return None
    verified = [r for r in snapshot.get('relations') or ()
                if r.get('level') == 'A' and r.get('status') == 'verified'
                and str(r.get('chainId')) == str(chain)
                and str(r.get('token') or '').lower() == address.lower()
                and r.get('pool')]
    if not verified:
        return None
    verified.sort(key=lambda row: (_number(row.get('liquidityUsd'), 0) or 0,
                                   _number(row.get('liquidityAt'), 0) or 0), reverse=True)
    relation = verified[0]
    identity = relation.get('stockIdentity') or {}
    ticker = identity.get('ticker') or relation.get('ticker')
    pool_liquidity, pool_volume = _pool_fact(relation, now)
    price = _market_field(asset, 'price', now, currency='USD')
    change_24h = _market_field(asset, 'change24h', now) if price else None
    volume_24h = _market_field(asset, 'volume24h', now, currency='USD')
    # A token-wide market volume may not be divided by one stock-pair pool's
    # liquidity.  Only the same pool's volume and liquidity form this ratio.
    ratio = round(pool_volume['value'] / pool_liquidity['value'], 2) if pool_volume else None
    relation_at = _number(relation.get('poolCreatedAt'), 1)
    if relation_at is not None and relation_at > now:
        relation_at = None
    market_times = [field['observedAt'] for field in
                    (price, change_24h, volume_24h, pool_liquidity, pool_volume) if field]
    data_as_of = min(market_times) if market_times else None
    risks = _risks(asset)
    concentration = ((asset.get('riskAssessment') or {}).get('safety') or {}).get('concentration') or {}
    top10 = _number((concentration.get('evidence') or {}).get('top10AdjustedPercent'), 0)
    if concentration.get('status') not in ('clear', 'triggered') or top10 is None or top10 > 100:
        top10 = None
    creator_check = ((asset.get('riskAssessment') or {}).get('safety') or {}).get('creatorHolding') or {}
    creator = _number((creator_check.get('evidence') or {}).get('creatorHoldingPercent'), 0)
    if creator_check.get('status') not in ('clear', 'triggered') or creator is None or creator > 100:
        creator = None
    market = relation.get('poolMarket') or {}
    buys, sells = _number(market.get('buys24h'), 0), _number(market.get('sells24h'), 0)
    if market.get('baseToken') and str(market['baseToken']).lower() != address.lower():
        buys, sells = sells, buys
    activity = {'buys': buys, 'sells': sells, 'at': market.get('updatedAt'), 'scope': market.get('scope')} if pool_volume and buys is not None and sells is not None else None
    missing = [name for name, value in (
        ('price', price), ('change5m', None), ('change1h', None),
        ('change24h', change_24h), ('volume24h', volume_24h),
        ('poolLiquidity', pool_liquidity), ('volumeLiquidityRatio', ratio),
        ('uniqueTraderAddresses', None), ('top10AdjustedPercent', top10),
        ('creatorHoldingPercent', creator), ('kolMentions24h', None),
    ) if value is None]
    fact = {
        'version': 1, 'chainId': str(chain), 'chain': CHAIN_NAMES.get(str(chain)),
        'address': address.lower(), 'name': asset.get('name'), 'symbol': asset.get('symbol'),
        'stockTicker': ticker,
        'relation': {'type': 'official-stock-pool', 'pool': str(relation['pool']).lower(),
                     'dex': relation.get('dex') or relation.get('protocol') or relation.get('venue'),
                     'createdAt': relation_at,
                     'issuerSourceUrl': identity.get('sourceUrl')},
        'priceUsd': price, 'change5m': None, 'change1h': None,
        'change24hPercent': change_24h, 'volume24hUsd': volume_24h,
        'poolLiquidityUsd': pool_liquidity, 'poolVolume24hUsd': pool_volume,
        'volumeLiquidityRatio': ratio,
        'uniqueTraderAddresses': None, 'top10AdjustedPercent': top10,
        'creatorHoldingPercent': creator, 'kolMentions24h': None,
        'risks': risks, 'activity24h': activity, 'missing': missing, 'dataAsOf': data_as_of,
    }
    return fact


def fact_hash(fact):
    return hashlib.sha256(json.dumps(fact, sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _money(number, lang):
    if number >= 1_000_000:
        return f'${number/1_000_000:.1f}M'
    if number >= 1_000:
        return f'${number/1_000:.1f}K'
    return f'${number:.2f}'


def _clock(ms):
    return datetime.fromtimestamp(ms / 1000, ZoneInfo('Asia/Shanghai')).strftime('%H:%M')


def template_lines(fact, lang='zh'):
    """At most three sentences, each composed only from current facts."""
    lang = 'en' if lang == 'en' else 'zh'
    ticker = str(fact.get('stockTicker') or ('stock token' if lang == 'en' else '股票代币'))[:12]
    dex = str((fact.get('relation') or {}).get('dex') or ('DEX' if lang == 'en' else 'DEX'))[:18]
    liquidity = fact.get('poolLiquidityUsd')
    volume = fact.get('poolVolume24hUsd') or fact.get('volume24hUsd')
    ratio = fact.get('volumeLiquidityRatio')
    activity = fact.get('activity24h')
    checks = fact.get('risks') or {}
    unknown = [RISK_LABELS[lang][key] for key, row in checks.items() if row.get('status') == 'unknown' and key in ('tax', 'permissions', 'concentration', 'creatorHolding', 'liquidityLock')]
    findings = []
    for key, row in checks.items():
        if row.get('status') != 'triggered':
            continue
        evidence = row.get('evidence') or {}
        if key == 'tax':
            if evidence.get('honeypot') or evidence.get('cannotSellAll'):
                findings.append('无法卖出' if lang == 'zh' else 'selling restricted')
            for field, label in (('buyTaxPct', '买入税' if lang == 'zh' else 'buy tax'), ('sellTaxPct', '卖出税' if lang == 'zh' else 'sell tax')):
                value = _number(evidence.get(field), 0)
                if value is not None and value > 10:
                    findings.append(f'{label} {value:g}%')
        elif key == 'concentration' and fact.get('top10AdjustedPercent') is not None:
            findings.append(f'前十持仓 {fact["top10AdjustedPercent"]:g}%' if lang == 'zh' else f'top10 {fact["top10AdjustedPercent"]:g}%')
        elif key == 'creatorHolding' and fact.get('creatorHoldingPercent') is not None:
            findings.append(f'创建者持仓 {fact["creatorHoldingPercent"]:g}%' if lang == 'zh' else f'creator {fact["creatorHoldingPercent"]:g}%')
        elif key == 'liquidityLock':
            locked = evidence.get('lockedPercentMin', 0)+evidence.get('burnedPercent', 0)
            findings.append(f'LP 已锁定或销毁 {locked:g}%' if lang == 'zh' else f'LP locked/burned {locked:g}%')
        else:
            findings.append(RISK_LABELS[lang][key])
    if lang == 'zh':
        relation = f'关系：在{dex}与{ticker}建池' + (f'，流动性 {_money(liquidity["value"], lang)}。' if liquidity else '，流动性未读取。')
        bits = []
        if volume:
            bits.append(f'24h {"同池" if fact.get("poolVolume24hUsd") else "资产"}成交 {_money(volume["value"], lang)}')
        if ratio is not None:
            bits.append(f'成交/流动性 {ratio:.1f} 倍')
        if activity:
            bits.append(f'买 {activity["buys"]:g} / 卖 {activity["sells"]:g} 笔')
        market = '行情：' + ('；'.join(bits) if bits else '成交与买卖笔数未读取') + '。'
        risk = '风险：' + ('；'.join(findings[:2]) if findings else '、'.join(unknown[:3])+'未能识别' if unknown else '已检项目未见异常') + '。'
    else:
        relation = f'Pair: {dex}, {ticker}' + (f', liquidity {_money(liquidity["value"], lang)}.' if liquidity else ', liquidity unknown.')
        bits = [f'24h {_money(volume["value"], lang)}' ] if volume else []
        if ratio is not None:
            bits.append(f'{ratio:.1f}x liquidity')
        if activity:
            bits.append(f'buy {activity["buys"]:g}/sell {activity["sells"]:g}')
        market = 'Market: ' + ('; '.join(bits) if bits else 'volume/activity unknown') + '.'
        risk = 'Risk: ' + ('; '.join(findings[:2]) if findings else ', '.join(unknown[:2])+' unknown' if unknown else 'tested items clear') + '.'
    return [relation, market, risk]


def validate_model_text(value, fact, lang):
    """Legacy callers can validate only the exact deterministic three sentences.

    Free-form rewriting is disabled. Tax labels such as 买入税 are facts, not
    investment advice, so the old substring ban is not applied to templates.
    """
    expected = template_lines(fact, lang)
    return expected if isinstance(value, str) and value.strip() == '\n'.join(expected) else None
