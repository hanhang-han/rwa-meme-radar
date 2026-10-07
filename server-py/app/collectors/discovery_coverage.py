"""Daily, bounded comparison with DexScreener's *indexed* BNB stock pools.

This is an external-index reconciliation, not proof that every on-chain pool
exists in DexScreener. A 30-row token result may be truncated, so it never
contributes to an unqualified coverage percentage.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable

import httpx

from ..db import store
from ..stock_identity import _manifest, chain_manifest_version
from .pool_gap_repair import KIND as GAP_KIND, candidate_id, queue_indexed_gap


ADDRESS = re.compile(r"0x[0-9a-f]{40}\Z", re.I)
CHAIN = "56"
NETWORK = "bsc"
PROVIDER = "DexScreener"
URL = "https://api.dexscreener.com/token-pairs/v1/bsc/"
REQUEST_SPACING_SECONDS = 0.6  # <=100/min, leaving headroom for quote enrichment.
SUSPECTED_RESULT_CAP = 30
REPORT_KIND = "discovery-coverage"
MAX_QUEUED_CANDIDATES = 4_000
NETWORKS = {"196": "xlayer", "56": "bsc", "4663": "robinhood", "5042": "arc"}
CANDIDATE_TTL_MS = 90 * 86_400_000


def _address(value) -> str | None:
    value = str(value or "").lower()
    return value if ADDRESS.fullmatch(value) else None


def official_contracts(chain: str = CHAIN) -> list[str]:
    """All current native/wrapper deployments, including inactive tickers."""
    _, index = _manifest()
    return sorted({address for (cid, address), identity in index.items()
                   if cid == chain and identity["eligibleForPair"]})


def normalize_pairs(rows, token: str, network: str = NETWORK) -> tuple[dict[str, dict], int, bool]:
    """Reject malformed/cross-chain results instead of inflating a score."""
    if not isinstance(rows, list):
        raise ValueError("invalid-provider-response")
    pairs: dict[str, dict] = {}
    malformed = 0
    for row in rows:
        if not isinstance(row, dict):
            malformed += 1
            continue
        base_token = row.get("baseToken")
        quote_token = row.get("quoteToken")
        if not isinstance(base_token, dict) or not isinstance(quote_token, dict):
            malformed += 1
            continue
        pool = _address(row.get("pairAddress"))
        base = _address(base_token.get("address"))
        quote = _address(quote_token.get("address"))
        if (row.get("chainId") != network or not pool or not base or not quote
                or base == quote or token not in (base, quote)):
            malformed += 1
            continue
        liquidity_row = row.get("liquidity")
        liquidity = liquidity_row.get("usd") if isinstance(liquidity_row, dict) else None
        if isinstance(liquidity, bool) or not isinstance(liquidity, (float, int)) or liquidity < 0:
            liquidity = None
        pairs[pool] = {"pool": pool, "token0": base, "token1": quote,
                       "stockSide": token, "liquidityUsd": liquidity,
                       "dexId": str(row.get("dexId") or "")[:64]}
    return pairs, malformed, len(rows) >= SUSPECTED_RESULT_CAP


def compare_pairs(indexed: dict[str, dict], local_pools: list[dict]) -> dict:
    """A local match requires both pool identity and on-chain token sides."""
    local = {}
    for row in local_pools:
        pool = _address(row.get("pool"))
        sides = {_address(row.get("token0")), _address(row.get("token1"))}
        if (pool and len(sides) == 2 and None not in sides
                and row.get("creationStatus") != "orphaned"
                and row.get("verificationStatus") != "reorged"):
            local[pool] = sides
    missing = [row for pool, row in indexed.items()
               if pool not in local or local[pool] != {row["token0"], row["token1"]}]
    missing.sort(key=lambda row: (-(row.get("liquidityUsd") or 0), row["pool"]))
    return {"eligible": len(indexed), "covered": len(indexed)-len(missing),
            "missing": len(missing), "samples": missing[:12]}


async def _fetch(client: httpx.AsyncClient, token: str, chain: str = CHAIN):
    if chain == '196':
        from .pool_market import GECKO_NETWORKS, gecko_pairs
        response = await client.get('https://api.geckoterminal.com/api/v2/networks/'
            + GECKO_NETWORKS[chain] + '/tokens/' + token + '/pools', params={'page': 1})
        if response.status_code == 404:
            return []  # Supported network, token absent from this public index.
        response.raise_for_status()
        return gecko_pairs(chain, response.json())
    response = await client.get("https://api.dexscreener.com/token-pairs/v1/" + NETWORKS[chain] + "/" + token)
    response.raise_for_status()
    return response.json()


async def reconcile_bnb(*, fetch: Callable[[str], Awaitable[object]] | None = None,
                        contracts: list[str] | None = None, request_spacing: float = REQUEST_SPACING_SECONDS,
                        now_ms: Callable[[], int] | None = None, chain: str = CHAIN,
                        max_contracts: int = 32) -> dict:
    """A bounded page of a durable scan; restart/cancellation repeats at most one token."""
    clock = now_ms or (lambda: int(time.time() * 1000))
    if chain not in NETWORKS or not 1 <= max_contracts <= 128:
        raise ValueError('unsupported-index-chain-or-bound')
    system = await store('system')
    subjects = sorted(set(contracts if contracts is not None else official_contracts(chain)))
    if not subjects or any(_address(subject) != subject for subject in subjects):
        raise ValueError('invalid-official-contracts')
    fingerprint = hashlib.sha256(json.dumps(subjects).encode()).hexdigest()
    local_store = await store(chain)
    local_pools = await local_store.all('pool')
    async with system._guard_write():
        await system.db.execute("""DELETE FROM facts WHERE kind=? AND id IN (
            SELECT id FROM facts WHERE kind=?
            AND CAST(json_extract(body,'$.lastIndexedAt') AS INTEGER)<? LIMIT 200)""",
            (system.key(GAP_KIND), system.key(GAP_KIND), clock() - CANDIDATE_TTL_MS))
        await system.db.commit()
    queued = len(await system.all(GAP_KIND))
    scan = await system.get('discovery-scan', chain) or {}
    if scan.get('fingerprint') != fingerprint or scan.get('manifestVersion') != chain_manifest_version(chain):
        scan = {}
    if scan.get('cursor', 0) >= len(subjects):
        if contracts is None and clock()-scan.get('completedAt', 0) < 86_400_000:
            return {'requested': 0, 'accepted': 0, 'updated': 0, 'failed': 0, 'skipped': 1}
        scan = {}
    started = clock()
    report = scan.get('report') or {
        'chainId': chain, 'status': 'partial', 'scope': 'dexscreener-indexed-official-stock-pools',
        'provider': 'GeckoTerminal' if chain == '196' else PROVIDER,
        'sourceUrl': 'https://apiguide.geckoterminal.com' if chain == '196' else 'https://docs.dexscreener.com/api/reference',
        'manifestVersion': chain_manifest_version(chain), 'startedAt': started,
        'checkedAt': None, 'updatedAt': started, 'subjectCount': len(subjects),
        'checkedContracts': 0, 'failedContracts': 0, 'cappedContracts': 0,
        'malformedPairs': 0, 'eligible': 0, 'covered': 0, 'missing': 0, 'samples': [],
        'queuedCandidates': 0, 'queueOverflow': 0, 'failureSamples': []}
    indexed = scan.get('indexed') or {}
    cursor = scan.get('cursor', 0)
    result = {'requested': 0, 'accepted': 0, 'updated': 0, 'failed': 0, 'unsupported': 0}
    local_sides = {_address(row.get('pool')): {_address(row.get('token0')), _address(row.get('token1'))}
        for row in local_pools if row.get('creationStatus') != 'orphaned' and row.get('verificationStatus') != 'reorged'}

    async def save():
        report.update(compare_pairs(indexed, await local_store.all('pool')))
        report.update(updatedAt=clock(), cursor=cursor, unqueriedContracts=len(subjects)-cursor,
                      maxContractsPerRound=max_contracts, resumable=True)
        report['status'] = ('complete' if cursor == len(subjects) and report['eligible'] > 0
            and not any(report[k] for k in ('failedContracts', 'cappedContracts', 'malformedPairs', 'queueOverflow'))
            else 'partial')
        report['coverageRatio'] = report['covered']/report['eligible'] if report['eligible'] and report['status'] == 'complete' else None
        report['observedMatchRatio'] = report['covered']/report['eligible'] if report['eligible'] else None
        report['passesCoverage95'] = report['coverageRatio'] is not None and report['coverageRatio'] >= .95
        if cursor == len(subjects):
            report['checkedAt'] = clock()
        # Save the cursor after each completed request, before yielding. Index
        # candidates are idempotent, so a crash between writes is safe to replay.
        await system.put('discovery-scan', chain, {'fingerprint': fingerprint,
            'manifestVersion': report['manifestVersion'], 'cursor': cursor, 'indexed': indexed,
            'report': report, 'completedAt': clock() if cursor == len(subjects) else None})
        await system.put(REPORT_KIND, chain, report)

    async def collect(get):
        nonlocal queued, cursor
        consecutive_failures = 0
        end = min(len(subjects), cursor+max_contracts)
        while cursor < end:
            token = subjects[cursor]
            result['requested'] += 1
            try:
                pairs, malformed, capped = normalize_pairs(await get(token), token, NETWORKS[chain])
                if chain == '196' and len(pairs) >= 20:
                    capped = True  # First public token-pool page; no completeness claim.
                indexed.update(pairs)
                for pool, pair in pairs.items():
                    pair = {**pair, 'chainId': chain, 'indexProvider': report['provider']}
                    ident = candidate_id(pair)
                    existing = await system.get(GAP_KIND, ident)
                    if local_sides.get(pool) == {pair['token0'], pair['token1']}:
                        if existing and existing.get('status') == 'awaiting-classification':
                            await system.patch_fact(GAP_KIND, ident, {'lastIndexedAt': clock()})
                        continue
                    if not existing and queued >= MAX_QUEUED_CANDIDATES:
                        report['queueOverflow'] += 1
                        continue
                    if await queue_indexed_gap(system, pair, seen_at=clock()):
                        queued += 1
                        report['queuedCandidates'] += 1
                report['checkedContracts'] += 1
                report['malformedPairs'] += malformed
                report['cappedContracts'] += int(capped)
                result['accepted'] += 1
                result['updated'] += len(pairs)
                result['unsupported'] += malformed + int(capped)
                consecutive_failures = 0
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                report['failedContracts'] += 1
                result['failed'] += 1
                consecutive_failures += 1
                if len(report['failureSamples']) < 5:
                    report['failureSamples'].append({'token': token, 'reason': type(exc).__name__})
            cursor += 1
            await save()
            if consecutive_failures >= 8:
                break
            if request_spacing and cursor < end:
                await asyncio.sleep(request_spacing)

    if fetch is not None:
        await collect(fetch)
    else:
        async with httpx.AsyncClient(timeout=8, headers={'Accept': 'application/json',
                'User-Agent': 'CliperX pool-coverage/2'}) as client:
            await collect(lambda token: _fetch(client, token, chain))
    return result


async def reconcile_all_chains():
    # Advance one chain per turn, including small chains; a long X Layer scan
    # cannot prevent BNB from being visited. The per-chain cursor is durable.
    system = await store('system')
    chains = [chain for chain in NETWORKS if official_contracts(chain)]
    if not chains:
        return {'requested': 0, 'accepted': 0, 'skipped': 1}
    checkpoint = await system.get('collector', 'discovery-chain-cursor') or {}
    offset = int(checkpoint.get('cursor', 0)) % len(chains)
    await system.put('collector', 'discovery-chain-cursor', {'cursor': (offset+1) % len(chains)})
    chain = chains[offset]
    return await reconcile_bnb(chain=chain, max_contracts=8 if chain == '196' else 32,
                               request_spacing=2.1 if chain == '196' else REQUEST_SPACING_SECONDS)


async def poll_active_stock_pairs():
    """Minute cadence, bounded and round-robin across verified catalogue sides.

    The scheduled task polls active contracts, not the full historical issuer
    manifest. Daily full reconciliation owns the completeness claim.
    """
    system = await store('system')
    subjects = []
    for chain in NETWORKS:
        approved = set(official_contracts(chain))
        if not approved:
            continue
        s = await store(chain)
        known = {_address(r.get('stockSide') or r.get('stock')) for r in await s.all('relation') if r.get('status') == 'verified'}
        catalogue = {_address(r.get('tokenContractAddress')) for r in await s.all('stock') if r.get('active') is not False}
        subjects.extend((chain, token) for token in sorted(approved & (known | catalogue)))
    if not subjects:
        return {'requested': 0, 'accepted': 0, 'skipped': 1}
    checkpoint = await system.get('collector', 'stock-pair-minute') or {}
    offset = int(checkpoint.get('cursor', 0)) % len(subjects)
    selected = (subjects[offset:]+subjects[:offset])[:8]
    result = {'requested': 0, 'accepted': 0, 'updated': 0, 'failed': 0}
    async with httpx.AsyncClient(timeout=10) as client:
        for chain, token in selected:
            result['requested'] += 1
            try:
                pairs, _, capped = normalize_pairs(await _fetch(client, token, chain), token, NETWORKS[chain])
                result['accepted'] += 1
                for pair in pairs.values():
                    result['updated'] += int(await queue_indexed_gap(system, {**pair, 'chainId': chain,
                        'indexProvider': 'GeckoTerminal' if chain == '196' else PROVIDER}, seen_at=int(time.time()*1000)))
            except (httpx.HTTPError, ValueError, TypeError):
                result['failed'] += 1
            await asyncio.sleep(2.1 if chain == '196' else REQUEST_SPACING_SECONDS)
    await system.put('collector', 'stock-pair-minute', {**result, 'at': int(time.time()*1000),
        'cursor': (offset+len(selected)) % len(subjects), 'subjectCount': len(subjects),
        'scope': 'active-verified-stock-catalogue', 'cadenceMs': 60_000, 'maxContractsPerRound': 8})
    return result


async def discovery_coverage_snapshot(now_ms: int) -> dict:
    system = await store('system')
    chains = []
    for chain in NETWORKS:
        found = await system.get(REPORT_KIND, chain)
        report = dict(found) if isinstance(found, dict) else {
            'chainId': chain, 'status': 'unverified', 'scope': 'external-pool-index-unconfirmed',
            'checkedAt': None, 'eligible': 0, 'covered': 0, 'missing': 0, 'coverageRatio': None}
        if not official_contracts(chain):
            report['reason'] = 'official-stock-manifest-unavailable'
        if report.get('checkedAt') and now_ms-report['checkedAt'] > 36*3600_000:
            report['status'] = 'stale'
            report['passesCoverage95'] = False
        report['factoryTask'] = await system.get('collector', 'factory-discovery:'+chain)
        chains.append(report)
    return {'chains': chains, 'minutePoll': await system.get('collector', 'stock-pair-minute')}
