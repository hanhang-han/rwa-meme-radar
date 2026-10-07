"""Bounded identity-only logo reads; independent of market projection freshness."""
import asyncio
import re
import time
from collections import OrderedDict
from urllib.parse import urlsplit

from .db import store

CHAINS = {'196', '56', '4663', '5042'}
_ID = re.compile(r'(196|56|4663|5042):(0x[0-9a-fA-F]{40})\Z')
_cache = OrderedDict()
_lock = asyncio.Lock()


def parse_ids(value):
    if not isinstance(value, str) or not value or len(value) > 2500:
        raise ValueError('invalid-logo-identities')
    parts = value.split(',')
    if not 1 <= len(parts) <= 50 or any(not _ID.fullmatch(part) for part in parts):
        raise ValueError('invalid-logo-identities')
    return list(dict.fromkeys(part.lower() for part in parts))


def safe_logo(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return ''
    try:
        url = urlsplit(value)
        return value if url.scheme == 'https' and url.hostname and not url.username and not url.password else ''
    except ValueError:
        return ''


async def asset_logos(identities):
    # Serialize misses, not provider requests. Existing facts use the indexed
    # (kind,id) key; only the logo field is decoded, never complete profiles.
    async with _lock:
        now = time.monotonic()
        missing = [key for key in identities if key not in _cache or _cache[key][0] <= now]
        found = {}
        for chain in sorted({key.split(':')[0] for key in missing}):
            addresses = [key.split(':')[1] for key in missing if key.startswith(chain+':')]
            scoped = await store(chain)
            kinds = [scoped.key('stock'), scoped.key('asset')]
            placeholders = ','.join('?' for _ in addresses)
            rows = await scoped.fetchall(
                "SELECT kind,id,json_extract(body,'$.logoUrl') FROM facts "
                f"WHERE kind IN (?,?) AND id IN ({placeholders}) ORDER BY kind DESC",
                (*kinds, *addresses))
            for kind, address, value in rows:
                key = chain+':'+address.lower()
                logo = safe_logo(value)
                if logo and key in missing and key not in found:
                    found[key] = logo
        for key in missing:
            logo = found.get(key, '')
            _cache[key] = (time.monotonic()+(300 if logo else 60), logo)
        result = {}
        for key in identities:
            _, logo = _cache[key]
            _cache.move_to_end(key)
            if logo:
                result[key] = logo
        while len(_cache) > 2048:
            _cache.popitem(last=False)
        return {'logos': result}
