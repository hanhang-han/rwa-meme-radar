"""Evidence-bound facts and short asset readouts from a published revision.

The readout deliberately has no generated numbers.  An optional model may
rephrase these four lines only after its output passes the numeric and wording
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
LABELS = {'zh': ('关系', '行情', '风险', '数据'),
          'en': ('Pair', 'Market', 'Risk', 'Data')}
RISK_LABELS = {
    'zh': {'tax': '税费', 'permissions': '合约权限', 'liquidityLock': 'LP 状态',
           'concentration': '前 10 持仓', 'turnover': '成交异常', 'thinSpike': '低深度波动'},
    'en': {'tax': 'tax', 'permissions': 'permissions', 'liquidityLock': 'LP',
           'concentration': 'top holders', 'turnover': 'turnover', 'thinSpike': 'thin spike'},
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
        'concentration': safety.get('concentration'),
        'turnover': checks.get('wash_suspect'),
        'thinSpike': checks.get('thin_spike'),
    }
    result = {}
    for key, check in raw.items():
        state = (check or {}).get('status')
        result[key] = {'status': state if state in ('clear', 'triggered') else 'unknown',
                       'source': (check or {}).get('provider'),
                       'observedAt': (check or {}).get('checkedAt')}
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
    missing = [name for name, value in (
        ('price', price), ('change5m', None), ('change1h', None),
        ('change24h', change_24h), ('volume24h', volume_24h),
        ('poolLiquidity', pool_liquidity), ('volumeLiquidityRatio', ratio),
        ('uniqueTraderAddresses', None), ('top10AdjustedPercent', top10),
        ('creatorHoldingPercent', None), ('kolMentions24h', None),
    ) if value is None]
    fact = {
        'version': 1, 'chainId': str(chain), 'chain': CHAIN_NAMES.get(str(chain)),
        'address': address.lower(), 'name': asset.get('name'), 'symbol': asset.get('symbol'),
        'stockTicker': ticker,
        'relation': {'type': 'official-stock-pool', 'pool': str(relation['pool']).lower(),
                     'dex': relation.get('dex') or relation.get('venue'),
                     'createdAt': relation_at,
                     'issuerSourceUrl': identity.get('sourceUrl')},
        'priceUsd': price, 'change5m': None, 'change1h': None,
        'change24hPercent': change_24h, 'volume24hUsd': volume_24h,
        'poolLiquidityUsd': pool_liquidity, 'poolVolume24hUsd': pool_volume,
        'volumeLiquidityRatio': ratio,
        'uniqueTraderAddresses': None, 'top10AdjustedPercent': top10,
        'creatorHoldingPercent': None, 'kolMentions24h': None,
        'risks': risks, 'missing': missing, 'dataAsOf': data_as_of,
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
    lang = 'en' if lang == 'en' else 'zh'
    name = str(fact.get('symbol') or fact.get('name') or fact['address'][:8])[:10]
    ticker = str(fact.get('stockTicker') or ('stock token' if lang == 'en' else '股票代币'))[:12]
    pool_volume = fact.get('poolVolume24hUsd')
    volume = pool_volume or fact.get('volume24hUsd')
    change = fact.get('change24hPercent')
    ratio = fact.get('volumeLiquidityRatio')
    flags = [RISK_LABELS[lang][key] for key, value in fact['risks'].items()
             if value['status'] == 'triggered']
    unknown = [RISK_LABELS[lang][key] for key, value in fact['risks'].items()
               if value['status'] == 'unknown']
    source = next((item.get('source') for item in (volume, change, fact.get('priceUsd'))
                   if item and item.get('source')), None)
    unavailable = ({'price': '价格', 'change5m': '5m涨跌', 'change1h': '1h涨跌',
                    'change24h': '24h涨跌', 'volume24h': '成交',
                    'poolLiquidity': '池流动性', 'volumeLiquidityRatio': '同池周转',
                    'uniqueTraderAddresses': '交易地址', 'top10AdjustedPercent': '前10持仓',
                    'creatorHoldingPercent': '创建者持仓', 'kolMentions24h': '社媒提及'}
                   if lang == 'zh' else
                   {'price': 'price', 'change5m': '5m', 'change1h': '1h',
                    'change24h': '24h', 'volume24h': 'volume',
                    'poolLiquidity': 'pool TVL', 'volumeLiquidityRatio': 'turnover',
                    'uniqueTraderAddresses': 'traders', 'top10AdjustedPercent': 'top10',
                    'creatorHoldingPercent': 'creator', 'kolMentions24h': 'social'})
    # Surface the consequential gaps before the optional short-window chart
    # fields, which are currently absent for almost every asset.
    gap_order = ('uniqueTraderAddresses', 'top10AdjustedPercent',
                 'creatorHoldingPercent', 'poolLiquidity', 'volume24h',
                 'price', 'change24h', 'kolMentions24h', 'change5m', 'change1h')
    important_missing = [unavailable[key] for key in gap_order if key in fact['missing']]
    missing_text = ('、'.join(important_missing[:2]) + ('等' if len(fact['missing']) > 2 else '')
                    if important_missing else None)
    if lang == 'zh':
        relation = f'关系：{name}与{ticker}代币有链上配对池。'
        market_bits = [f'24h {change["value"]:+.1f}%' if change else '24h涨跌暂无数据',
                       f'{"该池成交" if pool_volume else "成交"}{_money(volume["value"], lang)}'
                       if volume else '成交暂无数据']
        if ratio is not None:
            market_bits.append(f'同池周转{ratio:.1f}倍')
        market = '行情：' + '，'.join(market_bits) + '。'
        if len(market) > 40 and ratio is not None:
            market = '行情：' + '，'.join(market_bits[:2]) + '。'
        if len(market) > 40:
            market = '行情：具体涨跌与成交见详情。'
        risk = ('风险：' + '、'.join(flags[:2]) + '需留意。') if flags else (
            '风险：' + '、'.join(unknown[:2]) + '未能识别。' if unknown else '风险：已检项未触发阈值。')
        source_text = str(source or '来源暂无')[:12]
        data = (f'数据：{source_text} {_clock(fact["dataAsOf"])}；{missing_text}暂无数据。'
                if fact.get('dataAsOf') and missing_text else
                f'数据：{source_text}，基于{_clock(fact["dataAsOf"])}。'
                if fact.get('dataAsOf') else f'数据：{source_text}；行情时间暂无数据。')
    else:
        relation = f'Pair: {name} has a {ticker} pool.'
        market = 'Market: ' + (f'24h {change["value"]:+.1f}%' if change else '24h N/A') + (
            f', {_money(volume["value"], lang)} {"pool volume" if pool_volume else "volume"}.'
            if volume else ', volume N/A.')
        risk = ('Risk: ' + ', '.join(flags[:2]) + ' flagged.' if flags else
                'Risk: ' + ', '.join(unknown[:2]) + ' unknown.' if unknown else
                'Risk: tested items clear.')
        data = (f'Data: {str(source or "source N/A")[:8]} {_clock(fact["dataAsOf"])}; {important_missing[0]} N/A.'
                if fact.get('dataAsOf') and important_missing else
                f'Data: {str(source or "source N/A")[:10]}, {_clock(fact["dataAsOf"])} CST.'
                if fact.get('dataAsOf') else f'Data: {str(source or "source N/A")[:10]}, time N/A.')
        if len(market) > 40:
            market = 'Market: price and volume in details.'
    if len(relation) > 40:
        relation = '关系：该币与股票代币有链上配对池。' if lang == 'zh' else 'Pair: this token has a stock pool.'
    if len(risk) > 40:
        risk = '风险：逐项结果见详情。' if lang == 'zh' else 'Risk: see individual checks.'
    if len(data) > 40:
        data = (f'数据：{_clock(fact["dataAsOf"])}；缺失项见详情。' if lang == 'zh' and fact.get('dataAsOf')
                else '数据：来源时间暂无数据。' if lang == 'zh'
                else 'Data: gaps in details.')
    return [relation, market, risk, data]


def validate_model_text(value, fact, lang):
    """Reject unsupported numerals, banned claims and structural drift."""
    if not isinstance(value, str):
        return None
    lines = [line.strip() for line in value.strip().splitlines()]
    labels = LABELS['en' if lang == 'en' else 'zh']
    if len(lines) != 4 or any(len(line) > 40 or not line.startswith(label + (':' if lang == 'en' else '：'))
                              for line, label in zip(lines, labels)):
        return None
    if any(word in value.lower() for word in BLOCKED):
        return None
    template = template_lines(fact, lang)
    # Relationship and risk statements are evidence classifications.  The
    # model cannot safely reinterpret those states in free text.
    if lines[0] != template[0] or lines[2] != template[2]:
        return None
    # Names, ticker, timestamps and all rounded display values are covered by
    # the deterministic readout.  A model can omit facts but cannot add digits.
    allowed = set(NUMERIC.findall('\n'.join(template)))
    if not set(NUMERIC.findall(value)).issubset(allowed):
        return None
    return lines
