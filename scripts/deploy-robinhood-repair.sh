#!/usr/bin/env bash
# Publish only reviewed Robinhood identity and collection files; retain the UI.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
EVIDENCE="${1:?Pass evidence directory}"
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o IdentitiesOnly=yes -i "${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}")
PACKAGE=$(mktemp /tmp/cliperx-robinhood-repair.XXXXXX.tar.gz)
trap 'rm -f "$PACKAGE"' EXIT
python3 - "$EVIDENCE" "$PACKAGE" <<'PY'
import hashlib,json,pathlib,sys,tarfile
e=pathlib.Path(sys.argv[1]);baseline=json.loads((e/'release-baseline.json').read_text())
allowed={'server-py/app/stock_identity.py','server-py/app/collectors/pool_market.py',
         'server-py/app/collectors/discovery_coverage.py','src/lib/stock-identity.ts',
         'src/catalogues/robinhood-stock-tokens.v1.json'}
paths=json.loads((e/'release-files.json').read_text())
assert set(paths)==allowed and len(paths)==len(allowed)
release={'indexBefore':baseline['indexBefore'],'files':{}}
for name in paths:
 p=pathlib.Path(name);assert p.is_file() and not p.is_symlink()
 release['files'][name]={'before':baseline['files'][name],'after':hashlib.sha256(p.read_bytes()).hexdigest()}
(e/'release.json').write_text(json.dumps(release,indent=2))
with tarfile.open(sys.argv[2],'w:gz') as package:
 for name in paths:package.add(name,arcname=name)
 package.add('server-py/tests',arcname='tests')
 package.add(e/'release.json',arcname='release.json')
print('Packaged 5 reviewed Robinhood source files')
PY
REMOTE_PACKAGE="/tmp/$(basename "$PACKAGE")"
ssh "${SSH_OPTS[@]}" "$SERVER" "cat > $REMOTE_PACKAGE" < "$PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$REMOTE_PACKAGE" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
mkdir -p .releases
exec 9>.releases/pool-pipeline-repair.lock
exec 8>.releases/dashboard-web-only-deploy.lock
flock -n 8 || { printf 'Another dashboard release is running\n' >&2; exit 1; }
flock -n 9 || { printf 'Another pool release is running\n' >&2; exit 1; }
STAGING=$(realpath "$(mktemp -d .releases/robinhood-staging.XXXXXX)")
BACKUP=".releases/robinhood-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
export STAGING BACKUP
SERVICES=(memedashboard pyradar pyradar-worker pyradar-projection pyradar-market pyradar-market-api)
CHANGED=0
COMPLETE=0
wait_ready() {
 for attempt in {1..35}; do
  if curl -fsS --max-time 2 "http://127.0.0.1:$1/api/health/ready" -o /dev/null 2>/dev/null; then return 0; fi
  sleep 2
 done
 return 1
}
finish() {
 result=$?
 trap - EXIT
 if [ "$CHANGED" -eq 1 ] && [ "$COMPLETE" -ne 1 ]; then
  set +e
  pm2 stop "${SERVICES[@]}" >/dev/null 2>&1
  python3 - <<'PY'
import json,os,pathlib,shutil
b=pathlib.Path(os.environ['BACKUP']);m=json.loads((b/'release.json').read_text())
for name,row in m['files'].items():
 if row['before'] is None:pathlib.Path(name).unlink(missing_ok=True)
 else:shutil.copy2(b/name,name)
if (b/'pool-market.json').exists():shutil.copy2(b/'pool-market.json','data/pool-market.json')
PY
  pm2 restart "${SERVICES[@]}" >/dev/null 2>&1
  wait_ready 8010
  wait_ready 8011
  printf 'Rolled back Robinhood release; backup: %s\n' "$BACKUP" >&2
 fi
 rm -f "$REMOTE_PACKAGE"
 exit "$result"
}
REMOTE_PACKAGE="$1"
trap finish EXIT
tar xzf "$REMOTE_PACKAGE" -C "$STAGING"
mkdir -p "$STAGING/validation/server-py" "$STAGING/validation/src/lib" "$STAGING/validation/contracts"
cp -a server-py/app "$STAGING/validation/server-py/"
cp -a src/catalogues "$STAGING/validation/src/"
cp -a src/lib/stock-identity.ts "$STAGING/validation/src/lib/"
cp -a contracts/artifacts "$STAGING/validation/contracts/"
cp -a "$STAGING/tests" "$STAGING/validation/server-py/tests"
python3 - <<'PY'
import hashlib,json,os,pathlib,shutil
s=pathlib.Path(os.environ['STAGING']);b=pathlib.Path(os.environ['BACKUP']);m=json.loads((s/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name);assert not p.is_symlink(),name
 assert (hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None)==row['before'],name+' changed since audit'
 assert hashlib.sha256((s/p).read_bytes()).hexdigest()==row['after'],name
 if p.exists():
  target=b/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
 target=s/'validation'/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(s/p,target)
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore'],'Dashboard changed since audit'
shutil.copy2(s/'release.json',b/'release.json')
print('Production baseline verified; rollback source saved')
PY
NODE_ENV=test PYTHONPATH="$STAGING/validation/server-py" PYTHONDONTWRITEBYTECODE=1 server-py/.venv/bin/python - <<'PY'
import asyncio,os,pathlib,tempfile,unittest
from app import db
with tempfile.TemporaryDirectory(prefix='cliperx-rh-validation-') as folder:
 db.DB_PATH=folder+'/research.sqlite'
 root=pathlib.Path(os.environ['STAGING'])/'validation/server-py/tests'
 suite=unittest.TestSuite()
 for pattern in ('test_robinhood_identity.py','test_stock_identity.py','test_pool_market.py',
                 'test_discovery_coverage.py','test_factory_discovery.py','test_pool_gap_repair.py',
                 'test_stock_quotes.py','test_product_v2.py','test_dashboard_projection.py',
                 'test_stock_metric_contract.py','test_equity_market.py'):
  suite.addTests(unittest.defaultTestLoader.discover(str(root),pattern=pattern))
 result=unittest.TextTestRunner(verbosity=1).run(suite)
 asyncio.run(db.close_all())
 assert result.wasSuccessful(),'Candidate regression failed'
PY
NODE_ENV=test node --import tsx --input-type=module - <<'JS'
const m=await import(process.env.STAGING+'/validation/src/lib/stock-identity.ts');
const address='0x117cc2133c37b721f49de2a7a74833232b3b4c0c';
if(m.officialStockIdentity('4663',address).ticker!=='SPY')throw Error('Robinhood mapping failed');
if(m.officialStockIdentity('56',address).eligibleForPair)throw Error('Wrong-network authority');
console.log('Candidate Node issuer mapping verified');
JS
# Fetch just the Robinhood lane into staging. The install merges it with the
# latest other-chain observations after producers stop, avoiding stale overwrites.
PYTHONPATH="$STAGING/validation/server-py" PYTHONDONTWRITEBYTECODE=1 server-py/.venv/bin/python - <<'PY'
import asyncio,json,os,pathlib
from app.collectors.pool_market import PoolMarketCollector
from app.stock_identity import assess_pool_relation
from app.live_market_store import close_all
async def run():
 collector=PoolMarketCollector(path=str(pathlib.Path(os.environ['STAGING'])/'warm-robinhood.json'))
 try:
  metadata={k:v for k,v in (await collector.catalogue()).items() if v['chainId']=='4663'}
  assert len(metadata)==17,'Robinhood relation set changed; inspect before publishing'
  result=await asyncio.wait_for(collector.run_once(metadata=metadata),40)
  qualified=sum(assess_pool_relation({**metadata[k],**r})['level']=='A' for k,r in collector.body['pools'].items())
  print('Robinhood source preflight:',json.dumps({**result,'qualified':qualified}))
  assert result['accepted']==17 and qualified>=1,'Incomplete Robinhood pool observations'
 finally:await close_all()
asyncio.run(run())
PY
python3 - <<'PY'
import hashlib,json,os,pathlib
m=json.loads((pathlib.Path(os.environ['STAGING'])/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name)
 assert (hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None)==row['before'],name
PY
CHANGED=1
pm2 stop "${SERVICES[@]}" >/dev/null
python3 - <<'PY'
import hashlib,json,os,pathlib,shutil
s=pathlib.Path(os.environ['STAGING']);b=pathlib.Path(os.environ['BACKUP']);m=json.loads((s/'release.json').read_text())
p=pathlib.Path('data/pool-market.json')
if p.exists():shutil.copy2(p,b/'pool-market.json')
body=json.loads(p.read_text()) if p.exists() else {}
warm=json.loads((s/'warm-robinhood.json').read_text())
for field in ('pools','attempts'):
 target=body.setdefault(field,{})
 for key,row in warm[field].items():
  assert key.startswith('4663:')
  clock='liquidityAt' if field=='pools' else 'lastAttemptAt'
  if row.get(clock,0)>target.get(key,{}).get(clock,0):target[key]=row
tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(body,separators=(',',':'),allow_nan=False));os.replace(tmp,p)
for name,row in m['files'].items():
 p=pathlib.Path(name);p.parent.mkdir(parents=True,exist_ok=True)
 tmp=p.with_name(p.name+'.rh-release');shutil.copy2(s/p,tmp);os.replace(tmp,p)
 assert hashlib.sha256(p.read_bytes()).hexdigest()==row['after']
print('Installed reviewed issuer catalogue and Robinhood pool observations')
PY
pm2 restart "${SERVICES[@]}" >/dev/null
wait_ready 8010
wait_ready 8011
server-py/.venv/bin/python - <<'PY'
import json,time,urllib.request
for attempt in range(30):
 try:
  with urllib.request.urlopen('http://127.0.0.1:8010/api/dashboard?view=overview',timeout=5) as r:d=json.loads(r.read())
  u=d['unified'];metrics=u.get('metricsByChain',{}).get('4663',{});cards=u.get('hotStocksByChain',{}).get('4663',[])
  if metrics.get('verifiedPools',0)>0 and cards:
   print('Published Robinhood pools:',metrics['verifiedPools'],'home themes:',[r['ticker'] for r in cards]);break
 except Exception:pass
 time.sleep(2)
else:raise AssertionError('Robinhood homepage publication did not recover')
PY
python3 - <<'PY'
import hashlib,json,os,pathlib,subprocess
m=json.loads((pathlib.Path(os.environ['STAGING'])/'release.json').read_text())
for name,row in m['files'].items():assert hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()==row['after']
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore']
rows=json.loads(subprocess.check_output(['/www/server/nodejs/v22.22.0/bin/pm2','jlist']))
names={'memedashboard','pyradar','pyradar-worker','pyradar-projection','pyradar-market','pyradar-market-api'}
services=[{'name':r['name'],'status':r['pm2_env']['status'],'pid':r['pid']} for r in rows if r['name'] in names]
assert len(services)==6 and all(r['status']=='online' for r in services)
print(json.dumps({'services':services,'backup':os.environ['BACKUP'],'uiPreserved':True}))
PY
COMPLETE=1
printf 'Robinhood repair deployed; backup: %s\n' "$BACKUP"
REMOTE
