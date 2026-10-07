#!/usr/bin/env bash
# One collector module only; restart only the independent live-market worker.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
BASELINE="${1:?Pass the current reviewed collector source and six-service baseline JSON}"
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o IdentitiesOnly=yes -i "${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}")
PACKAGE=$(mktemp /tmp/cliperx-market-collector-hotfix.XXXXXX.tar.gz)
MANIFEST=$(mktemp /tmp/cliperx-market-collector-hotfix.XXXXXX.json)
trap 'rm -f "$PACKAGE" "$MANIFEST"' EXIT
python3 - "$BASELINE" "$MANIFEST" "$PACKAGE" <<'PY'
import hashlib,json,pathlib,sys,tarfile
name='server-py/app/collectors/live_market.py';p=pathlib.Path(name)
baseline=json.loads(pathlib.Path(sys.argv[1]).read_text());before=baseline['sha256'][name]
assert isinstance(before,str) and len(before)==64 and all(c in '0123456789abcdef' for c in before)
names={'memedashboard','pyradar','pyradar-worker','pyradar-projection','pyradar-market','pyradar-market-api'}
assert set(baseline['services'])==names,'Six-service baseline incomplete'
assert all(isinstance(r['pid'],int) and r['pid']>0 and r['status']=='online' for r in baseline['services'].values()),'Baseline service not online'
assert p.is_file() and not p.is_symlink(),'Collector source missing or symlinked'
after=hashlib.sha256(p.read_bytes()).hexdigest();assert before!=after,'Collector source unchanged'
compile(p.read_bytes(),str(p),'exec')
manifest={'path':name,'before':before,'after':after,'service':'pyradar-market','services':baseline['services']}
pathlib.Path(sys.argv[2]).write_text(json.dumps(manifest))
with tarfile.open(sys.argv[3],'w:gz') as out:
 out.add(p,arcname=name,recursive=False);out.add(sys.argv[2],arcname='release.json')
print('Reviewed one-file live-market collector hotfix prepared')
PY
REMOTE_PACKAGE="/tmp/$(basename "$PACKAGE")"
ssh "${SSH_OPTS[@]}" "$SERVER" "cat > $REMOTE_PACKAGE" < "$PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$REMOTE_PACKAGE" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
exec 9>.releases/live-market-collector-hotfix.lock
flock -n 9 || { printf 'Another market collector hotfix is running\n' >&2; exit 1; }
STAGING=$(mktemp -d .releases/live-market-collector-hotfix-staging.XXXXXX)
BACKUP=".releases/live-market-collector-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
export STAGING BACKUP
CHANGED=0
COMPLETE=0
REMOTE_PACKAGE="$1"
finish() {
 result=$?
 trap - EXIT
 if [ "$CHANGED" -eq 1 ] && [ "$COMPLETE" -ne 1 ]; then
  set +e
  ROLLBACK_OK=1
  pm2 stop pyradar-market >/dev/null
  if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  python3 - <<'PY'
import hashlib,json,os,pathlib,shutil,tempfile
b=pathlib.Path(os.environ['BACKUP']);m=json.loads((b/'release.json').read_text());p=pathlib.Path(m['path'])
with tempfile.NamedTemporaryFile(dir=p.parent,delete=False) as f:tmp=pathlib.Path(f.name)
shutil.copy2(b/p,tmp);os.replace(tmp,p)
assert hashlib.sha256(p.read_bytes()).hexdigest()==m['before']
PY
  if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  RESTART_AT_MS=$(python3 -c 'import time;print(int(time.time()*1000))')
  pm2 restart pyradar-market >/dev/null
  if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  MARKET_PID=$(pm2 pid pyradar-market)
  export MARKET_PID RESTART_AT_MS
  READY=0
  for attempt in {1..60}; do
   if python3 "$STAGING/verify-worker.py" "$STAGING/rollback-health.json"; then READY=1; break; fi
   sleep 2
  done
  if [ "$READY" -ne 1 ]; then ROLLBACK_OK=0; fi
  if ! curl -fsS --max-time 5 http://127.0.0.1:8011/api/health/ready -o /dev/null ||
     ! curl -fsS --max-time 5 http://127.0.0.1:8011/api/live-market/status -o "$STAGING/after-status.json" ||
     ! curl --compressed -fsS --max-time 10 https://cliperx.com/dashboard/api/live-market/status -o "$STAGING/public-status.json" ||
     ! python3 "$STAGING/verify-isolation.py"; then ROLLBACK_OK=0; fi
  if [ "$ROLLBACK_OK" -eq 1 ]; then printf 'Collector hotfix failed; prior module/worker restored. Backup: %s\n' "$BACKUP" >&2;
  else printf 'Collector hotfix failed; rollback needs inspection: %s\n' "$BACKUP" >&2; fi
 fi
 rm -f "$REMOTE_PACKAGE" 2>/dev/null || true
 exit "$result"
}
trap finish EXIT
python3 - "$REMOTE_PACKAGE" <<'PY'
import hashlib,json,os,pathlib,shutil,subprocess,sys,tarfile
s=pathlib.Path(os.environ['STAGING']);b=pathlib.Path(os.environ['BACKUP'])
with tarfile.open(sys.argv[1]) as archive:
 m=json.load(archive.extractfile('release.json'))
 assert m['path']=='server-py/app/collectors/live_market.py' and m['service']=='pyradar-market'
 assert {item.name for item in archive.getmembers()}=={m['path'],'release.json'}
 assert all(item.isfile() for item in archive.getmembers())
 archive.extractall(s)
p=pathlib.Path(m['path']);assert p.is_file() and not p.is_symlink()
assert hashlib.sha256(p.read_bytes()).hexdigest()==m['before'],'Production collector source changed'
assert hashlib.sha256((s/p).read_bytes()).hexdigest()==m['after']
names={'memedashboard','pyradar','pyradar-worker','pyradar-projection','pyradar-market','pyradar-market-api'}
assert set(m['services'])==names
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True))
actual={r['name']:{'pid':r['pid'],'status':r['pm2_env']['status']} for r in rows if r.get('name') in names}
assert actual==m['services'],'Six-service baseline changed; refresh before deployment'
target=b/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target);shutil.copy2(s/'release.json',b/'release.json')
(b/'services-before.json').write_text(json.dumps(actual))
print('Collector source and six online service identities verified')
PY
server-py/.venv/bin/python -m py_compile "$STAGING/server-py/app/collectors/live_market.py"
cat > "$STAGING/verify-worker.py" <<'PY'
import json,os,pathlib,sys,time
try:
 health=json.loads(pathlib.Path('data/market-worker-health.json').read_text())
 assert health.get('pid')==int(os.environ['MARKET_PID']),'Worker health belongs to another process'
 assert health.get('mode')=='isolated-native-markets' and health.get('ready') is True,'Worker catalogue not ready'
 at=health.get('updatedAt')
 assert isinstance(at,(int,float)) and at>=int(os.environ['RESTART_AT_MS']) and abs(time.time()*1000-at)<60_000,'Worker health predates restart or is stale'
 assert set(health.get('chains',{}))=={'196','56','4663'},'Worker health chain coverage missing'
 assert all(row.get('status') not in ('error','stopped','failed') for row in health['chains'].values()),'A worker chain failed'
 pathlib.Path(sys.argv[1]).write_text(json.dumps(health))
except (OSError,ValueError,TypeError,AssertionError) as error:
 print('Awaiting fresh worker readiness: '+str(error),file=sys.stderr);sys.exit(1)
PY
cat > "$STAGING/verify-isolation.py" <<'PY'
import json,os,pathlib,subprocess
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True))
actual={r['name']:{'pid':r['pid'],'status':r['pm2_env']['status']} for r in rows if r.get('name') in m['services']}
assert set(actual)==set(m['services'])
assert all(r['pid']>0 and r['status']=='online' for r in actual.values()),'A service is no longer online'
assert all(actual[n]==r for n,r in m['services'].items() if n!='pyradar-market'),'Unrelated service identity changed'
assert actual['pyradar-market']['pid']==int(os.environ['MARKET_PID']) and actual['pyradar-market']['pid']!=m['services']['pyradar-market']['pid'],'Worker did not restart or restarted again'
def identity(name):
 body=json.loads((s/name).read_text());assert body.get('enabled') is True and body.get('liveMarket') is True
 assert {r.get('chainId') for r in body.get('chains',[])}=={'196','56','4663'}
 result={r['chainId']:r['epoch'] for r in body['chains']};assert all(isinstance(v,str) and v for v in result.values());return result
assert identity('before-status.json')==identity('after-status.json')==identity('public-status.json'),'Journal epoch changed or public API is not isolated'
(s/'verification.json').write_text(json.dumps({'servicesAfter':actual,'epochs':identity('after-status.json')}))
print('Fresh worker PID/readiness, five unchanged services and public/local journal epochs verified')
PY
curl -fsS --max-time 5 http://127.0.0.1:8011/api/health/ready -o /dev/null
curl -fsS --max-time 5 http://127.0.0.1:8011/api/live-market/status -o "$STAGING/before-status.json"
python3 - <<'PY'
import hashlib,json,os,pathlib,subprocess
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
assert hashlib.sha256(pathlib.Path(m['path']).read_bytes()).hexdigest()==m['before']
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True));actual={r['name']:{'pid':r['pid'],'status':r['pm2_env']['status']} for r in rows if r.get('name') in m['services']}
assert actual==m['services'],'Service identities changed immediately before collector stop'
PY
CHANGED=1
pm2 stop pyradar-market >/dev/null
python3 - <<'PY'
import json,os,pathlib,shutil,tempfile
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text());p=pathlib.Path(m['path'])
with tempfile.NamedTemporaryFile(dir=p.parent,delete=False) as f:tmp=pathlib.Path(f.name)
shutil.copy2(s/p,tmp);os.replace(tmp,p)
PY
RESTART_AT_MS=$(python3 -c 'import time;print(int(time.time()*1000))')
pm2 restart pyradar-market >/dev/null
MARKET_PID=$(pm2 pid pyradar-market)
test "${MARKET_PID:-0}" -gt 0
export MARKET_PID RESTART_AT_MS
READY=0
for attempt in {1..60}; do
 if python3 "$STAGING/verify-worker.py" "$STAGING/worker-health.json"; then READY=1; break; fi
 sleep 2
done
test "$READY" -eq 1
curl -fsS --max-time 5 http://127.0.0.1:8011/api/health/ready -o /dev/null
curl -fsS --max-time 5 http://127.0.0.1:8011/api/live-market/status -o "$STAGING/after-status.json"
curl --compressed -fsS --max-time 10 https://cliperx.com/dashboard/api/live-market/status -o "$STAGING/public-status.json"
python3 "$STAGING/verify-worker.py" "$STAGING/worker-health.json"
python3 "$STAGING/verify-isolation.py"
python3 - <<'PY'
import hashlib,json,os,pathlib,shutil
s=pathlib.Path(os.environ['STAGING']);b=pathlib.Path(os.environ['BACKUP']);m=json.loads((s/'release.json').read_text())
assert hashlib.sha256(pathlib.Path(m['path']).read_bytes()).hexdigest()==m['after'],'Published collector hash mismatch'
for name in ('before-status.json','after-status.json','public-status.json','worker-health.json','verification.json'):shutil.copy2(s/name,b/name)
print('One collector module SHA and release evidence verified')
PY
COMPLETE=1
printf 'Market collector hotfix live; only pyradar-market restarted; rollback: %s\n' "$BACKUP"
REMOTE
