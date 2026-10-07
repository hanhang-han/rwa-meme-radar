"""Deterministic asset facts. Model rewriting is never enabled, even by env.

Kept as an independent read module so visitors and alert workers use the
same published snapshot without paid generation or write-side demand.
"""
import time
from .asset_facts import READOUT_MAX_AGE_MS, build_fact_packet, fact_hash, template_lines
from .realtime_projection import ProjectionUnavailable, read_token_projection

PROMPT_VERSION = 5


def _model_publication_enabled():
    return False


def _response(facts, lang, now, saved=None):
    stale = facts['dataAsOf'] is None or now - facts['dataAsOf'] > READOUT_MAX_AGE_MS
    lines = template_lines(facts, lang)[:3]
    return {'text': '\n'.join(lines), 'lines': lines,
            'status': 'stale' if stale else 'ready', 'stale': stale,
            'source': 'template', 'factHash': fact_hash(facts), 'facts': facts,
            'missing': facts['missing'], 'dataAsOf': facts['dataAsOf'],
            'refreshIntervalMs': 60_000}


async def request_insight(chain, address, lang):
    lang = 'en' if lang == 'en' else 'zh'
    try:
        snapshot = await read_token_projection(chain, address.lower())
    except ProjectionUnavailable:
        return {'text': None, 'lines': [], 'status': 'not_ready',
                'reason': 'snapshot-not-ready', 'stale': True, 'dataAsOf': None}
    if snapshot.get('asset') is None:
        return {'text': None, 'lines': [], 'status': 'not_indexed',
                'reason': 'not_indexed', 'stale': True, 'dataAsOf': None}
    facts = build_fact_packet(snapshot, chain, address.lower())
    if facts is None:
        return {'text': None, 'lines': [], 'status': 'no_verified_pool',
                'reason': 'no-verified-stock-pool', 'stale': True, 'dataAsOf': None}
    return _response(facts, lang, int(time.time() * 1000))


async def refresh_insights():
    """Compatibility scheduler hook; facts require no generation worker."""
    return None
