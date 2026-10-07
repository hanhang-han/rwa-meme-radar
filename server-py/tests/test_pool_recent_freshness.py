"""Fresh exact-pool scan evidence cannot erase a historical or transport gap."""
import pytest

from app.api.candles import pool_freshness


NOW = 1_790_000_000_000
POOL = '0x' + '1' * 40


def state(**proof_patch):
    health = {'status': 'reconnecting', 'updatedAt': NOW, 'lastHead': 1000}
    historical = {'block': 100, 'updatedAt': NOW - 100000, 'blockTime': NOW - 100000}
    proof = {'scope': 'exact-pool', 'pool': POOL, 'chainId': '56', 'canonical': True,
             'decoded': True, 'fromBlock': 969, 'throughBlock': 1000,
             'headBlockAtScan': 1000,
             'verifiedAt': NOW - 1000, 'blockTime': NOW - 2000, **proof_patch}
    meta = {'chainId': '56', 'poolId': POOL, 'lastSourceEventAt': NOW - 120000,
            'lastSuccessfulAt': NOW - 100000, 'poolScan': proof}
    return pool_freshness(health, historical, meta, NOW)


def test_current_exact_pool_can_be_quiet_while_transport_reconnects_and_history_backfills():
    result = state()
    assert not result['stale']
    assert result['marketStatus'] == 'quiet'
    assert result['coverageStatus'] == result['recentCoverageStatus'] == 'current'
    assert result['historicalCoverageStatus'] == 'backfilling'
    assert result['transportStatus'] == 'reconnecting'
    assert result['lastTradeAt'] == NOW - 120000
    assert (result['scanThroughBlock'], result['scanAt']) == (1000, NOW - 1000)
    assert result['nearTipPool'] == POOL
    assert (result['historicalScanThroughBlock'], result['historicalScanAt']) == (100, NOW - 100000)


@pytest.mark.parametrize('patch', [
    {'canonical': False}, {'decoded': False}, {'pool': '0x' + '2' * 40},
    {'chainId': '196'}, {'scope': 'chain'}, {'verifiedAt': NOW - 20001},
    {'verifiedAt': NOW + 1}, {'blockTime': NOW - 30000}, {'blockTime': NOW + 1},
    {'fromBlock': 1001}, {'throughBlock': None}, {'headBlockAtScan': 1001},
])
def test_stale_or_mismatched_proof_cannot_mark_old_replayed_trade_current(patch):
    result = state(**patch)
    assert result['stale']
    assert result['marketStatus'] == 'recovering'
    assert result['recentCoverageStatus'] == 'unverified'
    assert result['coverageStatus'] == 'backfilling'
    assert (result['historicalScanThroughBlock'], result['historicalScanAt']) == (100, NOW - 100000)
    assert (result['scanThroughBlock'], result['scanAt']) == (100, NOW - 100000)
    assert result['nearTipPool'] == patch.get('pool', POOL)


def test_recent_live_trade_remains_honest_without_proving_history():
    health = {'status': 'live', 'updatedAt': NOW}
    meta = {'lastSourceEventAt': NOW - 1000, 'lastSuccessfulAt': NOW - 500}
    result = pool_freshness(health, {}, meta, NOW)
    assert not result['stale']
    assert result['marketStatus'] == 'live'
    assert result['coverageStatus'] == result['historicalCoverageStatus'] == 'backfilling'
    assert result['recentCoverageStatus'] == 'unverified'
    assert result['nearTipPool'] is None
    assert result['historicalScanThroughBlock'] is None
    assert result['historicalScanAt'] is None


def test_known_reorg_invalidates_recent_proof_even_when_global_checkpoint_is_still_fresh():
    proof = {'scope': 'exact-pool', 'canonical': False}
    meta = {'lastSourceEventAt': NOW - 120000, 'lastSuccessfulAt': NOW - 100000, 'poolScan': proof}
    result = pool_freshness({'status': 'live', 'updatedAt': NOW, 'lastHead': 1000},
                            {'block': 1000, 'updatedAt': NOW, 'blockTime': NOW}, meta, NOW)
    assert result['stale']
    assert result['coverageStatus'] == 'backfilling'
