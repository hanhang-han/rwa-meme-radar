"""Build a release-pinned issuer manifest from an audited /rhj/assets response."""
import datetime
import hashlib
import json
import pathlib
import re
import sys

source_path, captured_at, output_path = sys.argv[1:]
raw = pathlib.Path(source_path).read_bytes()
datetime.datetime.fromisoformat(captured_at.replace('Z', '+00:00'))
assets = json.loads(raw)['assets']
assert isinstance(assets, list) and assets
rows, ids, contracts = [], set(), set()
for item in assets:
    ident, ticker, isin = item['id'], item['tokenSymbol'], item['isin']
    assert re.fullmatch(r'0x[0-9a-fA-F]{64}', ident) and ident not in ids
    assert re.fullmatch(r'[A-Z0-9.\-]{1,24}', ticker)
    assert re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}\d', isin)
    ids.add(ident)
    deployments = [d for d in item['deployments'] if str(d['chainId']) == '4663']
    assert len(deployments) == 1
    address = deployments[0]['contractAddress'].lower()
    assert re.fullmatch(r'0x[0-9a-f]{40}', address) and int(address, 16) and address not in contracts
    contracts.add(address)
    rows.append({'assetId': ident, 'ticker': ticker, 'tokenSymbol': ticker,
                 'nameEn': item['tokenName'], 'underlyingIsin': isin,
                 'assetStatus': item['status'], 'deployments': {'4663': {'native': address}}})
manifest = {'version': 'robinhood-assets-v1-'+captured_at[:10],
            'provenance': 'official-api-pinned-snapshot',
            'issuer': 'Robinhood Assets (Jersey) Limited',
            'sourceUrl': 'https://api.robinhood.com/rhj/assets',
            'sourceDocs': ['https://api.robinhood.com/rhj/assets'],
            'capturedAt': captured_at, 'sourceSha256': hashlib.sha256(raw).hexdigest(),
            'assets': sorted(rows, key=lambda row: row['ticker'])}
pathlib.Path(output_path).write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
print('Pinned Robinhood issuer deployments:', len(contracts))
