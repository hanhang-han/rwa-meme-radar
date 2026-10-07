"""Explicit account schema additions after the lossless source import.

The imported SQLite schema remains frozen. Account startup only verifies its
declared tables through storage_runtime; this installer adds the six empty
Telegram tables to the owned PostgreSQL target before the runtime switch.
"""
from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

from .storage_migration import IDENTITY_COLUMN, quote, target_ddl, target_index_ddl
from .storage_schema import postgres_runtime_schema_sql

_MODULES = ('developer_access.py', 'user_features.py', 'product_social.py', 'telegram_alerts.py')
TELEGRAM_TABLES = ('telegram_bindings', 'telegram_bind_codes', 'telegram_runtime',
                   'telegram_asset_state', 'telegram_outbox', 'telegram_daily_budget')


def declared_account_tables():
    """Read literal initialization DDL without running account code or imports."""
    connection = sqlite3.connect(':memory:')
    try:
        for module in _MODULES:
            source = ast.parse(Path(__file__).with_name(module).read_text())
            init = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'init')
            for node in ast.walk(init):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or not node.args:
                    continue
                value = node.args[0]
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    continue
                if node.func.attr == 'executescript':
                    connection.executescript(value.value)
                elif node.func.attr == 'execute' and value.value.strip().upper().startswith('CREATE INDEX'):
                    connection.execute(value.value)
        result = {}
        for name, statement in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid"):
            columns = [dict(zip(('cid', 'name', 'type', 'notnull', 'default', 'pk'), row))
                       for row in connection.execute('PRAGMA table_info(' + quote(name) + ')')]
            indexes = [row[0] for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL ORDER BY name", (name,))]
            result[name] = {'name': name, 'sql': statement, 'columns': columns, 'indexes': indexes}
        if not set(TELEGRAM_TABLES).issubset(result) or len(result) != 23:
            raise ValueError('unexpected-account-runtime-schema')
        return result
    finally:
        connection.close()


def install_account_auxiliary_schema(pg, schema='cliperx_storage_accounts'):
    """Install missing Telegram tables and locks in one explicit transaction.

    Accept a psycopg connection. No source SQLite file is opened or changed,
    and pre-existing account rows and migrated ownership metadata are retained.
    """
    if not re.fullmatch(r'cliperx_storage_[a-z0-9_]{1,40}', schema):
        raise ValueError('unowned-account-schema')
    declarations = declared_account_tables()
    installed = []
    indexes_installed = []
    with pg.transaction():
        pg.execute('SET LOCAL search_path TO ' + quote(schema) + ',pg_catalog')
        owner = pg.execute('SELECT source_id,source_schema_hash FROM _cliperx_migration_owner').fetchall()
        if len(owner) != 1 or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for value in owner[0]):
            raise ValueError('unverified-account-schema-owner')
        if pg.execute('SELECT 1 FROM _cliperx_migration_state WHERE NOT backfill_done OR NOT indexes_done LIMIT 1').fetchone():
            raise ValueError('account-import-not-complete')
        existing = {row[0] for row in pg.execute(
            'SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type=%s', (schema, 'BASE TABLE'))}
        missing = set(declarations) - existing
        if missing - set(TELEGRAM_TABLES):
            raise ValueError('missing-imported-account-table')
        for name, table in declarations.items():
            if name in missing:
                pg.execute(target_ddl(table))
                installed.append(name)
            actual = {row[0] for row in pg.execute(
                'SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s', (schema, name))}
            expected = {column['name'] for column in table['columns']} | {IDENTITY_COLUMN}
            if actual != expected:
                raise ValueError('account-runtime-column-mismatch:' + name)
            for statement in table['indexes']:
                from sqlglot import parse_one
                index_name = parse_one(statement, read='sqlite').this.name
                if not pg.execute('SELECT 1 FROM pg_indexes WHERE schemaname=%s AND indexname=%s', (schema, index_name)).fetchone():
                    pg.execute(target_index_ddl(statement))
                    indexes_installed.append(index_name)
        pg.execute(postgres_runtime_schema_sql(schema, list(declarations), realtime_outbox=False, identity_tables=()))
    return {'installedTables': installed, 'installedIndexes': indexes_installed, 'runtimeTables': len(declarations)}


def install_standby_account_auxiliary_schema(path):
    """Explicitly add the six empty Telegram tables to an existing standby.

    Call only after the forward migration's final receipt is saved. Recovery
    verifies the original table schema hashes separately before adopting the
    six extra empty tables. No existing account rows or original table DDL are
    rewritten, and an existing nonempty Telegram table is rejected.
    """
    declarations = declared_account_tables()
    installed = []
    indexes_installed = []
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=rw', uri=True, timeout=15)
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('BEGIN IMMEDIATE')
        existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = set(declarations) - set(TELEGRAM_TABLES)
        if required - existing:
            raise ValueError('missing-standby-account-table')
        for name, table in declarations.items():
            if name not in existing:
                connection.execute(table['sql'])
                installed.append(name)
            actual = list(connection.execute('PRAGMA table_info(' + quote(name) + ')'))
            expected = [(column['name'], column['type'].upper(), column['notnull'], column['pk'])
                        for column in table['columns']]
            if [(row[1], row[2].upper(), row[3], row[5]) for row in actual] != expected:
                raise ValueError('standby-account-column-mismatch:' + name)
            if name not in TELEGRAM_TABLES:
                continue
            if connection.execute('SELECT 1 FROM ' + quote(name) + ' LIMIT 1').fetchone():
                raise ValueError('standby-telegram-table-is-not-empty:' + name)
            for statement in table['indexes']:
                from sqlglot import parse_one
                index_name = parse_one(statement, read='sqlite').this.name
                if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index_name,)).fetchone():
                    connection.execute(statement)
                    indexes_installed.append(index_name)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {'installedTables': installed, 'installedIndexes': indexes_installed, 'runtimeTables': len(declarations)}
