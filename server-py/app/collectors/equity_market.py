"""Bounded public HK reference quotes and unadjusted daily exchange bars.

One batched quote request per minute, at most two historical requests per
round. Public references never claim licensed realtime or a known delay.
The worker atomically publishes a small file; API requests do no upstream IO.
"""
import asyncio
import json
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from ..equity_identity import hk_securities

SNAPSHOT = 'data/equity-market.json'
HISTORY_TTL_MS = 1_200_000
_lock = asyncio.Lock()


def number(value, minimum=0):
    if isinstance(value, bool) or value in (None, '', '-'):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= minimum else None
    except (ValueError, TypeError):
        return None


def normalize_quotes(text, requested, now):
    out = {}
    for market_code, payload in re.findall(r'v_hk(\d{5})="([^"\r\n]*)";', text):
        code = str(int(market_code))
        if code not in requested:
            continue
        fields = payload.split('~')
        if (len(fields) < 38 or fields[0] != '100' or fields[2] != market_code
                or 'HKD' not in fields[38:]):
            continue
        try:
            at = int(datetime.strptime(fields[30], '%Y/%m/%d %H:%M:%S').replace(tzinfo=ZoneInfo('Asia/Hong_Kong')).timestamp()*1000)
        except ValueError:
            continue
        price = number(fields[3], 1e-12)
        if price is None or not 0 < at <= now+60_000:
            continue
        out['XHKG:'+market_code] = {'id': 'XHKG:'+market_code, 'symbol': code.zfill(4)+'.HK',
            'provider': 'Tencent Finance', 'scope': 'equity-exchange', 'currency': 'HKD',
            'price': price, 'marketAt': at, 'observedAt': now, 'identityVerified': True,
            'realtime': False, 'delayMs': None, 'delayStatus': 'not-confirmed',
            'change24h': number(fields[32], -1e9), 'previousClose': number(fields[4], 1e-12),
            'open': number(fields[5], 1e-12), 'high': number(fields[33], 1e-12), 'low': number(fields[34], 1e-12),
            'volume': number(fields[36]), 'volumeUnit': 'shares', 'turnover': number(fields[37]),
            'turnoverCurrency': 'HKD', 'sourceUrl': 'https://gu.qq.com/hk'+market_code}
    return out


def normalize_history(body, code, now):
    data = body.get('data') if isinstance(body, dict) else None
    if (not isinstance(data, dict) or data.get('market') != 116
            or data.get('code') != code.zfill(5) or not isinstance(data.get('klines'), list)):
        raise ValueError('security-history-identity-mismatch')
    rows = {}
    for raw in data['klines'][-140:]:
        if not isinstance(raw, str):
            continue
        f = raw.split(',')
        if len(f) < 7:
            continue
        try:
            at = int(datetime.strptime(f[0], '%Y-%m-%d').replace(hour=9, minute=30, tzinfo=ZoneInfo('Asia/Hong_Kong')).timestamp()*1000)
        except ValueError:
            continue
        o, c, h, l = (number(f[i], 1e-12) for i in (1, 2, 3, 4))
        if (None in (o, c, h, l) or not 0 < at <= now or not l <= min(o, c) <= max(o, c) <= h):
            continue
        rows[at] = {'t': at, 'o': o, 'c': c, 'h': h, 'l': l,
                    'volumeShares': number(f[5]), 'turnover': number(f[6])}
    if not rows:
        raise ValueError('no-valid-equity-history')
    return {'id': 'XHKG:'+code.zfill(5), 'symbol': code.zfill(4)+'.HK', 'currency': 'HKD',
            'provider': 'Eastmoney', 'bar': '1D', 'adjustment': 'unadjusted', 'realtime': False,
            'observedAt': now, 'rows': [rows[t] for t in sorted(rows)],
            'sourceUrl': 'https://quote.eastmoney.com/hk/'+code.zfill(5)+'.html'}


def normalize_tencent_history(body, code, now):
    """Accept only the unadjusted day series of the requested HK security."""
    market_code = code.zfill(5)
    key = 'hk'+market_code
    if not isinstance(body, dict) or not isinstance(body.get('data'), dict):
        raise ValueError('security-history-identity-mismatch')
    data = body['data'].get(key)
    quote = (data.get('qt') or {}).get(key) if isinstance(data, dict) else None
    if (body.get('code') != 0 or not isinstance(quote, list) or len(quote) < 39
            or quote[0] != '100' or quote[2] != market_code or 'HKD' not in quote[38:]
            or not isinstance(data.get('day'), list)):
        raise ValueError('security-history-identity-mismatch')
    # day contains O/C/H/L and reported shares; qfqday/hfqday are deliberately
    # excluded. No inferred turnover or filled candle is added.
    klines = [','.join(str(v) for v in row[:6])+',' for row in data['day']
              if isinstance(row, list) and len(row) >= 6]
    result = normalize_history({'data': {'market': 116, 'code': market_code, 'klines': klines}}, code, now)
    result.update(provider='Tencent Finance', sourceUrl='https://gu.qq.com/'+key)
    return result


def read_snapshot():
    try:
        path = Path(SNAPSHOT)
        # Up to 79 verified HK securities each keep 120 daily bars.
        if path.stat().st_size <= 4_000_000:
            return json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    return {'quotes': {}, 'history': {}, 'attempts': {}}


async def refresh_equity_market():
    async with _lock:
        saved = read_snapshot()
        quotes, history, attempts = (dict(saved.get(k) or {}) for k in ('quotes', 'history', 'attempts'))
        now = int(time.time()*1000)
        codes = sorted(hk_securities(), key=lambda code: (code not in ('1024', '9992'), code))
        accepted, failed = 0, 0
        async with httpx.AsyncClient(timeout=8, follow_redirects=True,
                                     headers={'User-Agent': 'Mozilla/5.0'}) as client:
            try:
                response = await client.get('https://qt.gtimg.cn/q='+','.join('hk'+c.zfill(5) for c in codes))
                response.raise_for_status()
                fresh = normalize_quotes(response.content.decode('gb18030'), codes, now)
                for key, q in fresh.items():
                    if q['marketAt'] >= (quotes.get(key) or {}).get('marketAt', 0):
                        quotes[key] = q
                accepted += len(fresh)
                attempts['quotes'] = {'at': now, 'status': 'partial' if len(fresh) != len(codes) else 'ok', 'accepted': len(fresh)}
            except (httpx.HTTPError, ValueError, UnicodeError) as exc:
                failed += 1
                attempts['quotes'] = {'at': now, 'status': 'unavailable', 'reason': type(exc).__name__}
            due = [c for c in codes if now-(history.get('XHKG:'+c.zfill(5)) or {}).get('observedAt', 0) >= HISTORY_TTL_MS
                   and now-(attempts.get('XHKG:'+c.zfill(5)) or {}).get('at', 0) >= 60_000]
            due.sort(key=lambda c: (attempts.get('XHKG:'+c.zfill(5)) or {}).get('at', 0))
            for code in due[:2]:
                key = 'XHKG:'+code.zfill(5)
                previous = attempts.get(key) or {}
                provider = 'Eastmoney' if previous.get('status') == 'unavailable' and previous.get('provider') == 'Tencent Finance' else 'Tencent Finance'
                try:
                    if provider == 'Tencent Finance':
                        response = await client.get('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',
                                                    params={'param': 'hk'+code.zfill(5)+',day,,,120,'})
                    else:
                        response = await client.get('https://push2his.eastmoney.com/api/qt/stock/kline/get', params={
                            'secid': '116.'+code.zfill(5), 'klt': '101', 'fqt': '0', 'lmt': '120',
                            'end': '20500101', 'fields1': 'f1,f2,f3,f4,f5,f6',
                            'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'})
                    response.raise_for_status()
                    normalize = normalize_tencent_history if provider == 'Tencent Finance' else normalize_history
                    history[key] = normalize(response.json(), code, now)
                    attempts[key] = {'at': now, 'status': 'ok', 'provider': provider}
                    accepted += 1
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    attempts[key] = {'at': now, 'status': 'unavailable', 'provider': provider, 'reason': type(exc).__name__}
                    failed += 1
        output = {'version': 1, 'updatedAt': now, 'quotes': quotes, 'history': history, 'attempts': attempts,
                  'status': 'partial' if failed or len(quotes) < len(codes) else 'ok',
                  'scope': 'public-equity-reference', 'realtime': False}
        path = Path(SNAPSHOT)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(output, ensure_ascii=False, separators=(',', ':')))
        os.replace(temporary, path)
        return {'accepted': accepted, 'failed': failed, 'requested': 1+sum(v.get('at') == now for k,v in attempts.items() if k != 'quotes')}
