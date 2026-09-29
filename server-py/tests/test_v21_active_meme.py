import unittest

from app.state import DashboardData, _active_meme_count


class ActiveMemeTests(unittest.TestCase):
    def test_new_pair_count_is_unknown_without_creation_evidence(self):
        now = 2_000_000_000
        pool = {'chainId': '196', 'pool': '0x' + 'a' * 40,
                'token': '0x' + 'b' * 40, 'liquidityUsd': 1200,
                'liquidityAt': now - 1000, 'checkedAt': now - 1000,
                'firstSeen': now - 1000}
        data = DashboardData()
        unknown = data.metrics(now=now, pools=[pool])
        self.assertIsNone(unknown['newPair24h'])
        self.assertEqual(unknown['newRelations24h'], 1)
        known = data.metrics(now=now, pools=[{**pool, 'poolCreatedAt': now - 1000}])
        self.assertEqual(known['newPair24h'], 1)

    def test_requires_fresh_aggregate_and_quote_without_pair_grade(self):
        now = 2_000_000
        base = {
            'chainId': '196', 'token': '0x' + 'a' * 40, 'kind': 'candidate',
            'price': 1, 'fieldTimes': {'price': now - 1000},
            'totalLiquidityUsd': 1500, 'totalLiquidityAt': now - 1000,
            'totalLiquidityStatus': 'current',
            'relationLevel': None,
        }
        duplicate = dict(base)
        pool_only = {**base, 'token': '0x' + 'b' * 40, 'totalLiquidityUsd': None,
                     'singlePoolLiquidityUsd': 2000}
        stale_price = {**base, 'token': '0x' + 'c' * 40,
                       'fieldTimes': {'price': now - 900_001}}
        other_chain = {**base, 'chainId': '56', 'token': '0x' + 'd' * 40}
        rows = [base, duplicate, pool_only, stale_price, other_chain]
        self.assertEqual(_active_meme_count(rows, now), 2)
        self.assertEqual(_active_meme_count(rows, now, '196'), 1)
        self.assertEqual(_active_meme_count(rows, now, '56'), 1)


if __name__ == '__main__':
    unittest.main()
