"""Copy-on-write fact view tied to the projection's SQLite read snapshot.

The outbox includes external Node writers. Only committed changed bodies are
decoded again; a failed publication cannot mutate the last published view.
"""
import json

KINDS = ('asset', 'relation', 'stock', 'comparison', 'market-quote',
         'chain-stream', 'market-stream', 'market-stream-status',
         'basket', 'basket-last', 'basket-view', 'stock-sparkline')
KEYS = tuple(f'{chain}:{kind}' for chain in ('196', '56', '4663', '5042') for kind in KINDS) + ('system:shared-source', 'system:product-theme')


class ProjectionFacts:
    def __init__(self, rows):
        self.rows = rows

    @classmethod
    async def capture(cls, connection, previous=None, changes=()):
        if previous is None or any(key not in previous.rows for key in KEYS):
            records = await connection.execute_fetchall(
                'SELECT kind,id,body FROM facts WHERE kind IN (' + ','.join('?' for _ in KEYS) + ')', KEYS)
            rows = {kind: {} for kind in KEYS}
            for record in records:
                rows[record['kind']][record['id']] = json.loads(record['body'])
        else:
            rows = dict(previous.rows)
            changed = {}
            for change in changes:
                if change['kind'] in rows:
                    changed.setdefault(change['kind'], set()).add(change['entity'])
            for kind, identities in changed.items():
                rows[kind] = dict(rows[kind])
                # A delete followed by an insert must resolve to the final
                # body in this exact read transaction, not operation order.
                for identity in identities:
                    rows[kind].pop(identity, None)
                identities = sorted(identities)
                for start in range(0, len(identities), 400):
                    batch = identities[start:start+400]
                    records = await connection.execute_fetchall(
                        'SELECT id,body FROM facts WHERE kind=? AND id IN (' + ','.join('?' for _ in batch) + ')',
                        (kind, *batch))
                    for record in records:
                        rows[kind][record['id']] = json.loads(record['body'])
        # Checkpoints intentionally do not enter the market-change outbox.
        # Refresh only the price-less assets' quote rows in bounded PK batches
        # within this same snapshot. Replacing these small maps also captures
        # updates/deletes without dirtying every projection for job telemetry.
        for chain in ('196', '56', '4663', '5042'):
            kind = f'{chain}:collector-job'
            rows[kind] = {}
            identities = sorted({'quote:' + str(row.get('token')).lower()
                                 for row in rows[f'{chain}:asset'].values()
                                 if row.get('kind') in ('candidate', 'stock')
                                 and row.get('price') is None and row.get('token')})
            for start in range(0, len(identities), 400):
                batch = identities[start:start+400]
                records = await connection.execute_fetchall(
                    'SELECT id,body FROM facts WHERE kind=? AND id IN (' + ','.join('?' for _ in batch) + ')',
                    (kind, *batch))
                for record in records:
                    rows[kind][record['id']] = json.loads(record['body'])
        return cls(rows)

    def all(self, chain, kind):
        # DashboardData annotates root dictionaries. Nested evidence is read
        # only; enrich_asset explicitly copies every nested map it modifies.
        rows = self.rows[f'{chain}:{kind}']
        return [dict(rows[identity]) for identity in sorted(rows)]

    def get(self, chain, kind, identity):
        """Read a checkpoint from the same immutable view as its asset."""
        row = self.rows.get(f'{chain}:{kind}', {}).get(identity)
        return dict(row) if row is not None else None

    def all_kv(self, chain, kind):
        rows = self.rows[f'{chain}:{kind}']
        return [(identity, dict(rows[identity])) for identity in sorted(rows)]
