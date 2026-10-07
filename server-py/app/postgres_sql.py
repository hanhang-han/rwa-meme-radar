"""Restricted SQLite query compiler for the verified PostgreSQL storage schemas.

Identifiers and parameters are AST nodes, never string substitutions. Unsupported
commands fail before execution. This is not a general purpose SQLite emulator.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from sqlglot import exp, parse_one
from sqlglot.dialects import Dialect
from sqlglot.dialects.sqlite import SQLite
from sqlglot.tokens import TokenType

ROWID = '_cliperx_source_rowid'


class StorageSQLite(SQLite):
    class Parser(SQLite.Parser):
        def _parse_is(self, this):
            # SQLite IS accepts column/expression operands, whereas sqlglot
            # 27's generic parser only accepts primary literal operands.
            negate = self._match(TokenType.NOT)
            if self._match_text_seq('DISTINCT', 'FROM'):
                kind = exp.NullSafeEQ if negate else exp.NullSafeNEQ
                return self.expression(kind, this=this, expression=self._parse_bitwise())
            right = self._parse_bitwise()
            if right is None:
                self.raise_error('missing-IS-operand')
            if isinstance(right, (exp.Null, exp.Boolean)):
                result = self.expression(exp.Is, this=this, expression=right)
                return self.expression(exp.Not, this=result) if negate else result
            return self.expression(exp.NullSafeNEQ if negate else exp.NullSafeEQ,
                                   this=this, expression=right)


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[str, ...]
    types: tuple[str, ...]
    primary_key: tuple[str, ...]
    identities: tuple[str, ...] = ()

    @property
    def rowid_column(self):
        if len(self.primary_key) == 1:
            name = self.primary_key[0]
            if self.types[self.columns.index(name)] == 'bigint':
                return name
        return ROWID


@dataclass
class Compiled:
    sql: str
    parameters: dict
    writes: bool
    returning_rowid: bool = False
    body_columns: tuple[int, ...] = ()


def _function(name, *args):
    return exp.Anonymous(this=name, expressions=list(args))


def _paths(path):
    if isinstance(path, exp.Literal):
        path = parse_one('json_extract(x,' + path.sql(dialect='sqlite') + ')', read='sqlite').expression
    if not isinstance(path, exp.JSONPath):
        raise sqlite3.NotSupportedError('nonliteral-json-path')
    keys = []
    for part in path.expressions:
        if isinstance(part, exp.JSONPathRoot):
            continue
        if isinstance(part, (exp.JSONPathKey, exp.JSONPathSubscript)) and isinstance(part.this, (str, int)):
            keys.append(exp.Literal.string(str(part.this)))
        else:
            raise sqlite3.NotSupportedError('unsupported-json-path')
    return keys


def _json(node, text=False):
    decoded = _function('_cliperx_migration_json', node.this.copy())
    return _function('jsonb_extract_path_text' if text else 'jsonb_extract_path', decoded, *_paths(node.expression))


def _numeric_context(node):
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.Cast):
            return parent.to.this in (exp.DataType.Type.INT, exp.DataType.Type.BIGINT,
                                      exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT)
        if isinstance(parent, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Sum, exp.Avg,
                               exp.Abs, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)):
            return True
        if isinstance(parent, (exp.Coalesce, exp.Case, exp.If, exp.Nullif)):
            if any(isinstance(x, exp.Literal) and not x.is_string for x in parent.expressions):
                return True
        if isinstance(parent, (exp.EQ, exp.NEQ, exp.Is)):
            other = parent.expression if parent.this is node else parent.this
            return isinstance(other, exp.Literal) and not other.is_string
        if isinstance(parent, (exp.Select, exp.Order, exp.Group, exp.Where, exp.Join)):
            return False
        parent = parent.parent
    return False


def _projected_json(node):
    parent = node.parent
    while isinstance(parent, (exp.Alias, exp.Paren)):
        parent = parent.parent
    return isinstance(parent, exp.Select)


def _parameterize(statement, values):
    tokens = Dialect.get_or_raise('sqlite').tokenize(statement)
    replacements = []
    index = 0
    for token in tokens:
        if token.token_type == TokenType.PLACEHOLDER:
            if token.text != '?':
                raise sqlite3.NotSupportedError('unsupported-placeholder')
            replacements.append((token.start, token.end + 1, ':__cliperx_arg_' + str(index)))
            index += 1
    if isinstance(values, dict):
        raise sqlite3.NotSupportedError('named-sqlite-parameters')
    values = tuple(values or ())
    if len(values) != index:
        raise sqlite3.ProgrammingError('incorrect-parameter-count')
    for start, end, value in reversed(replacements):
        statement = statement[:start] + value + statement[end:]
    return statement, {'__cliperx_arg_' + str(i): value for i, value in enumerate(values)}


def compile_sql(statement, values, tables):
    statement, parameters = _parameterize(statement, values)
    # INDEXED BY is a planner hint with no PostgreSQL equivalent. Token positions
    # prevent accidental rewrites inside JSON bodies, comments or literals.
    tokens = Dialect.get_or_raise('sqlite').tokenize(statement)
    removals = []
    for index, token in enumerate(tokens):
        if token.text.upper() == 'INDEXED' and index + 2 < len(tokens) and tokens[index + 1].text.upper() == 'BY':
            removals.append((token.start, tokens[index + 2].end + 1))
    for start, end in reversed(removals):
        statement = statement[:start] + statement[end:]
    try:
        tree = parse_one(statement, read=StorageSQLite)
    except Exception as error:
        raise sqlite3.NotSupportedError('unsupported-query-syntax') from error
    if not isinstance(tree, (exp.Select, exp.Union, exp.Insert, exp.Update, exp.Delete)):
        raise sqlite3.NotSupportedError('unsupported-storage-command')
    aliases = {}
    ctes = {item.alias.lower() for item in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if table.db or table.catalog:
            raise sqlite3.NotSupportedError('cross-schema-storage-query')
        name = table.name.lower()
        if name not in tables:
            if name in ctes:
                continue
            raise sqlite3.OperationalError('unknown-storage-table')
        spec = tables[name]
        table.set('this', exp.to_identifier(spec.name, quoted=True))
        aliases[table.alias_or_name.lower()] = spec
    unique_tables = {spec.name: spec for spec in aliases.values()}
    for column in tree.find_all(exp.Column):
        if column.is_star:
            continue
        spec = aliases.get(column.table.lower()) if column.table else (
            next(iter(unique_tables.values())) if len(unique_tables) == 1 else None)
        if column.name.lower() in ('rowid', '_rowid_', 'oid'):
            column.set('this', exp.to_identifier(spec.rowid_column if spec else ROWID, quoted=True))
        elif spec:
            canonical = {name.lower(): name for name in spec.columns}.get(column.name.lower())
            if canonical:
                column.set('this', exp.to_identifier(canonical, quoted=True))
        elif not column.table:
            matches = {name for spec in unique_tables.values() for name in spec.columns if name.lower() == column.name.lower()}
            if len(matches) == 1:
                column.set('this', exp.to_identifier(matches.pop(), quoted=True))
    for select in tree.find_all(exp.Select):
        expressions = []
        for item in select.expressions:
            if isinstance(item, exp.Star) or isinstance(item, exp.Column) and item.is_star:
                if isinstance(item, exp.Column):
                    sources = [(item.table, aliases.get(item.table.lower()))]
                else:
                    sources = [(source.alias_or_name, aliases.get(source.alias_or_name.lower()))
                               for source in select.find_all(exp.Table)
                               if source.find_ancestor(exp.Select) is select]
                if not sources or any(spec is None for _, spec in sources):
                    raise sqlite3.NotSupportedError('unsupported-star-source')
                expressions.extend(exp.column(name, table=alias, quoted=True) for alias, spec in sources for name in spec.columns)
            else:
                expressions.append(item)
        select.set('expressions', expressions)
    for node in list(tree.find_all(exp.JSONType)):
        node.replace(_function('_cliperx_sqlite_json_type', _json(node)))
    for node in list(tree.find_all(exp.JSONExtract)):
        if isinstance(node.parent, exp.Cast) and node.parent.to.this in (exp.DataType.Type.INT, exp.DataType.Type.BIGINT):
            node.parent.replace(_function('_cliperx_sqlite_int', _json(node, text=True)))
            continue
        if _numeric_context(node):
            replacement = _function('_cliperx_sqlite_num', _json(node, text=True))
        elif _projected_json(node):
            replacement = _json(node)
        else:
            replacement = _json(node, text=True)
        node.replace(replacement)
    for node in list(tree.find_all(exp.JSONSet)):
        if len(node.expressions) % 2:
            raise sqlite3.NotSupportedError('invalid-json-set-arguments')
        value = _function('_cliperx_migration_json', node.this.copy())
        for index in range(0, len(node.expressions), 2):
            path, replacement = node.expressions[index:index + 2]
            keys = _paths(path)
            if len(keys) != 1:
                raise sqlite3.NotSupportedError('nested-json-set-requires-explicit-migration')
            replacement = replacement.copy()
            if isinstance(replacement, exp.Literal) and replacement.is_string:
                replacement = exp.Cast(this=replacement, to=exp.DataType.build('TEXT'))
            elif isinstance(replacement, exp.Placeholder):
                original = parameters[replacement.this]
                datatype = ('TEXT' if isinstance(original, str) or original is None else
                            'BOOLEAN' if isinstance(original, bool) else
                            'BIGINT' if isinstance(original, int) else
                            'DOUBLE PRECISION' if isinstance(original, float) else None)
                if datatype is None:
                    raise sqlite3.NotSupportedError('unsupported-json-set-value')
                replacement = exp.Cast(this=replacement, to=exp.DataType.build(datatype))
            replacement = exp.Coalesce(this=_function('to_jsonb', replacement),
                                       expressions=[exp.Cast(this=exp.Literal.string('null'), to=exp.DataType.build('JSONB'))])
            value = _function('jsonb_set', value, exp.Array(expressions=keys), replacement, exp.Boolean(this=True))
        node.replace(exp.Cast(this=value, to=exp.DataType.build('TEXT')))
    for node in list(tree.find_all(exp.Anonymous)):
        if node.name.lower() == 'json_valid':
            node.replace(exp.Not(this=exp.Is(this=_function('_cliperx_migration_json', node.expressions[0].copy()), expression=exp.Null())))
        elif node.name.lower() in ('changes', 'last_insert_rowid'):
            raise sqlite3.NotSupportedError('connection-local-function-must-be-intercepted')
    for node in list(tree.find_all(exp.StrPosition)):
        node.replace(_function('strpos', node.this.copy(), node.args['substr'].copy()))
    for node in list(tree.find_all(exp.Substring)):
        args = [node.this.copy(), node.args.get('start', exp.Literal.number(1)).copy()]
        if node.args.get('length') is not None:
            args.append(node.args['length'].copy())
        node.replace(_function('_cliperx_sqlite_substr', *args))
    for node in list(tree.find_all(exp.Glob)):
        patterns = {'*[^0-9]*': '[^0-9]', '*[^0-9a-f]*': '[^0-9a-f]'}
        if not isinstance(node.expression, exp.Literal) or node.expression.this not in patterns:
            raise sqlite3.NotSupportedError('unsupported-glob-pattern')
        node.replace(exp.RegexpLike(this=node.this.copy(), expression=exp.Literal.string(patterns[node.expression.this])))
    for node in list(tree.find_all(exp.Is)):
        if not isinstance(node.expression, (exp.Null, exp.Boolean)):
            replacement = exp.NullSafeEQ(this=node.this.copy(), expression=node.expression.copy())
            if isinstance(node.parent, exp.Not):
                node.parent.replace(exp.NullSafeNEQ(this=node.this.copy(), expression=node.expression.copy()))
            else:
                node.replace(replacement)
    for node in list(tree.find_all(exp.Sum)):
        if isinstance(node.this, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.And, exp.Or, exp.Not)):
            node.set('this', exp.Cast(this=node.this.copy(), to=exp.DataType.build('INT')))
    for node in list(tree.find_all(exp.Max, exp.Min)):
        if node.expressions:
            args = [node.this.copy(), *[value.copy() for value in node.expressions]]
            missing = exp.or_(*[exp.Is(this=value.copy(), expression=exp.Null()) for value in args])
            node.replace(exp.Case(ifs=[exp.If(this=missing, true=exp.Null())], default=_function('greatest' if isinstance(node, exp.Max) else 'least', *args)))
    for node in list(tree.find_all(exp.Cast)):
        if node.to.this in (exp.DataType.Type.INT, exp.DataType.Type.BIGINT):
            # Boolean cast is PostgreSQL's integer 0/1; other conversions need
            # SQLite's permissive/truncating numeric conversion.
            if not isinstance(node.this, (exp.EQ, exp.NEQ, exp.Not, exp.Is, exp.NullSafeEQ, exp.NullSafeNEQ)):
                node.replace(_function('_cliperx_sqlite_int', exp.Cast(this=node.this.copy(), to=exp.DataType.build('TEXT'))))
    for node in list(tree.find_all(exp.Like)):
        node.replace(exp.ILike(this=node.this.copy(), expression=node.expression.copy()))
    # SQLite permits mixed literals in text IN sets. The actual user identity
    # query uses ('', 0), where JSON scalar zero and string zero are both blank.
    for node in tree.find_all(exp.In):
        if any(isinstance(x, exp.Literal) and x.is_string for x in node.expressions):
            node.set('expressions', [exp.Literal.string(x.this) if isinstance(x, exp.Literal) and not x.is_string else x for x in node.expressions])
    returning = False
    if isinstance(tree, exp.Insert):
        target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        spec = tables[target.name.lower()]
        if isinstance(tree.this, exp.Table):
            tree.set('this', exp.Schema(this=target.copy(), expressions=[exp.to_identifier(name, quoted=True) for name in spec.columns]))
        columns = [item.name for item in tree.this.expressions]
        if isinstance(tree.expression, exp.Values):
            for row in tree.expression.expressions:
                if len(row.expressions) != len(columns):
                    raise sqlite3.ProgrammingError('insert-column-count')
                for index, name in enumerate(columns):
                    value = row.expressions[index]
                    if spec.name == 'dashboard_projection' and name == 'body' and isinstance(value, exp.Placeholder):
                        original = parameters[value.this]
                        if isinstance(original, str):
                            parameters[value.this] = b's' + original.encode()
                        elif isinstance(original, (bytes, bytearray, memoryview)):
                            parameters[value.this] = b'b' + bytes(original)
                        else:
                            raise sqlite3.DataError('unsupported-projection-body')
                    if name in spec.identities and (isinstance(value, exp.Null) or isinstance(value, exp.Placeholder) and parameters[value.this] is None):
                        row.expressions[index] = exp.Var(this='DEFAULT')
        alternative = tree.args.get('alternative')
        if alternative:
            tree.set('alternative', None)
            if alternative.upper() == 'IGNORE':
                tree.set('conflict', exp.OnConflict(action=exp.Var(this='DO NOTHING')))
            elif alternative.upper() == 'REPLACE':
                if not spec.primary_key:
                    raise sqlite3.NotSupportedError('replace-without-primary-key')
                updates = [exp.EQ(this=exp.column(name, quoted=True), expression=exp.column(name, table='excluded', quoted=True)) for name in columns if name not in spec.primary_key]
                tree.set('conflict', exp.OnConflict(action=exp.Var(this='DO UPDATE'), conflict_keys=[exp.column(name, quoted=True) for name in spec.primary_key], expressions=updates))
            else:
                raise sqlite3.NotSupportedError('unsupported-insert-alternative')
        conflict = tree.args.get('conflict')
        if conflict is not None:
            # PostgreSQL exposes both the target and excluded row in an UPSERT.
            # Bare references such as used=used+1 or MAX(generation,excluded...)
            # are ambiguous there, unlike SQLite. Assignment left sides must
            # stay unqualified; only their value expressions and WHERE read the
            # current target row by its explicit table/alias.
            expressions = [assignment.expression for assignment in conflict.expressions
                           if isinstance(assignment, exp.EQ)]
            if conflict.args.get('where') is not None:
                expressions.append(conflict.args['where'])
            for expression in expressions:
                for column in expression.find_all(exp.Column):
                    if not column.table and not column.is_star:
                        column.set('table', exp.to_identifier(target.alias_or_name, quoted=True))
        if tree.args.get('returning'):
            raise sqlite3.NotSupportedError('explicit-returning-not-supported')
        tree.set('returning', exp.Returning(expressions=[exp.column(spec.rowid_column, quoted=True)]))
        returning = True
    for identifier in tree.find_all(exp.Identifier):
        identifier.set('quoted', True)
    sql = tree.sql(dialect='postgres')
    # psycopg must escape literal percent signs when any bindings are supplied.
    if parameters:
        sql = sql.replace('%', '%%')
        sql = re.sub(r'%%\((__cliperx_arg_\d+)\)s', r'%(\1)s', sql)
    return Compiled(sql, parameters, isinstance(tree, (exp.Insert, exp.Update, exp.Delete)), returning)
