#!/usr/bin/env bash
# Publish only the independent batch logo endpoint. Collector/projection/static services are preserved.
set -Eeuo pipefail
EVIDENCE="${1:?Pass absolute logo release evidence directory}"
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o IdentitiesOnly=yes -i "${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}")
PACKAGE="$EVIDENCE/backend-release.tar.gz"
test -s "$PACKAGE"
REMOTE_PACKAGE="/tmp/cliperx-asset-logos-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
ssh "${SSH_OPTS[@]}" "$SERVER" "cat > $REMOTE_PACKAGE" < "$PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$REMOTE_PACKAGE" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
exec 9>.releases/asset-logos-deploy.lock
flock -n 9
STAGING=$(mktemp -d /opt/memedashboard/.releases/asset-logos-staging.XXXXXX)
BACKUP=".releases/asset-logos-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
export STAGING BACKUP
CHANGED=0
STOPPED=0
SUCCESS=0
finish() {
 result=$?
 trap - EXIT
 if [ "$SUCCESS" -ne 1 ] && [ "$STOPPED" -eq 1 ]; then
  set +e
  RESTORED=1
  pm2 stop pyradar >/dev/null 2>&1
  if [ "$CHANGED" -eq 1 ]; then
   python3 - <<'PY'
import hashlib,json,os,pathlib,shutil
b=pathlib.Path(os.environ['BACKUP']);m=json.loads((b/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name)
 if row['before'] is None:p.unlink(missing_ok=True)
 else:
  shutil.copy2(b/p,p)
  assert hashlib.sha256(p.read_bytes()).hexdigest()==row['before']
PY
   if [ "$?" -ne 0 ]; then RESTORED=0; fi
  fi
  pm2 restart pyradar >/dev/null 2>&1
  if [ "$?" -ne 0 ]; then RESTORED=0; fi
  READY=0
  for attempt in {1..20}; do
   if curl -fsS --max-time 3 http://127.0.0.1:8010/api/health/ready -o /dev/null; then READY=1;break;fi
   sleep 2
  done
  if [ "$READY" -ne 1 ]; then RESTORED=0; fi
  if [ "$RESTORED" -eq 1 ]; then
   printf 'Logo release failed; prior source restored and API ready. Backup: %s\n' "$BACKUP" >&2
  else
   printf 'Logo release failed; rollback needs inspection: %s\n' "$BACKUP" >&2
  fi
 fi
 rm -f "$1" 2>/dev/null || true
 exit "$result"
}
REMOTE_PACKAGE="$1"
trap 'finish "$REMOTE_PACKAGE"' EXIT
python3 - "$REMOTE_PACKAGE" <<'PY'
import hashlib,json,os,pathlib,subprocess,tarfile,shutil
s=pathlib.Path(os.environ['STAGING']);b=pathlib.Path(os.environ['BACKUP'])
with tarfile.open(__import__('sys').argv[1]) as t:
 for item in t.getmembers():
  p=pathlib.PurePosixPath(item.name)
  assert not p.is_absolute() and '..' not in p.parts and item.isfile()
  assert item.name in {'release.json','run-logo-tests.py','probe-logos.py'} or item.name.startswith(('server-py/app/','server-py/tests/'))
 t.extractall(s)
m=json.loads((s/'release.json').read_text())
expected={'server-py/app/asset_logo_metadata.py','server-py/app/api/product_v2.py'}
assert set(m['files'])==expected
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore']
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True))
states={r['name']:{'pid':r['pid'],'status':r['pm2_env']['status']} for r in rows if r['name'] in m['services']}
assert states==m['services'],'Production services changed before release'
for name,row in m['files'].items():
 p=pathlib.Path(name)
 assert not p.is_symlink(),name
 assert (hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None)==row['before'],name
 assert hashlib.sha256((s/p).read_bytes()).hexdigest()==row['after'],name
 if p.exists():
  target=b/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
shutil.copy2(s/'release.json',b/'release.json')
v=s/'validation';(v/'server-py').mkdir(parents=True)
shutil.copytree('server-py/app',v/'server-py/app',ignore=shutil.ignore_patterns('__pycache__'))
shutil.copytree(s/'server-py/tests',v/'server-py/tests')
for name in m['files']:shutil.copy2(s/name,v/name)
for name in ['src/catalogues','contracts/artifacts']:
 target=v/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(name,target)
(v/'scripts').mkdir();shutil.copy2('scripts/deploy.sh',v/'scripts/deploy.sh')
shutil.copy2(s/'run-logo-tests.py',v/'run-logo-tests.py')
print('Production code, six services and unchanged frontend verified',flush=True)
PY
(
 cd "$STAGING/validation"
 NODE_ENV=test PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$STAGING/validation/server-py" /opt/memedashboard/server-py/.venv/bin/python run-logo-tests.py
)
python3 - <<'PY'
import hashlib,json,os,pathlib,subprocess
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name);assert (hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None)==row['before'],name
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True))
assert {r['name']:{'pid':r['pid'],'status':r['pm2_env']['status']} for r in rows if r['name'] in m['services']}==m['services']
PY
STOPPED=1
pm2 stop pyradar >/dev/null
CHANGED=1
python3 - <<'PY'
import json,os,pathlib,shutil
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for name in m['files']:
 p=pathlib.Path(name);tmp=p.with_name(p.name+'.logo-new');shutil.copy2(s/p,tmp);os.replace(tmp,p)
PY
pm2 restart pyradar >/dev/null
READY=0
for attempt in {1..20}; do
 if curl -fsS --max-time 3 http://127.0.0.1:8010/api/health/ready -o /dev/null; then READY=1;break;fi
 sleep 2
done
test "$READY" -eq 1
server-py/.venv/bin/python "$STAGING/probe-logos.py" > "$BACKUP/probe-after.json"
cat "$BACKUP/probe-after.json"
python3 - <<'PY'
import hashlib,json,os,pathlib,subprocess
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for name,row in m['files'].items():assert hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()==row['after'],name
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore']
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True))
states={r['name']:{'pid':r['pid'],'status':r['pm2_env']['status']} for r in rows if r['name'] in m['services']}
assert all(r['status']=='online' and r['pid'] for r in states.values())
for name in set(m['services'])-{'pyradar'}:assert states[name]==m['services'][name],name
print(json.dumps({'services':states,'backup':os.environ['BACKUP'],'frontendUnchanged':True}))
PY
SUCCESS=1
printf 'Real asset logo API deployed; five other services preserved; rollback: %s\n' "$BACKUP"
REMOTE
