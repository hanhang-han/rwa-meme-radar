#!/usr/bin/env bash
# Release only changed application code plus the built dashboard; keep market data intact.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
BASELINE="${1:?Pass the read-only server baseline JSON}"
FILES="${2:?Pass the reviewed source file whitelist JSON}"
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
CONTROL_DIR=$(mktemp -d /tmp/cliperx-product-ssh.XXXXXX)
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=45 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o ControlMaster=auto -o ControlPersist=180 -o ControlPath="$CONTROL_DIR/socket" -o IdentitiesOnly=yes -i "${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}")
PACKAGE=$(mktemp /tmp/cliperx-product.XXXXXX.tar.gz)
MANIFEST=$(mktemp /tmp/cliperx-product.XXXXXX.json)
cleanup() {
  ssh "${SSH_OPTS[@]}" -O exit "$SERVER" >/dev/null 2>&1 || true
  rm -f "$PACKAGE" "$MANIFEST"
  rmdir "$CONTROL_DIR" 2>/dev/null || true
}
trap cleanup EXIT
python3 - "$BASELINE" "$MANIFEST" "$PACKAGE" "$FILES" <<'PY'
import hashlib,json,pathlib,subprocess,sys,tarfile
baseline=json.load(open(sys.argv[1]))
paths=json.load(open(sys.argv[4]))
assert isinstance(paths,list) and paths and len(paths)==len(set(paths))
for p in paths:
 path=pathlib.Path(p)
 assert not path.is_absolute() and '..' not in path.parts
 assert p.startswith(('server-py/app/','web/src/')) and path.is_file()
 assert path.suffix in ('.py','.js','.vue','.css'), f'Unexpected release file: {p}'
paths=sorted(paths)
assert pathlib.Path('web/dist/index.html').is_file()
assert '/dashboard/assets/' in pathlib.Path('web/dist/index.html').read_text(), 'Build with VITE_BASE=/dashboard/ before release'
manifest={'files':{p:{'before':baseline.get(p),'after':hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()} for p in paths},'index':hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()}
pathlib.Path(sys.argv[2]).write_text(json.dumps(manifest))
with tarfile.open(sys.argv[3],'w:gz') as out:
 for p in paths: out.add(p,arcname=p)
 out.add('web/dist',arcname='web/dist')
 out.add(sys.argv[2],arcname='release.json')
print('Release manifest:',len(paths),'source files')
PY
for attempt in {1..3}; do
  if ssh "${SSH_OPTS[@]}" "$SERVER" true; then break; fi
  if [ "$attempt" -eq 3 ]; then false; fi
done
ssh "${SSH_OPTS[@]}" "$SERVER" 'cat > /tmp/cliperx-product-release.tar.gz' < "$PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
mkdir -p .releases
STAGING=$(mktemp -d .releases/product-staging.XXXXXX)
BACKUP=".releases/product-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
export STAGING BACKUP
# Stage a complete server package, then overlay only the reviewed changes.
# This keeps unchanged dependencies available while importing the new modules.
mkdir -p "$STAGING/server-py"
cp -a server-py/app "$STAGING/server-py/app"
# The registry loads its deployed ABI relative to the repository root.
mkdir -p "$STAGING/contracts"
cp -a contracts/artifacts "$STAGING/contracts/artifacts"
mkdir -p "$STAGING/src"
cp -a src/catalogues "$STAGING/src/catalogues"
tar xzf /tmp/cliperx-product-release.tar.gz -C "$STAGING"
server-py/.venv/bin/python - <<'PY'
import hashlib,json,os,pathlib,py_compile,shutil
stage=pathlib.Path(os.environ['STAGING']);backup=pathlib.Path(os.environ['BACKUP'])
m=json.loads((stage/'release.json').read_text())
for name,hashes in m['files'].items():
 path=pathlib.Path(name)
 assert not path.is_absolute() and '..' not in path.parts
 current=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
 assert current==hashes['before'],f'Server file changed since audit: {name}'
 assert hashlib.sha256((stage/path).read_bytes()).hexdigest()==hashes['after']
 if name.endswith('.py'):py_compile.compile(str(stage/path),doraise=True)
 if path.exists():(backup/path).parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,backup/path)
shutil.copy2(stage/'release.json',backup/'release.json')
if pathlib.Path('.env').exists():shutil.copy2('.env',backup/'.env');os.chmod(backup/'.env',0o600)
print('Server baseline verified; rollback prepared')
PY
# Import staged modules before stopping any running process.
PYTHONPATH="$STAGING/server-py" server-py/.venv/bin/python - <<'PY'
from app.main import app
from app import product_quotes, worker
paths=app.openapi()['paths']
assert '/api/v2/product/capabilities' in paths
assert '/api/v2/stocks/{ticker}' in paths
print('Staged API imports and routes verified')
PY
CHANGED=0
WEB_MOVED=0
COMPLETE=0
finish() {
  result=$?
  trap - EXIT
  if [ "$CHANGED" -eq 1 ] && [ "$COMPLETE" -eq 0 ]; then
    rollback_failed=0
    pm2 stop pyradar-worker pyradar-projection pyradar >/dev/null 2>&1 || true
    if ! python3 - <<'PY'
import json,os,pathlib,shutil
backup=pathlib.Path(os.environ['BACKUP']);m=json.loads((backup/'release.json').read_text())
for name,row in m['files'].items():
 path=pathlib.Path(name)
 if row['before'] is None:path.unlink(missing_ok=True)
 else:shutil.copy2(backup/path,path)
if (backup/'.env').exists():shutil.copy2(backup/'.env','.env')
if (backup/'web-dist').exists():
 if pathlib.Path('web/dist').exists():shutil.move('web/dist',backup/'failed-web-dist')
 shutil.move(backup/'web-dist','web/dist')
PY
    then rollback_failed=1; fi
    pm2 restart pyradar >/dev/null 2>&1 || rollback_failed=1
    api_restored=0
    for attempt in {1..120}; do
      if curl -fsS --max-time 3 http://127.0.0.1:8010/api/health/ready -o /dev/null 2>/dev/null; then api_restored=1; break; fi
      sleep 3
    done
    if [ "$api_restored" -eq 0 ]; then rollback_failed=1; fi
    pm2 restart pyradar-projection >/dev/null 2>&1 || rollback_failed=1
    pm2 restart pyradar-worker >/dev/null 2>&1 || rollback_failed=1
    background_restored=0
    for attempt in {1..20}; do
      if python3 - <<'PY'
import json,os,pathlib,subprocess,time
rows={r['name']:r for r in json.loads(subprocess.check_output(['pm2','jlist']))}
for name in ('pyradar','pyradar-worker','pyradar-projection'):
 assert rows[name]['pid']>0 and rows[name]['pm2_env']['status']=='online'
for name,path in (('pyradar-worker','data/worker-health.json'),('pyradar-projection','data/projection-health.json')):
 record=json.loads(pathlib.Path(path).read_text());os.kill(record['pid'],0)
 assert record['pid']==rows[name]['pid'] and record.get('tasks') and -5000<=int(time.time()*1000)-record['updatedAt']<180000
PY
      then background_restored=1; break; fi
      sleep 3
    done
    if [ "$background_restored" -eq 0 ]; then rollback_failed=1; fi
    if [ "$rollback_failed" -eq 0 ]; then
      printf 'Release rolled back and processes verified: %s\n' "$BACKUP" >&2
    else
      printf 'Rollback needs operator recovery; saved source and dashboard: %s\n' "$BACKUP" >&2
    fi
  fi
  exit "$result"
}
trap finish EXIT
CHANGED=1
pm2 stop pyradar-worker pyradar-projection pyradar >/dev/null
ACTIVATED_AT=$(python3 -c 'import time;print(int(time.time()*1000))')
export ACTIVATED_AT
server-py/.venv/bin/python - <<'PY'
import json,os,pathlib,shutil
stage=pathlib.Path(os.environ['STAGING']);m=json.loads((stage/'release.json').read_text())
for name in m['files']:
 path=pathlib.Path(name);path.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(stage/path,path)
# Operator IDs are configured separately; application releases do not grant access.
PY
# One process initializes/replays the WAL before other writers start. A large
# crash-recovery log can take longer than the normal 15-second writer budget.
pm2 restart pyradar >/dev/null
for attempt in {1..120}; do
  if curl -fsS --max-time 3 http://127.0.0.1:8010/api/health/ready -o /dev/null 2>/dev/null; then break; fi
  if [ "$attempt" -eq 120 ]; then false; fi
  sleep 3
done
pm2 restart pyradar-projection >/dev/null
pm2 restart pyradar-worker >/dev/null
for attempt in {1..24}; do
  if curl -fsS --max-time 5 http://127.0.0.1:8010/api/health/ready -o /dev/null &&
     curl --compressed -fsS --max-time 8 'http://127.0.0.1:8010/api/dashboard?view=overview' -o "$STAGING/overview.json" &&
     PYTHONPATH=server-py server-py/.venv/bin/python - "$STAGING/overview.json" <<'PY'
import json,os,pathlib,subprocess,sys,time
from app.main import process_health
p=pathlib.Path(sys.argv[1]);d=json.loads(p.read_text());u=d['unified']
assert p.stat().st_size<=50000 and u['snapshotScope']=='overview'
activated=int(os.environ['ACTIVATED_AT']);now=int(time.time()*1000)
assert 'newAssets24h' in u['metrics'] and 'hotStocks' in u and 'stockThemes' in u
assert d['now']>=activated,'Waiting for a newly built product snapshot'
processes={r['name']:r for r in json.loads(subprocess.check_output(['pm2','jlist']))}
for name in ('pyradar','pyradar-worker','pyradar-projection'):
 row=processes[name]
 assert row['pid']>0 and row['pm2_env']['status']=='online' and row['pm2_env']['pm_uptime']>=activated,f'{name} is not the new live process'
for name,path,expected in (
 ('pyradar-worker','data/worker-health.json',('dexBatchMarket','productSparklines','poolVolumeSnapshots','arcPoller','tradeConfirmations','stockPairMinute')),
 ('pyradar-projection','data/projection-health.json',('realtimeProjection',))):
 health=process_health(path,now)
 assert health['ok'] and health['pid']==processes[name]['pid'] and health['updatedAt']>=activated,f'Waiting for {name} heartbeat'
 assert all(task in health['tasks'] for task in expected),f'{name} did not initialize product tasks'
 assert all(health['tasks'][task]['status'] not in ('stopped',) for task in expected)
 if name=='pyradar-projection':assert health['tasks']['realtimeProjection'].get('lastSuccessAt',0)>=activated,'Waiting for product projection success'
safe={'at':now,'snapshotAt':d['now'],'overviewBytes':p.stat().st_size,
      'processes':[{k:row[k] for k in ('name','pid')} for row in processes.values() if row['name'] in ('pyradar','pyradar-worker','pyradar-projection')]}
(pathlib.Path(os.environ['STAGING'])/'release-health.json').write_text(json.dumps(safe))
print('New process heartbeats and projection verified; overview bytes:',p.stat().st_size)
PY
  then break; fi
  if [ "$attempt" -eq 24 ]; then false; fi
  sleep 3
done
curl --compressed -fsS --max-time 30 'http://127.0.0.1:8010/api/v2/stocks/700?chain=all' -o "$STAGING/theme.json"
curl --compressed -fsS --max-time 15 'http://127.0.0.1:8010/api/v2/product/capabilities' -o "$STAGING/capabilities.json"
python3 - <<'PY'
import json,os,pathlib
stage=pathlib.Path(os.environ['STAGING'])
theme=json.loads((stage/'theme.json').read_text())
assert theme['unified']['snapshotScope']=='theme' and 'themeMetrics' in theme
cap=json.loads((stage/'capabilities.json').read_text())
assert cap['naturalFilters']['available'] and cap['assetFacts']['modelEnabled'] is False
print('Versioned theme and filter capabilities verified')
PY
# Backend accepts the new view before exposing the new JavaScript.
if [ -d web/dist/assets ]; then cp -an web/dist/assets/. "$STAGING/web/dist/assets/"; fi
mv web/dist "$BACKUP/web-dist"
WEB_MOVED=1
mv "$STAGING/web/dist" web/dist
curl --compressed -fsS --max-time 20 https://cliperx.com/dashboard/ -o "$STAGING/public-index.html"
python3 - <<'PY'
import hashlib,json,os,pathlib
stage=pathlib.Path(os.environ['STAGING']);m=json.loads((stage/'release.json').read_text())
assert hashlib.sha256((stage/'public-index.html').read_bytes()).hexdigest()==m['index']
PY
curl -fsS --max-time 10 https://cliperx.com/dashboard/api/health/ready -o /dev/null
COMPLETE=1
cp "$STAGING/release-health.json" "$BACKUP/release-health.json"
pm2 save >/dev/null
printf 'Product iteration deployed; rollback: %s\n' "$BACKUP"
REMOTE
