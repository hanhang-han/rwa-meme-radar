#!/usr/bin/env bash
# Publish only the reviewed live-market files; preserve both SQLite databases.
# Usage: deploy-isolated-market.sh BASELINE.json FILES.json [--web]
set -Eeuo pipefail
cd "$(dirname "$0")/.."
BASELINE="${1:?Pass the read-only production source baseline JSON}"
FILES="${2:?Pass the reviewed source whitelist JSON}"
WITH_WEB=0
if [ "${3:-}" = '--web' ]; then WITH_WEB=1; fi
if [ "$#" -gt 3 ] || { [ "$#" -eq 3 ] && [ "$3" != '--web' ]; }; then
  printf 'Only optional --web is supported\n' >&2
  exit 2
fi
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o IdentitiesOnly=yes -i "${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}")
PACKAGE=$(mktemp /tmp/cliperx-live-market.XXXXXX.tar.gz)
MANIFEST=$(mktemp /tmp/cliperx-live-market.XXXXXX.json)
trap 'rm -f "$PACKAGE" "$MANIFEST"' EXIT
python3 - "$BASELINE" "$FILES" "$MANIFEST" "$PACKAGE" "$WITH_WEB" <<'PY'
import hashlib,json,pathlib,sys,tarfile
allowed={
 'server-py/app/collectors/chain_stream.py',
 'server-py/app/collectors/live_market.py',
 'server-py/app/collectors/factory_discovery.py',
 'server-py/app/market_worker.py',
 'server-py/app/market_main.py',
 'server-py/app/live_market_store.py',
 'server-py/app/api/live_market.py',
 'server-py/app/main.py',
 'server-py/app/worker.py',
 'server-py/app/projection_worker.py',
 'ecosystem.config.cjs',
 'nginx/dashboardv2.conf',
 'nginx/live-market.conf',
}
baseline=json.loads(pathlib.Path(sys.argv[1]).read_text())
spec=json.loads(pathlib.Path(sys.argv[2]).read_text())
paths=spec['paths'] if isinstance(spec,dict) else spec
frozen=spec.get('sha256') if isinstance(spec,dict) else None
assert isinstance(paths,list) and paths and len(paths)==len(set(paths))
assert set(paths)==allowed,'Release must contain exactly the thirteen reviewed source/config files'
if frozen is not None:assert set(frozen)==set(paths),'Frozen source manifest incomplete'
for name in paths:
 p=pathlib.Path(name)
 assert p.is_file() and not p.is_symlink(),f'Missing source: {name}'
 assert name in baseline,f'Missing explicit production baseline: {name}'
 before=baseline[name]
 assert before is None or (isinstance(before,str) and len(before)==64 and all(c in '0123456789abcdef' for c in before))
 if frozen is not None:assert hashlib.sha256(p.read_bytes()).hexdigest()==frozen[name],f'Local source changed after freeze: {name}'
manifest={'files':{p:{'before':baseline[p],'after':hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()} for p in paths},'web':bool(int(sys.argv[5]))}
if manifest['web']:
 index=pathlib.Path('web/dist/index.html')
 assert index.is_file() and '/dashboard/assets/' in index.read_text()
 assert not any(p.is_symlink() for p in pathlib.Path('web/dist').rglob('*')),'Symlinks in dashboard package'
 manifest['index']=hashlib.sha256(index.read_bytes()).hexdigest()
pathlib.Path(sys.argv[3]).write_text(json.dumps(manifest))
with tarfile.open(sys.argv[4],'w:gz') as out:
 for p in paths:out.add(p,arcname=p,recursive=False)
 if manifest['web']:out.add('web/dist',arcname='web/dist')
 out.add(sys.argv[3],arcname='release.json')
print('Reviewed live-market release:',len(paths),'source files; dashboard:',manifest['web'])
PY
REMOTE_PACKAGE="/tmp/$(basename "$PACKAGE")"
ssh "${SSH_OPTS[@]}" "$SERVER" "cat > $REMOTE_PACKAGE" < "$PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$REMOTE_PACKAGE" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
mkdir -p .releases
exec 9>.releases/isolated-market-deploy.lock
flock -n 9 || { printf 'Another isolated-market release is running\n' >&2; exit 1; }
STAGING=$(mktemp -d .releases/isolated-market-staging.XXXXXX)
BACKUP=".releases/isolated-market-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
export STAGING BACKUP
CHANGED=0
WEB_MOVED=0
COMPLETE=0
NGINX_CHANGED=0
pm2 jlist | python3 -c 'import json,sys,os,pathlib; rows=json.load(sys.stdin); names={"pyradar","pyradar-worker","pyradar-projection","pyradar-market","pyradar-market-api"}; selected={p["name"]:{"pid":p.get("pid"),"status":p.get("pm2_env",{}).get("status")} for p in rows if p.get("name") in names};assert all(selected.get(n,{}).get("status")=="online" for n in ("pyradar","pyradar-worker","pyradar-projection")),"Existing Python services must be online before deployment";pathlib.Path(os.environ["BACKUP"],"services-before.json").write_text(json.dumps(selected))'
MARKET_EXISTED=$(python3 -c 'import json,os,pathlib;print(int("pyradar-market" in json.loads(pathlib.Path(os.environ["BACKUP"],"services-before.json").read_text())))')
OLD_MARKET_STATUS=$(python3 -c 'import json,os,pathlib;print(json.loads(pathlib.Path(os.environ["BACKUP"],"services-before.json").read_text()).get("pyradar-market",{}).get("status","absent"))')
MARKET_API_EXISTED=$(python3 -c 'import json,os,pathlib;print(int("pyradar-market-api" in json.loads(pathlib.Path(os.environ["BACKUP"],"services-before.json").read_text())))')
OLD_MARKET_API_STATUS=$(python3 -c 'import json,os,pathlib;print(json.loads(pathlib.Path(os.environ["BACKUP"],"services-before.json").read_text()).get("pyradar-market-api",{}).get("status","absent"))')
OLD_MARKET_PID=$(pm2 pid pyradar-market 2>/dev/null || true)
NODE_PID=$(pm2 pid memedashboard)
export NODE_PID
finish() {
 result=$?
 trap - EXIT
 if [ "$COMPLETE" -ne 1 ] && [ "$CHANGED" -eq 1 ]; then
  set +e
  ROLLBACK_OK=1
  for service in pyradar-market-api pyradar-market pyradar pyradar-worker pyradar-projection; do pm2 stop "$service" >/dev/null 2>&1 || true; done
  python3 - <<'PY'
import hashlib,json,os,pathlib,shutil,tempfile
b=pathlib.Path(os.environ['BACKUP']);m=json.loads((b/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name)
 if row['before'] is None:p.unlink(missing_ok=True)
 else:
  with tempfile.NamedTemporaryFile(dir=p.parent,delete=False) as f:tmp=pathlib.Path(f.name)
  shutil.copy2(b/p,tmp);os.replace(tmp,p)
 actual=hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
 assert actual==row['before'],f'Restored source mismatch: {name}'
PY
  if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  if [ "$NGINX_CHANGED" -eq 1 ]; then
   sudo -n /www/server/nginx/sbin/nginx -t
   if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; else
    sudo -n /www/server/nginx/sbin/nginx -s reload
    if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
   fi
  fi
  if [ "$WEB_MOVED" -eq 1 ]; then
   cp -an web/dist/assets/. "$BACKUP/web-dist/assets/"
   if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
   python3 - <<'PY'
import ctypes,os
libc=ctypes.CDLL(None,use_errno=True)
if libc.renameat2(-100,b'web/dist',-100,os.fsencode(os.environ['BACKUP']+'/web-dist'),2):raise OSError(ctypes.get_errno(),'Dashboard restore failed')
PY
   if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  fi
  for service in pyradar pyradar-worker pyradar-projection; do
   pm2 restart ecosystem.config.cjs --only "$service" --update-env >/dev/null
   if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  done
  if [ "$MARKET_EXISTED" -eq 1 ] && [ "$OLD_MARKET_STATUS" = 'online' ]; then
   pm2 restart ecosystem.config.cjs --only pyradar-market --update-env >/dev/null
   if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  elif [ "$MARKET_EXISTED" -eq 0 ]; then
   pm2 delete pyradar-market >/dev/null 2>&1 || true
  fi
  if [ "$MARKET_API_EXISTED" -eq 1 ] && [ "$OLD_MARKET_API_STATUS" = 'online' ]; then
   pm2 restart ecosystem.config.cjs --only pyradar-market-api --update-env >/dev/null
   if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  elif [ "$MARKET_API_EXISTED" -eq 0 ]; then
   pm2 delete pyradar-market-api >/dev/null 2>&1 || true
  fi
  RESTORED_READY=0
  for attempt in {1..16}; do
   if curl -fsS --max-time 15 http://127.0.0.1:8010/api/health/ready -o /dev/null; then RESTORED_READY=1; break; fi
   sleep 2
  done
  if [ "$RESTORED_READY" -ne 1 ]; then ROLLBACK_OK=0; fi
  pm2 save >/dev/null
  if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  if [ "$ROLLBACK_OK" -eq 1 ]; then
   printf 'Release failed; prior services and source restored. Both databases retained. Backup: %s\n' "$BACKUP" >&2
  else
   printf 'Release failed; rollback incomplete. Both databases retained. Inspect: %s\n' "$BACKUP" >&2
  fi
 fi
 rm -f "$1" 2>/dev/null || true
 exit "$result"
}
REMOTE_PACKAGE="$1"
trap 'finish "$REMOTE_PACKAGE"' EXIT
python3 - "$REMOTE_PACKAGE" <<'PY'
import json,os,pathlib,tarfile
s=pathlib.Path(os.environ['STAGING'])
with tarfile.open(__import__('sys').argv[1]) as archive:
 m=json.load(archive.extractfile('release.json'))
 for item in archive.getmembers():
  p=pathlib.PurePosixPath(item.name)
  assert not p.is_absolute() and '..' not in p.parts and (item.isfile() or item.isdir()),'Unsafe release member'
  assert item.name=='release.json' or item.name in m['files'] or (m['web'] and (item.name=='web/dist' or item.name.startswith('web/dist/'))),'Unexpected release member'
 archive.extractall(s)
PY
mkdir -p "$STAGING/validation/server-py"
cp -a server-py/app "$STAGING/validation/server-py/"
python3 - <<'PY'
import ctypes,hashlib,json,os,pathlib,shutil
s=pathlib.Path(os.environ['STAGING']);b=pathlib.Path(os.environ['BACKUP']);m=json.loads((s/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name);actual=hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
 assert actual==row['before'],f'Production source changed: {name}'
 assert hashlib.sha256((s/p).read_bytes()).hexdigest()==row['after']
 if p.is_file():
  target=b/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
 if p.suffix=='.py':
  target=s/'validation'/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(s/p,target)
shutil.copy2(s/'release.json',b/'release.json')
if m['web']:
 assert hasattr(ctypes.CDLL(None),'renameat2'),'Atomic web exchange unavailable'
 assert hashlib.sha256((s/'web/dist/index.html').read_bytes()).hexdigest()==m['index']
 shutil.copytree('web/dist',b/'web-dist')
PY
server-py/.venv/bin/python - <<'PY'
import json,os,pathlib,py_compile
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for name in m['files']:
 if name.endswith('.py'):py_compile.compile(str(s/name),doraise=True)
print('Staged Python source syntax verified')
PY
node --check "$STAGING/ecosystem.config.cjs"
node - "$STAGING/ecosystem.config.cjs" <<'JS'
const path=require('path');const config=require(path.resolve(process.argv[2]));
const market=config.apps.filter(p=>p.name==='pyradar-market');
if(market.length!==1 || market[0].args!=='-m app.market_worker')throw Error('Unexpected live-market service');
const marketApi=config.apps.filter(p=>p.name==='pyradar-market-api');
if(marketApi.length!==1 || !marketApi[0].args.includes('app.market_main:app') || !marketApi[0].args.includes('--port 8011'))throw Error('Unexpected independent market API');
if(!config.apps.some(p=>p.name==='pyradar') || !config.apps.some(p=>p.name==='pyradar-worker') || !config.apps.some(p=>p.name==='pyradar-projection'))throw Error('Existing services missing');
JS
# Recheck the baseline immediately before stopping any service.
python3 - <<'PY'
import hashlib,json,os,pathlib
m=json.loads((pathlib.Path(os.environ['STAGING'])/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name);actual=hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
 assert actual==row['before'],f'Concurrent deployment changed: {name}'
PY
sudo -n /www/server/nginx/sbin/nginx -t
CHANGED=1
for service in pyradar pyradar-worker pyradar-projection; do pm2 stop "$service" >/dev/null; done
if [ -n "$OLD_MARKET_PID" ] && [ "$OLD_MARKET_PID" != '0' ]; then pm2 stop pyradar-market >/dev/null; fi
if [ "$MARKET_API_EXISTED" -eq 1 ]; then pm2 stop pyradar-market-api >/dev/null; fi
NGINX_CHANGED=1
python3 - <<'PY'
import json,os,pathlib,shutil,tempfile
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for name in sorted(m['files'], key=lambda p: p=='nginx/dashboardv2.conf'):
 p=pathlib.Path(name);p.parent.mkdir(parents=True,exist_ok=True)
 with tempfile.NamedTemporaryFile(dir=p.parent,delete=False) as f:tmp=pathlib.Path(f.name)
 shutil.copy2(s/p,tmp);os.replace(tmp,p)
PY
pm2 start ecosystem.config.cjs --only pyradar-market --update-env >/dev/null
pm2 start ecosystem.config.cjs --only pyradar-market-api --update-env >/dev/null
pm2 restart ecosystem.config.cjs --only pyradar --update-env >/dev/null
pm2 restart ecosystem.config.cjs --only pyradar-projection --update-env >/dev/null
pm2 restart ecosystem.config.cjs --only pyradar-worker --update-env >/dev/null
MARKET_PID=$(pm2 pid pyradar-market)
export MARKET_PID
API_READY=0
for attempt in {1..60}; do
 if curl -fsS --max-time 5 http://127.0.0.1:8011/api/health/ready -o /dev/null &&
    curl -fsS --max-time 5 http://127.0.0.1:8011/api/live-market/status -o "$STAGING/live-market-status.json" &&
    server-py/.venv/bin/python - <<'PY'
import json,os,pathlib,time
health=json.loads(pathlib.Path('data/market-worker-health.json').read_text())
assert health.get('pid')==int(os.environ['MARKET_PID']),'Live-market health is from another process'
assert health.get('ready') is True,'Live-market catalogue is not ready'
at=health.get('updatedAt') or health.get('at')
assert isinstance(at,(int,float)) and abs(time.time()*1000-at)<60_000,'Live-market health stale'
assert health.get('status') not in ('error','stopped','failed'),'Live-market health failed'
status=json.loads((pathlib.Path(os.environ['STAGING'])/'live-market-status.json').read_text())
assert isinstance(status,dict),'Unexpected live-market status response'
assert status.get('liveMarket') is True and status.get('enabled') is True,'Live-market API is disabled'
assert {row.get('chainId') for row in status.get('chains',[])}=={'196','56','4663'},'Live-market chain status missing'
assert pathlib.Path('data/live-market.sqlite').is_file(),'Isolated database missing'
PY
 then API_READY=1; break; fi
 sleep 2
done
test "$API_READY" -eq 1
OLD_API_READY=0
for attempt in {1..16}; do
 if curl -fsS --max-time 15 http://127.0.0.1:8010/api/health/ready -o /dev/null; then OLD_API_READY=1; break; fi
 sleep 2
done
test "$OLD_API_READY" -eq 1
test "$(pm2 pid memedashboard)" = "$NODE_PID"
sudo -n /www/server/nginx/sbin/nginx -t
sudo -n /www/server/nginx/sbin/nginx -s reload
curl --compressed -fsS --max-time 10 https://cliperx.com/dashboard/api/live-market/status -o "$STAGING/public-live-market-status.json"
python3 - <<'PY'
import json,os,pathlib
s=pathlib.Path(os.environ['STAGING']);public=json.loads((s/'public-live-market-status.json').read_text());local=json.loads((s/'live-market-status.json').read_text())
assert public.get('liveMarket') is True and public.get('enabled') is True
assert {r.get('chainId') for r in public.get('chains',[])}=={'196','56','4663'}
assert {r['chainId']:r['epoch'] for r in public['chains']}=={r['chainId']:r['epoch'] for r in local['chains']},'Public route is not serving the independent market journal'
assert (s/'public-live-market-status.json').stat().st_size<100_000,'Public status unexpectedly large'
print('Dedicated market API and public narrow proxy verified')
PY
if python3 - <<'PY'
import json,os,pathlib,sys
sys.exit(0 if json.loads((pathlib.Path(os.environ['STAGING'])/'release.json').read_text())['web'] else 1)
PY
then
 if [ -d web/dist/assets ]; then cp -an web/dist/assets/. "$STAGING/web/dist/assets/"; fi
 python3 - <<'PY'
import ctypes,os
libc=ctypes.CDLL(None,use_errno=True)
if libc.renameat2(-100,b'web/dist',-100,os.fsencode(os.environ['STAGING']+'/web/dist'),2):raise OSError(ctypes.get_errno(),'Atomic dashboard activation failed')
PY
 WEB_MOVED=1
 curl --compressed -fsS --max-time 20 https://cliperx.com/dashboard/ -o "$STAGING/public-index.html"
 python3 - <<'PY'
import hashlib,json,os,pathlib
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
assert hashlib.sha256((s/'public-index.html').read_bytes()).hexdigest()==m['index'],'Public dashboard mismatch'
PY
fi
python3 - <<'PY'
import hashlib,json,os,pathlib,sqlite3
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for name,row in m['files'].items():assert hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()==row['after'],f'Deployed hash mismatch: {name}'
if m['web']:assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['index']
con=sqlite3.connect('file:data/live-market.sqlite?mode=ro',uri=True,timeout=.5)
assert con.execute('PRAGMA quick_check(1)').fetchone()[0]=='ok','Isolated database integrity check failed'
con.close()
print('Isolated market source, worker identity, readiness and database verified')
PY
pm2 save >/dev/null
COMPLETE=1
printf 'Isolated market release live; databases retained; rollback: %s\n' "$BACKUP"
REMOTE
