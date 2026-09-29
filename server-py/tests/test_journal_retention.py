import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from app.realtime_schema import REALTIME_SCHEMA
from app.stream_hub import _prune_journal


class JournalRetentionTests(unittest.TestCase):
    def test_cleanup_outpaces_a_burst_without_deleting_business_records_or_unconsumed_events(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'STREAM_LEDGER_PATH': directory + '/events.sqlite'
        }):
            connection = sqlite3.connect(directory + '/events.sqlite')
            try:
                connection.executescript(REALTIME_SCHEMA)
                connection.execute('CREATE TABLE trades (id INTEGER PRIMARY KEY, body TEXT)')
                connection.execute("INSERT INTO trades VALUES (1, 'permanent-market-history')")
                now = int(time.time()*1000)
                connection.executemany('INSERT INTO realtime_events VALUES (?,?,?,?)',
                    [(i, 'trade', '{}', now-7_200_000) for i in range(1, 7001)] +
                    [(7001, 'trade', '{}', now), (7002, 'trade', '{}', now-7_200_000)])
                connection.commit()
                _prune_journal(7001)
                self.assertEqual(connection.execute('SELECT id FROM realtime_events ORDER BY id').fetchall(),
                                 [(7001,), (7002,)])
                self.assertEqual(connection.execute('SELECT body FROM trades').fetchone()[0],
                                 'permanent-market-history')
            finally:
                connection.close()

    def test_fresh_journal_never_contends_with_live_writer(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'STREAM_LEDGER_PATH': directory + '/events.sqlite'
        }):
            connection = sqlite3.connect(directory + '/events.sqlite')
            try:
                connection.execute('PRAGMA journal_mode=WAL')
                connection.executescript(REALTIME_SCHEMA)
                connection.execute('INSERT INTO realtime_events VALUES (1,?,?,?)',
                                   ('trade', '{}', int(time.time()*1000)))
                connection.commit()
                connection.execute('BEGIN IMMEDIATE')
                # No expired rows: maintenance needs no writer lock, even
                # while another process keeps its ingestion transaction open.
                _prune_journal(2)
                connection.rollback()
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM realtime_events').fetchone()[0], 1)
            finally:
                connection.close()

    def test_age_and_count_retention_preserve_unconsumed_events(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'STREAM_LEDGER_PATH': directory + '/events.sqlite'
        }):
            connection = sqlite3.connect(directory + '/events.sqlite')
            try:
                connection.executescript(REALTIME_SCHEMA)
                now = int(time.time()*1000)
                connection.executemany('INSERT INTO realtime_events VALUES (?, ?, ?, ?)', [
                    (1, 'trade', '{}', now),        # beyond count retention
                    (2, 'trade', '{}', now-90_000_000),
                    (500002, 'trade', '{}', now),   # retained
                    (500004, 'trade', '{}', now-90_000_000),  # not consumed
                ])
                connection.commit()
                _prune_journal(500003)
                self.assertEqual(connection.execute('SELECT id FROM realtime_events ORDER BY id').fetchall(),
                                 [(500002,), (500004,)])
            finally:
                connection.close()
