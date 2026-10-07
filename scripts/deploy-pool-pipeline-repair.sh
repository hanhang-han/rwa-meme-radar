#!/usr/bin/env bash
# Reviewed stock data/UI release. No configuration, credentials or database migrations.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
EVIDENCE="${1:?Pass the stock repair evidence directory}"
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=25 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o IdentitiesOnly=yes -i "${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}")
PACKAGE=$(mktemp /tmp/cliperx-stock-repair.XXXXXX.tar.gz)
trap 'rm -f "$PACKAGE"' EXIT
python3 - "$EVIDENCE" "$PACKAGE" <<'PY'
import hashlib,json,pathlib,sys,tarfile
e=pathlib.Path(sys.argv[1]);before=json.loads((e/'release-baseline.json').read_text());paths=json.loads((e/'release-files.json').read_text())
assert paths and len(paths)==len(set(paths))
files={}
for name in paths:
 p=pathlib.Path(name)
 assert not p.is_absolute() and '..' not in p.parts and p.is_file() and not p.is_symlink()
 assert name.startswith(('server-py/app/','web/src/')) or name in ('src/lib/stock-identity.ts','src/catalogues/hk-underlyings.v1.json')
 files[name]={'before':before['files'][name],'after':hashlib.sha256(p.read_bytes()).hexdigest()}
index=pathlib.Path('web/dist/index.html')
assert '/dashboard/assets/' in index.read_text()
m={'files':files,'indexBefore':before['indexBefore'],'indexAfter':hashlib.sha256(index.read_bytes()).hexdigest()}
(e/'release.json').write_text(json.dumps(m,indent=2))
with tarfile.open(sys.argv[2],'w:gz') as package:
 for name in files:package.add(name,arcname=name)
 package.add('web/dist',arcname='web/dist')
 package.add('server-py/tests',arcname='tests')
 package.add(e/'release.json',arcname='release.json')
print('Packaged reviewed source files:',len(files))
PY
REMOTE_PACKAGE="/tmp/$(basename "$PACKAGE")"
ssh "${SSH_OPTS[@]}" "$SERVER" "cat > $REMOTE_PACKAGE" < "$PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$REMOTE_PACKAGE" "${DEPLOY_PREFLIGHT_ONLY:-0}" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
mkdir -p .releases
exec 9>.releases/pool-pipeline-repair.lock
exec 8>.releases/dashboard-web-only-deploy.lock
flock -n 8 || { printf 'A dashboard release is still running\n' >&2; exit 1; }
flock -n 9 || { printf 'Another stock repair release is running\n' >&2; exit 1; }
STAGING=$(mktemp -d .releases/pool-pipeline-staging.XXXXXX)
STAGING=$(realpath "$STAGING")
BACKUP=".releases/pool-pipeline-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
export STAGING BACKUP
CHANGED=0
COMPLETE=0
SERVICES=(memedashboard pyradar pyradar-worker pyradar-projection pyradar-market pyradar-market-api)
wait_ready() {
 local port="$1"
 for attempt in {1..45}; do
  if curl -fsS --max-time 2 "http://127.0.0.1:$port/api/health/ready" -o /dev/null 2>/dev/null; then return 0; fi
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
  rollback=0
  python3 - <<'PY'
import hashlib,json,os,pathlib,shutil
b=pathlib.Path(os.environ['BACKUP']);m=json.loads((b/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name)
 if row['before'] is None:p.unlink(missing_ok=True)
 else:shutil.copy2(b/p,p)
 assert (hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None)==row['before']
if (b/'pool-market.json').exists():shutil.copy2(b/'pool-market.json','data/pool-market.json')
else:pathlib.Path('data/pool-market.json').unlink(missing_ok=True)
if (b/'web-dist').exists():
 for p in pathlib.Path('web/dist/assets').rglob('*'):
  if p.is_file():
   target=b/'web-dist/assets'/p.relative_to('web/dist/assets');target.parent.mkdir(parents=True,exist_ok=True)
   if not target.exists():shutil.copy2(p,target)
 if pathlib.Path('web/dist').exists():shutil.move('web/dist',b/'failed-web-dist')
 shutil.move(b/'web-dist','web/dist')
PY
  if [ "$?" -ne 0 ]; then rollback=1; fi
  pm2 restart "${SERVICES[@]}" >/dev/null 2>&1 || rollback=1
  wait_ready 8010 || rollback=1
  wait_ready 8011 || rollback=1
  if [ "$rollback" -eq 0 ]; then printf 'Release rolled back to %s\n' "$BACKUP" >&2;
  else printf 'Rollback needs inspection: %s\n' "$BACKUP" >&2; fi
 fi
 rm -f "$1" 2>/dev/null || true
 exit "$result"
}
REMOTE_PACKAGE="$1"
PREFLIGHT_ONLY="${2:-0}"
trap 'finish "$REMOTE_PACKAGE"' EXIT
tar xzf "$REMOTE_PACKAGE" -C "$STAGING"
mkdir -p "$STAGING/validation/server-py" "$STAGING/validation/src" "$STAGING/validation/contracts"
cp -a server-py/app "$STAGING/validation/server-py/"
cp -a src/catalogues "$STAGING/validation/src/"
cp -a contracts/artifacts "$STAGING/validation/contracts/"
cp -a "$STAGING/tests" "$STAGING/validation/server-py/tests"
mkdir -p "$STAGING/validation/src/lib"
cp -a src/lib/stock-identity.ts "$STAGING/validation/src/lib/"
python3 - <<'PY'
import hashlib,json,os,pathlib,shutil
s=pathlib.Path(os.environ['STAGING']);b=pathlib.Path(os.environ['BACKUP']);m=json.loads((s/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name)
 assert not p.is_symlink(),name
 actual=hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
 assert actual==row['before'],f'Production source changed after audit: {name}'
 assert hashlib.sha256((s/p).read_bytes()).hexdigest()==row['after'],name
 if p.exists():
  target=b/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
 target=s/'validation'/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(s/p,target)
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore'],'Dashboard changed since audit'
shutil.copy2(s/'release.json',b/'release.json')
if pathlib.Path('data/pool-market.json').exists():shutil.copy2('data/pool-market.json',b/'pool-market.json')
shutil.copytree('web/dist',b/'web-dist')
print('Production source and dashboard baseline verified; rollback ready')
PY
NODE_ENV=test PYTHONPATH="$STAGING/validation/server-py" PYTHONDONTWRITEBYTECODE=1 server-py/.venv/bin/python - <<'PY'
import asyncio,os,pathlib,tempfile,unittest
from app import db
from app.main import app
assert '/api/v2/stocks/{ticker}/equity-chart' in app.openapi()['paths']
with tempfile.TemporaryDirectory(prefix='cliperx-stock-validation-') as folder:
 db.DB_PATH=folder+'/research.sqlite'
 root=pathlib.Path(os.environ['STAGING'])/'validation/server-py/tests'
 suite=unittest.TestSuite()
 for pattern in ('test_background_resource_budget.py','test_collector_repair.py','test_discovery_coverage.py','test_pool_gap_repair.py','test_pool_market.py','test_comparisons.py','test_stock_identity.py','test_product_v2.py','test_dashboard_projection.py','test_live_market_api.py','test_hot_market_isolation.py','test_stock_metric_contract.py'):
  suite.addTests(unittest.defaultTestLoader.discover(str(root),pattern=pattern))
 result=unittest.TextTestRunner(verbosity=1).run(suite)
 asyncio.run(db.close_all())
 assert result.wasSuccessful(),'Candidate regression failure'
PY
NODE_ENV=test node --import tsx --input-type=module - <<'JS'
const m=await import(process.env.STAGING+'/validation/src/lib/stock-identity.ts');
for(const [code,symbol] of [['1024','KUAIx'],['9992','POPMTx']]){
 const identity=m.stockIdentity({stockCode:code,tokenSymbol:symbol});
 if(identity.market!=='HKEX'||identity.id!=='XHKG:'+code.padStart(5,'0'))throw Error('Security mapping failed');
}
console.log('Candidate Node security mapping verified');
JS
# Read-only catalogue plus bounded free pool requests; stage observations with
# their actual clocks. Candidate verification does not modify production facts.
PYTHONPATH="$STAGING/validation/server-py" PYTHONDONTWRITEBYTECODE=1 server-py/.venv/bin/python - <<'PYWARM'
import asyncio,json,os,pathlib,shutil,time
from app.collectors.pool_market import PoolMarketCollector
from app.live_market_store import close_all
async def run():
 path=pathlib.Path(os.environ['STAGING'])/'warm-pool-market.json'
 if pathlib.Path('data/pool-market.json').exists():shutil.copy2('data/pool-market.json',path)
 collector=PoolMarketCollector(path=str(path))
 try:
  result=await asyncio.wait_for(collector.run_once(),75)
  print('Direct pool source preflight:',json.dumps(result))
  current=sum(0<=time.time()*1000-r.get('liquidityAt',0)<900000 for r in collector.body.get('pools',{}).values())
  assert result['accepted']>0 or current>0,'No current verified direct-pool USD observations from public sources'
  if not path.exists():await collector.persist()
 finally:
  await close_all()
asyncio.run(run())
PYWARM
if [ "$PREFLIGHT_ONLY" = "1" ]; then printf 'Preflight passed; no running service changed\n'; COMPLETE=1; exit 0; fi
# Recheck immediately before publishing, detecting another deployment.
python3 - <<'PY'
import hashlib,json,os,pathlib
m=json.loads((pathlib.Path(os.environ['STAGING'])/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name);assert (hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None)==row['before'],name
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore']
PY
CHANGED=1
pm2 stop pyradar-worker pyradar-projection pyradar-market >/dev/null
python3 - <<'PY'
import json,os,pathlib,shutil
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for name in m['files']:
 p=pathlib.Path(name);p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(s/p,p)
shutil.copy2(s/'warm-pool-market.json','data/pool-market.json.tmp')
os.replace('data/pool-market.json.tmp','data/pool-market.json')
PY
pm2 restart pyradar-market >/dev/null
pm2 restart pyradar-market-api >/dev/null
wait_ready 8011
pm2 restart pyradar >/dev/null
wait_ready 8010
pm2 restart memedashboard >/dev/null
pm2 restart pyradar-worker >/dev/null
pm2 restart pyradar-projection >/dev/null
DATA_READY=0
for attempt in {1..45}; do
 if python3 - <<'PYREAD'
import json,urllib.request
with urllib.request.urlopen('http://127.0.0.1:8010/api/v2/stocks?chain=all&sort=related&limit=20',timeout=5) as r:d=json.load(r)
rows=d['unified']['stockThemes'];assert rows
assert all('poolCoverage' in row['theme'] for row in rows)
PYREAD
 then DATA_READY=1; break; fi
 sleep 2
done
test "$DATA_READY" -eq 1
# Retain old immutable assets, then exchange the static directory atomically.
python3 - <<'PY'
import ctypes,hashlib,json,os,pathlib,shutil
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for p in pathlib.Path('web/dist/assets').rglob('*'):
 if p.is_file():
  target=s/'web/dist/assets'/p.relative_to('web/dist/assets');target.parent.mkdir(parents=True,exist_ok=True)
  if target.exists():assert target.read_bytes()==p.read_bytes(),f'Immutable asset collision: {p.name}'
  else:shutil.copy2(p,target)
libc=ctypes.CDLL(None,use_errno=True)
if libc.renameat2(-100,b'web/dist',-100,os.fsencode(str(s/'web/dist')),2):raise OSError(ctypes.get_errno(),'Static exchange failed')
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexAfter']
PY
curl --compressed -fsS --max-time 15 https://cliperx.com/dashboard/ -o "$STAGING/public-index.html"
python3 - <<'PY'
import hashlib,json,os,pathlib,subprocess,time
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
assert hashlib.sha256((s/'public-index.html').read_bytes()).hexdigest()==m['indexAfter']
for name,row in m['files'].items():assert hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()==row['after'],name
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True));names={'memedashboard','pyradar','pyradar-worker','pyradar-projection','pyradar-market','pyradar-market-api'}
safe=[{'name':r['name'],'pid':r['pid'],'status':r['pm2_env']['status']} for r in rows if r['name'] in names]
assert len(safe)==6 and all(r['pid']>0 and r['status']=='online' for r in safe)
print(json.dumps({'at':int(time.time()*1000),'services':safe,'backup':os.environ['BACKUP']},ensure_ascii=False))
PY
COMPLETE=1
printf 'Pool pipeline repair live; rollback: %s\n' "$BACKUP"
REMOTE
