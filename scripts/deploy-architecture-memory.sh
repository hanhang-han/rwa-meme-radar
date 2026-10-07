#!/usr/bin/env bash
# Byte-cache follow-up; preserve publishers, native markets and settings.
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
ARCH_STAGE=$(realpath "${1:?Pass validated follow-up directory}")
ARCH_READER_BACKUP="$(pwd)/.releases/architecture-memory-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
export ARCH_STAGE ARCH_READER_BACKUP
exec 9>.releases/pool-pipeline-repair.lock
exec 8>.releases/dashboard-web-only-deploy.lock
exec 7>.releases/architecture-deploy.lock
flock -n 9
flock -n 8
flock -n 7
mkdir -m 700 "$ARCH_READER_BACKUP"
CHANGED=0
COMPLETE=0
finish() {
 result=$?
 trap - EXIT
 if [ "$CHANGED" -eq 1 ] && [ "$COMPLETE" -ne 1 ]; then
  bash "$ARCH_READER_BACKUP/rollback.sh" || printf 'Reader rollback needs inspection\n' >&2
 fi
 exit "$result"
}
trap finish EXIT
server-py/.venv/bin/python - <<'PY'
import hashlib,json,os,pathlib,shutil,subprocess
s=pathlib.Path(os.environ['ARCH_STAGE']);b=pathlib.Path(os.environ['ARCH_READER_BACKUP']);m=json.loads((s/'release.json').read_text())
assert set(m['files'])=={'server-py/app/realtime_projection.py','server-py/app/projection_query_cache.py'}
tests=json.loads((s/'preflight.json').read_text());assert tests['successful'] and tests['testsRun']==168 and tests['skipped']==0
for n,r in m['files'].items():
 p=pathlib.Path(n);assert not p.is_symlink()
 assert hashlib.sha256(p.read_bytes()).hexdigest()==r['before'],n+' changed after verification'
 assert hashlib.sha256((s/n).read_bytes()).hexdigest()==r['after']
 saved=b/p;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,saved)
shutil.copy2(s/'release.json',b/'release.json')
state={'pids':{r['name']:r.get('pid',0) for r in json.loads(subprocess.check_output(['pm2','jlist']))},
 'settingsHash':hashlib.sha256(pathlib.Path('data/architecture-runtime.json').read_bytes()).hexdigest()}
(b/'state.json').write_text(json.dumps(state))
print('Reader source and 168-test gate verified')
PY
cat > "$ARCH_READER_BACKUP/rollback.sh" <<'ROLLBACK'
#!/usr/bin/env bash
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
export ARCH_READER_RESTORE=$(dirname "$(realpath "$0")")
server-py/.venv/bin/python - <<'PY'
import hashlib,json,os,pathlib,shutil
b=pathlib.Path(os.environ['ARCH_READER_RESTORE']);m=json.loads((b/'release.json').read_text())
for n,r in m['files'].items():
 assert hashlib.sha256(pathlib.Path(n).read_bytes()).hexdigest() in (r['before'],r['after']),n+' changed; refusing overwrite'
for n in m['files']:
 p=pathlib.Path(n);t=p.with_name(p.name+'.restore-reader');shutil.copy2(b/p,t);os.replace(t,p)
PY
pm2 restart pyradar >/dev/null
pm2 save >/dev/null
printf 'Reader-only follow-up restored\n'
ROLLBACK
chmod 700 "$ARCH_READER_BACKUP/rollback.sh"
CHANGED=1
server-py/.venv/bin/python - <<'PY'
import json,os,pathlib,shutil
s=pathlib.Path(os.environ['ARCH_STAGE']);m=json.loads((s/'release.json').read_text())
for n in m['files']:
 p=pathlib.Path(n);t=p.with_name(p.name+'.reader-release');shutil.copy2(s/p,t);os.replace(t,p)
PY
pm2 restart pyradar >/dev/null
for attempt in {1..25}; do
 if curl -fsS --max-time 2 http://127.0.0.1:8010/api/health/ready -o /dev/null 2>/dev/null; then break; fi
 sleep 1
done
server-py/.venv/bin/python /tmp/cliperx-architecture-acceptance.py sample "$ARCH_READER_BACKUP/after-sample.json"
server-py/.venv/bin/python - <<'PY'
import hashlib,json,os,pathlib,subprocess
b=pathlib.Path(os.environ['ARCH_READER_BACKUP']);s=json.loads((b/'state.json').read_text());m=json.loads((b/'release.json').read_text())
for n,r in m['files'].items():assert hashlib.sha256(pathlib.Path(n).read_bytes()).hexdigest()==r['after']
assert hashlib.sha256(pathlib.Path('data/architecture-runtime.json').read_bytes()).hexdigest()==s['settingsHash']
rows={r['name']:r for r in json.loads(subprocess.check_output(['pm2','jlist']))}
assert all(rows[n].get('pid',0)==pid for n,pid in s['pids'].items() if n!='pyradar')
assert rows['pyradar']['pm2_env']['status']=='online' and rows['pyradar']['pid']>0
(b/'receipt.json').write_text(json.dumps({'backup':str(b),'files':list(m['files']),
 'restarted':['pyradar'],'otherPidsUnchanged':True,'settingsUnchanged':True},indent=2))
print('Reader ready; publisher, quotes, native markets and settings unchanged')
PY
pm2 save >/dev/null
COMPLETE=1
printf 'Snapshot byte cache deployed; rollback: %s\n' "$ARCH_READER_BACKUP"
