"""Durable scheduling checkpoints; failures never monopolise a collector lane."""
from .assets import now_ms


def result(**values):
    return {**dict(requested=0, accepted=0, updated=0, failed=0, quotaBlocked=0,
                   unsupported=0, skipped=0), **values}


def combine(total, part):
    for name in ('requested', 'accepted', 'updated', 'failed', 'quotaBlocked', 'unsupported', 'skipped'):
        total[name] = total.get(name, 0) + (part or {}).get(name, 0)
    return total


def due(job, now, interval=0):
    return now >= max(job.get('nextRetryAt') or 0, (job.get('lastSuccessAt') or 0) + interval)


async def checkpoint(s, domain, key, *, success, reason=None, now=None, retry_ms=None):
    now = now or now_ms()
    ident = f'{domain}:{key}'
    previous = await s.get('collector-job', ident) or {}
    failures = 0 if success else min(10, (previous.get('failureCount') or 0) + 1)
    row = {**previous, 'id': ident, 'domain': domain, 'key': key, 'lastAttemptAt': now,
           'lastSuccessAt': now if success else previous.get('lastSuccessAt'),
           'failureCount': failures, 'reason': reason,
           'nextRetryAt': 0 if success else now + (retry_ms or min(3_600_000, 60_000 * 2 ** failures))}
    await s.put('collector-job', ident, row)
    return row


async def jobs(s, domain):
    return {r.get('key'): r for r in await s.all('collector-job') if r.get('domain') == domain}
