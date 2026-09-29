#!/usr/bin/env python3
"""Read-only release checks against the actual public data contract."""
import argparse
import gzip
import json
import urllib.request


def verify(base):
    def get(path):
        request=urllib.request.Request(base.rstrip('/')+path,headers={'Accept-Encoding':'gzip'})
        with urllib.request.urlopen(request,timeout=90) as response:
            body=response.read()
            if response.headers.get('Content-Encoding')=='gzip':body=gzip.decompress(body)
            return json.loads(body)
    health=get('/health/data')
    if not health.get('worker',{}).get('ok'):
        raise RuntimeError('Worker heartbeat unavailable')
    data=get('/dashboard')['unified']
    assets,stocks,relations=data['assets'],data['stockTokens'],data['relations']
    assert len(assets)>=1000, 'Candidate inventory unexpectedly shrank'
    assert len(stocks)>=2700, 'Stock inventory unexpectedly shrank'
    assert not any(s.get('referenceSymbol') in ('SLV.WAR','QQQ.BA') for s in stocks),'Quarantined identity escaped'
    binance=[s for s in stocks if s.get('provider')=='Binance']
    assert len(binance)>=29, 'Binance shared quotes are missing'
    assert all(s.get('priceCurrency')=='USDT' and s.get('priceScope')=='exchange' for s in binance)
    refs=[s for s in stocks if s.get('stockPrice') is not None]
    assert all(s.get('referenceAt') and s.get('referenceCurrency') and s.get('referenceProvider') for s in refs),'Incomplete reference metadata'
    sample=binance[0]
    detail=get('/token/56/'+sample['tokenContractAddress'])
    assert detail['asset']['priceScope']=='exchange' and detail['asset']['priceCurrency']=='USDT'
    assert detail['asset']['buys24h'] is None and detail['asset']['sells24h'] is None,'DEX counts leaked into exchange card'
    assert detail.get('tradesScope')=='dex'
    comparison=get('/comparisons/56/'+sample['tokenContractAddress'])
    return {'assets':len(assets),'stocks':len(stocks),'relations':len(relations),
            'binanceQuotes':len(binance),'referencesWithMetadata':len(refs),
            'poolEnriched':sum(bool(r.get('liquidityProvider')) for r in relations),
            'coverage':health.get('coverage'),'dataStatus':health.get('status'),'issues':health.get('issues'),
            'sourceStatus':{s.get('provider'):s.get('status') for s in data.get('sources',[])},
            'comparisonResponded':isinstance(comparison,dict),'disk':health.get('disk')}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',default='https://cliperx.com/dashboard/api')
    args=parser.parse_args()
    print(json.dumps(verify(args.base),ensure_ascii=False,indent=2))
