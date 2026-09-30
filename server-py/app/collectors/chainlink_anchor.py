"""Narrow, independently dated BNB Chain USD oracle observations.

Only the three exact, reviewed quote-token contracts can consume their paired
feeds. The RPC read shares the pool stream's public-node gate; no swap or HTTP
request waits for an oracle call. Samples never publish a headline asset price.
"""
import asyncio
import json
import time
from decimal import Decimal

from ..db import retry_busy_write, store


CHAIN_ID = '56'
WBNB = '0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c'
USDT = '0x55d398326f99059ff775485246999027b3197955'
USDC = '0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d'
FEED = '0x0567f2323251f0aab15c8dfb1967e4e8a7d42aee'
USDT_FEED = '0xb97ad0e74fa7d920791e90258a6e2085088b4320'
USDC_FEED = '0x51597f405303c4377e36123cbc172b13269ea163'
DESCRIPTION = 'BNB / USD'
DECIMALS = 8
KIND = 'oracle-feed'
PROVIDER = 'Chainlink'
SOURCE_URL = 'https://data.chain.link/feeds/bsc/mainnet/bnb-usd'
FEEDS = {
    WBNB: {'feed': FEED, 'description': DESCRIPTION, 'sourceUrl': SOURCE_URL},
    USDT: {'feed': USDT_FEED, 'description': 'USDT / USD',
           'sourceUrl': 'https://data.chain.link/feeds/bsc/mainnet/usdt-usd'},
    USDC: {'feed': USDC_FEED, 'description': 'USDC / USD',
           'sourceUrl': 'https://data.chain.link/feeds/bsc/mainnet/usdc-usd'},
}
MAX_QUOTE_AGE_MS = 15 * 60_000
HISTORY_MS = 48 * 60 * 60_000
MAX_SAMPLES = 576
MAX_WINDOW_ROWS = 16
BACKLOG_WAIT_STEPS = 10
BACKLOG_WAIT_SECONDS = 1
FORCE_SAMPLE_AGE_MS = 10 * 60_000
MAX_ROUND_RPC_CALLS = 12


class OracleDeferred(RuntimeError):
    """Live pool logs have priority over the periodic oracle observation."""


def _integer(value):
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, str)) or isinstance(value, str) and not value:
        return None
    try:
        number = int(value, 16) if isinstance(value, str) and value.startswith('0x') else int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _bytes(raw):
    if not isinstance(raw, str) or not raw.startswith('0x') or len(raw) % 2:
        return None
    try:
        return bytes.fromhex(raw[2:])
    except ValueError:
        return None


def _description(raw):
    value = _bytes(raw)
    if value is None or len(value) < 64 or int.from_bytes(value[:32], 'big') != 32:
        return None
    size = int.from_bytes(value[32:64], 'big')
    if size <= 0 or size > 64 or len(value) != 64 + ((size + 31) // 32) * 32:
        return None
    try:
        return value[64:64 + size].decode('ascii')
    except UnicodeDecodeError:
        return None


def _single_word(raw):
    value = _bytes(raw)
    return int.from_bytes(value, 'big') if value is not None and len(value) == 32 else None


def _round(raw, observed_at):
    value = _bytes(raw)
    if value is None or len(value) != 32 * 5:
        return None
    round_id, unsigned_answer, started, updated, answered = (
        int.from_bytes(value[i:i + 32], 'big') for i in range(0, 160, 32))
    answer = unsigned_answer - (1 << 256) if unsigned_answer >= (1 << 255) else unsigned_answer
    # A reverted, incomplete, future-dated, or non-positive round is not a
    # market price. Never substitute the local receipt timestamp.
    if (round_id <= 0 or answered < round_id or answer <= 0 or started <= 0
            or updated < started or updated * 1000 > observed_at):
        return None
    return {'roundId': str(round_id), 'answeredInRound': str(answered),
            'answer': str(answer), 'startedAt': started * 1000,
            'updatedAt': updated * 1000}


def _backlogged(stream):
    queue = getattr(stream, 'queue', None)
    if queue is None:
        return True
    pending = queue.qsize()
    # A live stream normally has both a shallow queue and an active processor.
    # Skipping that ordinary state would starve every five-minute oracle run.
    return pending >= 32


async def _wait_for_backlog(stream, steps):
    """Return (clear, seconds waited) within the round's shared wait budget."""
    if not _backlogged(stream):
        return True, 0
    for waited in range(steps):
        await asyncio.sleep(BACKLOG_WAIT_SECONDS)
        if not _backlogged(stream):
            return True, waited + 1
    return False, steps


async def _force_due(scoped, now):
    """Use persisted sample receipt times so restarts cannot reset deferral."""
    statuses = await latest_sample_status(scoped)
    return any((not isinstance(row['observedAt'], int)
                or row['observedAt'] > now
                or now - row['observedAt'] >= FORCE_SAMPLE_AGE_MS)
               for row in statuses.values())


async def verified_sample(rpc, *, quote_token=WBNB, observed_at=None, should_pause=None):
    """Read one verified proxy round through the stream's bounded RPC gate."""
    spec = FEEDS.get(quote_token)
    if spec is None:
        return None
    async def call(method, params):
        if should_pause is not None and should_pause():
            raise OracleDeferred('live-backlog')
        return await rpc(method, params)

    chain = _integer(await call('eth_chainId', []))
    if chain != 56:
        return None
    target = {'to': spec['feed']}
    description = _description(await call('eth_call', [{**target, 'data': '0x7284e416'}, 'latest']))
    decimals = _single_word(await call('eth_call', [{**target, 'data': '0x313ce567'}, 'latest']))
    if description != spec['description'] or decimals != DECIMALS:
        return None
    raw_round = await call('eth_call', [{**target, 'data': '0xfeaf968c'}, 'latest'])
    # This local time describes receipt only. The market timestamp stays the
    # feed's updatedAt, including when a round changes during these RPC calls.
    observed_at = int(time.time() * 1000) if observed_at is None else observed_at
    round_data = _round(raw_round, observed_at)
    if not round_data:
        return None
    return {
        'chainId': CHAIN_ID, 'quoteToken': quote_token, 'feed': spec['feed'],
        'description': spec['description'], 'decimals': DECIMALS, **round_data,
        'observedAt': observed_at, 'currency': 'USD', 'provider': PROVIDER,
        'independent': True, 'sourceUrl': spec['sourceUrl'],
    }


def _valid_sample(sample):
    if not isinstance(sample, dict):
        return False
    spec = FEEDS.get(sample.get('quoteToken'))
    if spec is None:
        return False
    if (sample.get('chainId') != CHAIN_ID or sample.get('feed') != spec['feed']
            or sample.get('description') != spec['description']
            or sample.get('decimals') != DECIMALS or sample.get('currency') != 'USD'
            or sample.get('provider') != PROVIDER or sample.get('independent') is not True
            or sample.get('sourceUrl') != spec['sourceUrl']):
        return False
    if any(not isinstance(sample.get(field), int) or isinstance(sample.get(field), bool)
           for field in ('startedAt', 'updatedAt', 'observedAt')):
        return False
    updated = _integer(sample.get('updatedAt'))
    started = _integer(sample.get('startedAt'))
    observed = _integer(sample.get('observedAt'))
    round_id = _integer(sample.get('roundId'))
    answered = _integer(sample.get('answeredInRound'))
    answer = _integer(sample.get('answer'))
    return (updated is not None and updated > 0 and started is not None and 0 < started <= updated
            and observed is not None and updated <= observed and round_id is not None
            and round_id > 0 and answered is not None and answered >= round_id
            and answer is not None and answer > 0)


def _sample_id(updated_at, feed=FEED):
    return feed + ':' + f'{updated_at:013d}'


async def save_sample(scoped, sample):
    """Persist one round per feed and indexed fact, retaining 48h/576 rows."""
    if scoped.scope != CHAIN_ID or not _valid_sample(sample):
        return False
    feed = FEEDS[sample['quoteToken']]['feed']

    async def write():
        async with scoped._guard_write():
            await scoped.db.execute('BEGIN IMMEDIATE')
            try:
                rows = await scoped.db.execute_fetchall(
                    'SELECT id,body FROM facts WHERE kind=? AND id BETWEEN ? AND ? '
                    'ORDER BY id DESC LIMIT 1',
                    (scoped.key(KIND), _sample_id(0, feed),
                     _sample_id(9_999_999_999_999, feed)))
                if rows:
                    try:
                        latest = json.loads(rows[0][1])
                    except (TypeError, ValueError):
                        latest = None
                    # An unknown/corrupted round cannot authorize moving the
                    # feed cursor. Equal or older rounds are durable no-ops.
                    if (not _valid_sample(latest)
                            or latest['quoteToken'] != sample['quoteToken']
                            or rows[0][0] != _sample_id(latest['updatedAt'], feed)
                            or int(sample['roundId']) <= int(latest['roundId'])
                            or sample['updatedAt'] <= latest['updatedAt']):
                        await scoped.db.rollback()
                        return False
                await scoped.db.execute('INSERT INTO facts(kind,id,body) VALUES (?,?,?)',
                                        (scoped.key(KIND), _sample_id(sample['updatedAt'], feed),
                                         json.dumps(sample, ensure_ascii=False, separators=(',', ':'))))
                cutoff = sample['updatedAt'] - HISTORY_MS
                if cutoff > 0:
                    await scoped.db.execute(
                        'DELETE FROM facts WHERE kind=? AND id BETWEEN ? AND ?',
                        (scoped.key(KIND), _sample_id(0, feed), _sample_id(cutoff - 1, feed)))
                overflow = await scoped.db.execute_fetchall(
                    'SELECT id FROM facts WHERE kind=? AND id BETWEEN ? AND ? '
                    'ORDER BY id DESC LIMIT 1 OFFSET ?',
                    (scoped.key(KIND), _sample_id(0, feed),
                     _sample_id(9_999_999_999_999, feed),
                     MAX_SAMPLES))
                if overflow:
                    await scoped.db.execute(
                        'DELETE FROM facts WHERE kind=? AND id BETWEEN ? AND ?',
                        (scoped.key(KIND), _sample_id(0, feed), overflow[0][0]))
                await scoped.db.commit()
                return True
            except BaseException:
                await scoped.db.rollback()
                raise

    return await retry_busy_write(write)


async def dated_quote_rate(db, scoped, quote_token, trade_at):
    """Use only this exact token's prior, sufficiently recent oracle round."""
    spec = FEEDS.get(quote_token)
    if (spec is None or scoped.scope != CHAIN_ID or not isinstance(trade_at, int)
            or isinstance(trade_at, bool) or trade_at <= 0):
        return None
    feed = spec['feed']
    rows = await db.execute_fetchall(
        'SELECT id,body FROM facts WHERE kind=? AND id BETWEEN ? AND ? '
        'ORDER BY id DESC LIMIT ?',
        (scoped.key(KIND), _sample_id(max(0, trade_at - MAX_QUOTE_AGE_MS), feed),
         _sample_id(trade_at, feed), MAX_WINDOW_ROWS + 1))
    if not rows or len(rows) > MAX_WINDOW_ROWS:
        return None
    for row in rows:
        try:
            sample = json.loads(row[1])
        except (TypeError, ValueError):
            continue
        if (not _valid_sample(sample) or sample['quoteToken'] != quote_token
                or row[0] != _sample_id(sample['updatedAt'], feed)):
            continue
        updated = sample['updatedAt']
        if 0 <= trade_at - updated <= MAX_QUOTE_AGE_MS:
            try:
                rate = Decimal(sample['answer']) / Decimal(10) ** DECIMALS
            except (ArithmeticError, TypeError, ValueError):
                continue
            if rate.is_finite() and rate > 0:
                return updated, rate
    return None


async def dated_wbnb_rate(db, scoped, trade_at):
    """Compatibility entry point for callers valuing canonical BNB WBNB."""
    return await dated_quote_rate(db, scoped, WBNB, trade_at)


async def latest_sample_status(scoped):
    """Bounded operator view: most recent feed update per reviewed token."""
    if scoped.scope != CHAIN_ID:
        return {}
    status = {}
    for token, spec in FEEDS.items():
        rows = await scoped.db.execute_fetchall(
            'SELECT id,body FROM facts WHERE kind=? AND id BETWEEN ? AND ? '
            'ORDER BY id DESC LIMIT 1',
            (scoped.key(KIND), _sample_id(0, spec['feed']),
             _sample_id(9_999_999_999_999, spec['feed'])))
        sample = None
        if rows:
            try:
                sample = json.loads(rows[0][1])
            except (TypeError, ValueError):
                pass
        valid = (_valid_sample(sample) and sample['quoteToken'] == token
                 and rows[0][0] == _sample_id(sample['updatedAt'], spec['feed']))
        status[token] = {'feed': spec['feed'], 'updatedAt': sample['updatedAt'] if valid else None,
                         'observedAt': sample['observedAt'] if valid else None}
    return status


async def refresh_bnb_anchor():
    """Independent five-minute oracle task, bounded even under live backlog."""
    from .chain_stream import stream_for

    stream = stream_for(CHAIN_ID)
    totals = {'requested': 0, 'accepted': 0, 'updated': 0,
              'failed': 0, 'unsupported': 0, 'skipped': 0}
    if stream is None:
        totals['skipped'] = len(FEEDS)
        return totals
    clear, waited = await _wait_for_backlog(stream, BACKLOG_WAIT_STEPS)
    scoped = await store(CHAIN_ID)
    force = not clear and await _force_due(scoped, int(time.time() * 1000))
    if not clear and not force:
        totals['skipped'] = len(FEEDS)
        return totals

    calls = 0

    async def budgeted_rpc(method, params):
        nonlocal calls
        if calls >= MAX_ROUND_RPC_CALLS:
            raise RuntimeError('oracle-rpc-round-budget')
        calls += 1
        return await stream.rpc(method, params)

    for index, token in enumerate(FEEDS):
        remaining = len(FEEDS) - index
        if not force:
            clear, just_waited = await _wait_for_backlog(
                stream, BACKLOG_WAIT_STEPS - waited)
            waited += just_waited
        if not force and not clear:
            force = await _force_due(scoped, int(time.time() * 1000))
            if not force:
                totals['skipped'] += remaining
                break
        try:
            async with asyncio.timeout(15):
                # Once a feed is admitted, finish its four bounded calls.
                # Rechecking the queue between eth_calls caused repeated
                # partial reads at the same traffic peak every five minutes.
                sample = await verified_sample(budgeted_rpc, quote_token=token)
        except OracleDeferred:
            totals['skipped'] += remaining
            break
        except Exception:
            totals['requested'] += 1
            totals['failed'] += 1
            continue
        totals['requested'] += 1
        if sample is None:
            totals['unsupported'] += 1
            continue
        try:
            changed = await save_sample(scoped, sample)
            if not changed:
                # Duplicate rounds are healthy; an invalid/corrupt fact or
                # backwards provider response is not a durable observation.
                stored = await scoped.get(KIND, _sample_id(sample['updatedAt'],
                                                         FEEDS[token]['feed']))
                if (not _valid_sample(stored)
                        or stored['quoteToken'] != token
                        or stored['roundId'] != sample['roundId']
                        or stored['answer'] != sample['answer']):
                    totals['failed'] += 1
                    continue
        except Exception:
            totals['failed'] += 1
            continue
        age = sample['observedAt'] - sample['updatedAt']
        if age > MAX_QUOTE_AGE_MS:
            totals['unsupported'] += 1
            continue
        totals['accepted'] += 1
        totals['updated'] += int(changed)
    return totals
