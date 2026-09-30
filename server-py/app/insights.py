"""Asset readouts from the published fact revision.

HTTP reads never run a model. They immediately render a bounded four-line
fact template; the worker may publish a validated rephrasing for the exact
same fact hash when model publication is explicitly enabled.
"""
import os
import time

from .ai import MODEL, ai_enabled, ai_failure, ai_narrate
from .asset_facts import (READOUT_MAX_AGE_MS, build_fact_packet, fact_hash,
                          template_lines, validate_model_text)
from .db import store
from .realtime_projection import ProjectionUnavailable, read_token_projection


CHAINS = ('196', '56', '4663')
DEMAND_MS = 2 * 60_000
RETRY_MS = 5 * 60_000
PROMPT_VERSION = 4


def _id(address: str, lang: str) -> str:
    return f'{address}:{lang}'


def _model_publication_enabled() -> bool:
    # The PRD calls for a 100-asset manual contradiction audit before model
    # prose is published. Deterministic templates remain available now.
    return os.environ.get('INSIGHT_MODEL_PUBLISH') == '1' and ai_enabled()


def _response(facts, lang, now, saved=None):
    digest = fact_hash(facts)
    stale = (facts['dataAsOf'] is None or now - facts['dataAsOf'] > READOUT_MAX_AGE_MS)
    source = 'template'
    lines = template_lines(facts, lang)
    if (_model_publication_enabled() and not stale and saved and saved.get('inputHash') == digest
            and saved.get('promptVersion') == PROMPT_VERSION
            and saved.get('source') == 'model'):
        approved = validate_model_text(saved.get('text'), facts, lang)
        if approved:
            source, lines = 'model', approved
    return {'text': '\n'.join(lines), 'lines': lines,
            'status': 'stale' if stale else 'ready', 'stale': stale,
            'source': source, 'factHash': digest, 'facts': facts,
            'missing': facts['missing'], 'dataAsOf': facts['dataAsOf'],
            'refreshIntervalMs': 60_000}


async def request_insight(chain: str, address: str, lang: str) -> dict:
    """Read one published asset revision; enqueue paid work only by lease."""
    lang = 'en' if lang == 'en' else 'zh'
    address = address.lower()
    now = int(time.time() * 1000)
    try:
        snapshot = await read_token_projection(chain, address)
    except ProjectionUnavailable:
        return {'text': None, 'lines': [], 'status': 'not_ready',
                'reason': 'snapshot-not-ready', 'stale': True, 'dataAsOf': None}
    if snapshot.get('asset') is None:
        return {'text': None, 'lines': [], 'status': 'not_indexed',
                'reason': 'not_indexed', 'stale': True, 'dataAsOf': None}
    facts = build_fact_packet(snapshot, chain, address)
    if facts is None:
        return {'text': None, 'lines': [], 'status': 'no_verified_pool',
                'reason': 'no-verified-stock-pool', 'stale': True, 'dataAsOf': None}
    s = await store(chain)
    ident = _id(address, lang)
    saved = await s.get('insight', ident)
    response = _response(facts, lang, now, saved)
    if _model_publication_enabled() and not response['stale'] and response['source'] != 'model':
        from .demand_leases import publish_lease
        publish_lease(s, 'insight-demand', ident, {
            'chainId': chain, 'address': address, 'lang': lang,
            'factHash': response['factHash'], 'requestedAt': now,
            'expiresAt': now + DEMAND_MS,
        })
    return response


async def refresh_insights() -> None:
    """Worker-only model pass; never publish a claim that fails validation."""
    if not _model_publication_enabled():
        return
    now = int(time.time() * 1000)
    for chain in CHAINS:
        s = await store(chain)
        for ident, demand in await s.all_kv('insight-demand'):
            if int(demand.get('expiresAt') or 0) < now:
                continue
            address = str(demand.get('address') or '').lower()
            lang = 'en' if demand.get('lang') == 'en' else 'zh'
            if not (address.startswith('0x') and len(address) == 42):
                continue
            try:
                snapshot = await read_token_projection(chain, address)
            except ProjectionUnavailable:
                continue
            facts = build_fact_packet(snapshot, chain, address)
            if facts is None or facts['dataAsOf'] is None or now - facts['dataAsOf'] > READOUT_MAX_AGE_MS:
                continue
            digest = fact_hash(facts)
            if demand.get('factHash') != digest:
                continue
            saved = await s.get('insight', ident)
            if saved and saved.get('inputHash') == digest and saved.get('promptVersion') == PROMPT_VERSION:
                continue
            job = await s.get('insight-job', ident) or {}
            if int(job.get('nextRetryAt') or 0) > now:
                continue
            attempts = int(job.get('attempts') or 0) + 1
            await s.put('insight-job', ident, {
                'status': 'running', 'startedAt': now, 'attempts': attempts,
                'inputHash': digest,
            })
            template = '\n'.join(template_lines(facts, lang))
            task = (
                'Rewrite the four short fact lines. Keep their four labels, '
                'all figures and evidence meaning unchanged. Each line at most '
                '40 characters. Add no claims or advice. Return four plain lines. '
                'The verified template is:\n' + template
            )
            key = f'insight:{chain}:{address}:{digest}:v{PROMPT_VERSION}'
            result = await ai_narrate(key, lang, 30 * 60_000, facts, task, force=True)
            finished = int(time.time() * 1000)
            approved = validate_model_text((result or {}).get('text'), facts, lang)
            if approved:
                record = {'text': '\n'.join(approved), 'lines': approved,
                          'at': result['at'], 'lang': lang, 'chainId': chain,
                          'address': address, 'inputHash': digest,
                          'promptVersion': PROMPT_VERSION, 'source': 'model',
                          'model': MODEL, 'dataAsOf': facts['dataAsOf']}
                await s.put('insight', ident, record)
                await s.put('insight-job', ident, {
                    'status': 'success', 'updatedAt': finished,
                    'lastSuccessAt': finished, 'attempts': attempts,
                    'inputHash': digest,
                })
            else:
                failure = ai_failure(key, lang) or {}
                await s.put('insight-job', ident, {
                    'status': 'invalid_output' if result else 'upstream_failed',
                    'updatedAt': finished, 'attempts': attempts,
                    'nextRetryAt': failure.get('retryAt') or finished + RETRY_MS,
                    'errorReason': failure.get('reason') if not result else 'fact-validation',
                    'inputHash': digest,
                })
