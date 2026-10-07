#!/usr/bin/env bash
# Publish only reviewed backend files with source checks and code rollback.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
EVIDENCE="${1:?Pass the release evidence directory}"
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o IdentitiesOnly=yes -i "${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}")
PACKAGE=$(mktemp /tmp/cliperx-server-optimization.XXXXXX.tar.gz)
trap 'rm -f "$PACKAGE"' EXIT
python3 - "$EVIDENCE" "$PACKAGE" <<'PY'
import hashlib,json,pathlib,sys,tarfile
e=pathlib.Path(sys.argv[1]);before=json.loads((e/'before-code-api.json').read_text())['sourceSha256']
original=e/'before-source/server-py/app/main.py'
assert original.is_file() and original.stat().st_size>0
before['server-py/app/main.py']=hashlib.sha256(original.read_bytes()).hexdigest()
original=e/'before-source/server-py/app/collectors/main_round.py'
assert original.is_file() and original.stat().st_size>0
before['server-py/app/collectors/main_round.py']=hashlib.sha256(original.read_bytes()).hexdigest()
changes={}
for name,digest in before.items():
 override=e/'candidate-source'/name
 p=override if override.is_file() else pathlib.Path(name)
 assert p.is_file() and name.startswith('server-py/app/') and p.suffix=='.py'
 actual=hashlib.sha256(p.read_bytes()).hexdigest()
 if digest!=actual:changes[name]={'before':digest,'after':actual}
assert changes
tests=sorted(str(p) for p in pathlib.Path('server-py/tests').glob('test_*.py'))
resource_only=set(changes)<= {'server-py/app/resource_budget.py','server-py/app/collectors/scheduler.py','server-py/app/projection_worker.py'}
services=['pyradar-worker','pyradar-projection'] if resource_only else ['pyradar','pyradar-worker','pyradar-projection']
all_services=['memedashboard','pyradar','pyradar-worker','pyradar-projection','pyradar-market','pyradar-market-api']
manifest={'files':changes,'services':services,
          'preserveServices':[name for name in all_services if name not in services]}
(e/'release.json').write_text(json.dumps(manifest,indent=2))
with tarfile.open(sys.argv[2],'w:gz') as out:
 for name in changes:
  override=e/'candidate-source'/name
  out.add(override if override.is_file() else pathlib.Path(name),arcname=name)
 for name in tests:out.add(name,arcname=name)
 out.add(e/'release.json',arcname='release.json')
print('Reviewed backend files:',len(changes))
PY
REMOTE_PACKAGE="/tmp/$(basename "$PACKAGE")"
ssh "${SSH_OPTS[@]}" "$SERVER" "cat > $REMOTE_PACKAGE" < "$PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$REMOTE_PACKAGE" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
mkdir -p .releases
exec 9>.releases/server-optimization-deploy.lock
flock -n 9 || { printf 'Another optimization release is running\n' >&2; exit 1; }
STAGING=$(mktemp -d .releases/server-optimization-staging.XXXXXX)
BACKUP=".releases/server-optimization-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
export STAGING BACKUP
CHANGED=0
SERVICES_STOPPED=0
SUCCESS=0
RESTART_SERVICES=(pyradar pyradar-worker pyradar-projection)
finish() {
 result=$?
 trap - EXIT
 if [ "$SUCCESS" -ne 1 ] && [ "$SERVICES_STOPPED" -eq 1 ]; then
  set +e
  pm2 stop "${RESTART_SERVICES[@]}" >/dev/null 2>&1
  RESTORED=1
  if [ "$CHANGED" -eq 1 ]; then
   python3 - <<'PY'
import hashlib,json,os,pathlib,shutil
b=pathlib.Path(os.environ['BACKUP']);m=json.loads((b/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name)
 if row['before'] is None:p.unlink(missing_ok=True)
 else:shutil.copy2(b/p,p)
 digest=hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
 assert digest==row['before'],name
PY
   if [ "$?" -ne 0 ]; then RESTORED=0; fi
  fi
  pm2 restart "${RESTART_SERVICES[@]}" >/dev/null 2>&1
  if [ "$?" -ne 0 ]; then RESTORED=0; fi
  READY=0
  for attempt in {1..30}; do
   if curl -fsS --max-time 3 http://127.0.0.1:8010/api/health/ready -o /dev/null; then READY=1; break; fi
   sleep 2
  done
  if [ "$READY" -ne 1 ]; then RESTORED=0; fi
  if [ "$RESTORED" -eq 1 ]; then
   printf 'Release failed; previous source and services restored. Backup: %s\n' "$BACKUP" >&2
  else
   printf 'Release failed; rollback needs inspection. Backup: %s\n' "$BACKUP" >&2
  fi
 fi
 exit "$result"
}
trap finish EXIT
tar xzf "$1" -C "$STAGING"
mapfile -t RESTART_SERVICES < <(python3 - <<'PY'
import json,os,pathlib
m=json.loads((pathlib.Path(os.environ['STAGING'])/'release.json').read_text())
assert set(m['services'])<= {'pyradar','pyradar-worker','pyradar-projection'} and m['services']
print('\n'.join(m['services']))
PY
)
test "${#RESTART_SERVICES[@]}" -ge 1
mkdir -p "$STAGING/validation/server-py"
cp -a server-py/app "$STAGING/validation/server-py/"
cp -a "$STAGING/server-py/tests" "$STAGING/validation/server-py/"
mkdir -p "$STAGING/validation/src" "$STAGING/validation/contracts" "$STAGING/validation/scripts"
cp -a src/catalogues "$STAGING/validation/src/"
cp -a contracts/artifacts "$STAGING/validation/contracts/"
cp -a scripts/deploy.sh "$STAGING/validation/scripts/"
python3 - <<'PY'
import hashlib,json,os,pathlib,shutil,subprocess
s=pathlib.Path(os.environ['STAGING']);b=pathlib.Path(os.environ['BACKUP']);m=json.loads((s/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name)
 assert not p.is_symlink(),name
 digest=hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
 assert digest==row['before'],f'Production changed: {name}'
 assert hashlib.sha256((s/p).read_bytes()).hexdigest()==row['after'],name
 if p.exists():
  target=b/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
 target=s/'validation'/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(s/p,target)
shutil.copy2(s/'release.json',b/'release.json')
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True))
pids={row['name']:row['pid'] for row in rows if row['name'] in m['preserveServices']}
assert len(pids)==len(m['preserveServices']) and all(pids.values())
(b/'preserved-pids.json').write_text(json.dumps(pids))
PY
PYTHONDONTWRITEBYTECODE=1 server-py/.venv/bin/python -m compileall -q "$STAGING/validation/server-py/app"
# All focused candidate regressions use isolated temporary data or mocks.
NODE_ENV=test PYTHONPATH="$STAGING/validation/server-py" server-py/.venv/bin/python - <<'PY'
import asyncio,pathlib,tempfile,unittest
from app import db
# Any test fallback store belongs to the temporary validation database.
temporary=tempfile.TemporaryDirectory(prefix='cliperx-validation-')
db.DB_PATH=str(pathlib.Path(temporary.name)/'research.sqlite')
root=pathlib.Path(__import__('os').environ['STAGING'])/'validation/server-py/tests'
patterns=['test_activity_reads.py','test_projection_query_cache.py','test_background_resource_budget.py',
          'test_collector_work_bounds.py','test_realtime_projection.py','test_product_v2.py',
          'test_meme_directory.py','test_live_priority_scheduler.py','test_projection_cooldown.py',
          'test_projection_process.py',
          'test_scoped_detail_reads.py','test_iteration_data_contract.py']
suite=unittest.TestSuite()
for pattern in patterns:
 assert (root/pattern).is_file(),f'Missing required test: {pattern}'
 suite.addTests(unittest.defaultTestLoader.discover(str(root),pattern=pattern))
try:
 result=unittest.TextTestRunner(verbosity=1).run(suite)
finally:
 asyncio.run(db.close_all())
 temporary.cleanup()
assert result.testsRun>=60 and result.wasSuccessful()
PY
# Recheck immediately before changing files, after the staging tests.
python3 - <<'PY'
import hashlib,json,os,pathlib
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name);digest=hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
 assert digest==row['before'],f'Production changed during validation: {name}'
PY
SERVICES_STOPPED=1
pm2 stop "${RESTART_SERVICES[@]}" >/dev/null
CHANGED=1
python3 - <<'PY'
import json,os,pathlib,shutil
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for name in m['files']:
 p=pathlib.Path(name);p.parent.mkdir(parents=True,exist_ok=True)
 temporary=p.with_name(p.name+'.optimization-new')
 shutil.copy2(s/p,temporary);os.replace(temporary,p)
PY
if [[ " ${RESTART_SERVICES[*]} " == *" pyradar "* ]]; then
 pm2 restart pyradar >/dev/null
fi
for attempt in {1..45}; do
 if curl -fsS --max-time 3 http://127.0.0.1:8010/api/health/ready -o /dev/null; then break; fi
 if [ "$attempt" -eq 45 ]; then false; fi
 sleep 2
done
pm2 restart pyradar-worker pyradar-projection >/dev/null
curl --compressed -fsS --max-time 30 'http://127.0.0.1:8010/api/v2/stocks?chain=all&limit=20' -o "$STAGING/stocks.json"
curl --compressed -fsS --max-time 30 'http://127.0.0.1:8010/api/v2/memes?chain=all&limit=20' -o "$STAGING/memes.json"
curl -fsS --max-time 5 'http://127.0.0.1:8011/api/live-market/status' -o "$STAGING/hot-status.json"
python3 - <<'PY'
import hashlib,json,os,pathlib,subprocess,time
s=pathlib.Path(os.environ['STAGING']);b=pathlib.Path(os.environ['BACKUP']);m=json.loads((s/'release.json').read_text())
stocks=json.loads((s/'stocks.json').read_text());memes=json.loads((s/'memes.json').read_text())
assert len(stocks['unified']['stockThemes'])<=20 and stocks['directory']['total']>0
assert len(memes['groups'])<=20 and memes['directory']['catalogTotal']>=memes['directory']['filteredTotal']>0
for name,row in m['files'].items():assert hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()==row['after'],name
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True))
states={row['name']:{'pid':row['pid'],'status':row['pm2_env']['status']} for row in rows if row['name'] in m['services']+m['preserveServices']}
assert all(row['status']=='online' and row['pid'] for row in states.values())
original=json.loads((b/'preserved-pids.json').read_text())
for name,pid in original.items():assert states[name]['pid']==pid,f'Unrelated service restarted: {name}'
print(json.dumps({'services':states,'stockThemes':stocks['directory']['total'],
                  'memeAssets':memes['directory']['catalogTotal'],'backup':str(b)},ensure_ascii=False))
PY
pm2 save >/dev/null
SUCCESS=1
printf 'Optimization deployed; code backup: %s\n' "$BACKUP"
REMOTE
curl --compressed -fsS --max-time 20 'https://cliperx.com/dashboard/api/health/ready' -o /dev/null
printf 'Public API ready after backend optimization.\n'
