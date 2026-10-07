"""Explicit security identities, separate from on-chain issuer verification."""
import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

US_UNDERLYINGS = frozenset("AAPL TSLA NVDA GOOG GOOGL AMZN META MSFT AMD INTC COIN PLTR SOFI HIMS GME AMC DJT MSTR RIVN LCID OPEN AI NKE DIS NFLX BA GM SBUX MCD NIO XPEV LI BABA JD PDD BIDU NTES TME IQ HOOD QQQ SLV SPY GLD TSM NOK MRNA SNDK CRCL SOXL SOXS TQQQ SQQQ BMNR".split())


@lru_cache(maxsize=1)
def hk_securities():
    path = Path(__file__).resolve().parents[2] / 'src/catalogues/hk-underlyings.v1.json'
    saved = json.loads(path.read_text())
    if saved.get('version') != 'hk-underlyings-v1':
        raise ValueError('unknown security mapping version')
    return saved['securities']


def equity_identity(row):
    from .stock_identity import token_identity
    issuer = token_identity(row.get('chainId') or row.get('chainIndex'), row.get('tokenContractAddress'))
    raw = str(issuer.get('ticker') or row.get('stockCode') or '').strip().upper()
    code = str(int(raw)) if raw.isdigit() else raw
    previous = dict(row.get('stockIdentity') or {})
    entry = hk_securities().get(code)
    # A known issuer deployment supplies its token symbol; an arbitrary
    # numeric ticker never establishes a Hong Kong security identity.
    symbol = str(issuer.get('tokenSymbol') or row.get('tokenSymbol') or '').upper()
    if entry and symbol == entry['symbol']:
        return {'id': 'XHKG:'+code.zfill(5), 'code': code, 'marketCode': code.zfill(5),
                'market': 'HKEX', 'currency': 'HKD', 'nameZh': entry['zh'], 'nameEn': entry['en'],
                'status': 'identified', 'source': entry['source']}
    if code in US_UNDERLYINGS:
        return {**previous, 'id': code, 'code': code, 'market': 'US', 'currency': 'USD',
                'status': 'identified', 'source': 'curated-underlying-market-v1'}
    return previous or {'id': 'unresolved:'+raw, 'code': raw, 'market': None, 'status': 'unresolved'}


def clock_session(identity, now):
    """Only prove closed hours; weekdays need a holiday calendar to be open."""
    market = identity.get('market')
    if market not in ('HKEX', 'US'):
        return None
    stamp = datetime.fromtimestamp(now/1000, ZoneInfo('Asia/Hong_Kong' if market == 'HKEX' else 'America/New_York'))
    minute = stamp.hour*60+stamp.minute
    reason = ('weekend' if stamp.weekday() >= 5 else
              'outside-session-hours' if minute < 570 or minute >= 960 else
              'midday-break' if market == 'HKEX' and 720 <= minute < 780 else None)
    return {'status': 'closed', 'asOf': now, 'source': 'exchange-clock', 'reason': reason} if reason else None
