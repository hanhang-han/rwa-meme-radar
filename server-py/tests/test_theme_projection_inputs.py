"""Snapshot-local indexing must preserve evidence and chain-scoped metrics."""
import copy
import unittest

from app.product_metrics import build_product_theme_metrics
from app.state import _theme_metric_inputs

NOW = 1_791_100_000_000


def asset(chain, token, **extra):
    return {'chainId': chain, 'token': token, 'symbol': token.upper(),
            'aggregateMarket': {'volume24h': 100, 'volumeAt': NOW-1000,
                                'provider': 'test', 'scope': 'token-aggregate',
                                'volumeCurrency': 'USD'}, **extra}


def relation(chain, ticker, token, pool, **extra):
    return {'chainId': chain, 'ticker': ticker, 'token': token, 'pool': pool,
            'status': 'verified', 'level': 'A', 'liquidityUsd': 2000,
            'liquidityAt': NOW-1000, 'checkedAt': NOW-1000,
            'poolCreatedAt': NOW-2000, 'creationConfirmed': True,
            'poolMarket': {'volume24h': 25, 'updatedAt': NOW-1000,
                           'provider': 'test', 'scope': 'pool:'+pool,
                           'volumeCurrency': 'USD'}, **extra}


class ThemeProjectionInputsTests(unittest.TestCase):
    def fixture(self):
        assets = [asset('56', 'a'), asset('196', 'a'),
                  asset('56', 'derivative', assetCategory='derivative'),
                  asset('56', 'name-only', relationLevel='B', match={'ticker': 'nvda'}),
                  asset('196', 'name-only', relationLevel='B', match={'ticker': 'NVDA'}),
                  *[asset('56', 'unrelated-'+str(index)) for index in range(100)]]
        rows = [relation('56', 'NVDA', 'a', 'p1'), relation('196', 'NVDA', 'a', 'p1'),
                relation('56', 'NVDA', 'a', 'p2'),
                relation('56', 'NVDA', 'derivative', 'p3'),
                relation('56', 'NVDA', 'missing', 'p4'),
                relation('56', 'OTHER', 'unrelated-0', 'q1'),
                # Latest invalid evidence must override an older verified row.
                relation('56', 'NVDA', 'a', 'p2', status='invalid', checkedAt=NOW),
                relation('56', 'wrong-reported', 'a', 'p5', stockIdentity={'ticker': 'nvda'})]
        return assets, rows

    def test_same_input_metrics_match_full_scan_with_duplicates_missing_and_derivative_evidence(self):
        assets, rows = self.fixture()
        original = copy.deepcopy((assets, rows))
        for chain in (None, '56', '196', '4663'):
            scoped_assets = [row for row in assets if chain is None or row['chainId']==chain]
            scoped_relations = [row for row in rows if chain is None or row['chainId']==chain]
            inputs, names = _theme_metric_inputs(scoped_assets, scoped_relations)
            for ticker in ('NVDA', 'OTHER', 'EMPTY'):
                selected_assets, selected_rows = inputs(ticker)
                self.assertEqual(build_product_theme_metrics(ticker, selected_assets, selected_rows, NOW),
                                 build_product_theme_metrics(ticker, scoped_assets, scoped_relations, NOW))
                expected_names = sum(row.get('relationLevel')=='B' and
                                     str((row.get('match') or {}).get('ticker') or '').upper()==ticker
                                     for row in scoped_assets)
                self.assertEqual(names.get(ticker,0), expected_names)
            self.assertLessEqual(len(inputs('NVDA')[0]), 4)
        self.assertEqual((assets, rows), original)

    def test_new_snapshot_update_deletion_and_ticker_move_do_not_reuse_previous_index(self):
        assets, rows = self.fixture()
        first, first_names = _theme_metric_inputs(assets, rows)
        previous = build_product_theme_metrics('NVDA', *first('NVDA'), NOW)
        next_assets = [row for row in assets if row['token']!='name-only']
        next_rows = [row for row in rows if row['pool']!='p1']
        next_rows.append(relation('56', 'OTHER', 'a', 'new'))
        second, second_names = _theme_metric_inputs(next_assets, next_rows)
        for ticker in ('NVDA', 'OTHER'):
            self.assertEqual(build_product_theme_metrics(ticker, *second(ticker), NOW),
                             build_product_theme_metrics(ticker, next_assets, next_rows, NOW))
        self.assertEqual(first_names['NVDA'], 2)
        self.assertNotIn('NVDA', second_names)
        self.assertEqual(build_product_theme_metrics('NVDA', *first('NVDA'), NOW), previous)
        self.assertNotEqual(build_product_theme_metrics('NVDA', *second('NVDA'), NOW), previous)

    def test_each_source_is_scanned_once_before_all_ticker_lookups(self):
        class Once:
            def __init__(self, rows): self.rows, self.reads = rows, 0
            def __iter__(self):
                self.reads += 1
                if self.reads > 1: raise AssertionError('catalogue-rescanned')
                return iter(self.rows)
        assets, rows = self.fixture()
        # Assets are traversed twice once for keyed dependency lookup and once
        # for B-level name counts, independent of the number of stock themes.
        # A reusable list snapshot deliberately supplies those two bounded passes.
        relations = Once(rows)
        inputs, _ = _theme_metric_inputs(assets, relations)
        for index in range(1500):
            selected_assets, selected_rows = inputs('EMPTY'+str(index))
            self.assertEqual((selected_assets, selected_rows), ([], []))
        self.assertEqual(relations.reads, 1)
