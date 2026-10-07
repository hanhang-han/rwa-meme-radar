#!/usr/bin/env bash
# On-host deployment of eight checksum-reviewed architecture files only.
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
ARCH_CANDIDATE=$(realpath "${1:?Pass validated candidate directory}")
export ARCH_CANDIDATE
exec 9>.releases/pool-pipeline-repair.lock
exec 8>.releases/dashboard-web-only-deploy.lock
exec 7>.releases/architecture-deploy.lock
flock -n 9
flock -n 8
flock -n 7
ARCH_BACKUP="$(pwd)/.releases/architecture-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -m 700 "$ARCH_BACKUP"
export ARCH_BACKUP
ARCH_CHANGED=0
ARCH_COMPLETE=0
wait_ready() {
 for attempt in {1..25}; do
  if curl -fsS --max-time 2 http://127.0.0.1:8010/api/health/ready -o /dev/null 2>/dev/null; then return 0; fi
  sleep 1
 done
 return 1
}
finish() {
 code=$?
 trap - EXIT
 if [ "$ARCH_CHANGED" -eq 1 ] && [ "$ARCH_COMPLETE" -ne 1 ]; then
  bash "$ARCH_BACKUP/rollback.sh" || printf 'Architecture rollback needs inspection: %s\n' "$ARCH_BACKUP" >&2
 fi
 exit "$code"
}
trap finish EXIT
server-py/.venv/bin/python - <<'PY'
import hashlib,json,os,pathlib,shutil,subprocess
s=pathlib.Path(os.environ['ARCH_CANDIDATE']);b=pathlib.Path(os.environ['ARCH_BACKUP'])
m=json.loads((s/'release.json').read_text())
allowed={'server-py/app/worker.py','server-py/app/resource_budget.py','server-py/app/collectors/scheduler.py',
 'server-py/app/realtime_projection.py','server-py/app/read_model_store.py','server-py/app/read_model_worker.py',
 'server-py/requirements.txt','ecosystem.config.cjs'}
assert set(m['files'])==allowed
for name,row in m['files'].items():
 p=pathlib.Path(name);assert not p.is_symlink()
 assert (hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None)==row['before'], name+' changed since audit'
 assert hashlib.sha256((s/p).read_bytes()).hexdigest()==row['after'], name+' package mismatch'
 if p.exists():
  saved=b/p;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,saved)
shutil.copy2(s/'release.json',b/'release.json')
config=pathlib.Path('data/architecture-runtime.json')
assert json.loads(config.read_text()).get('enabled') is False
shutil.copy2(config,b/'architecture-runtime.json');(b/'architecture-runtime.json').chmod(0o600)
processes={r['name']:r.get('pid',0) for r in json.loads(subprocess.check_output(['pm2','jlist']))}
assert 'pyradar-read-model' not in processes
(b/'pids.json').write_text(json.dumps(processes))
print('Eight source baselines verified; private rollback saved on host')
PY
cat > "$ARCH_BACKUP/rollback.sh" <<'ROLLBACK'
#!/usr/bin/env bash
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
export ARCH_RESTORE=$(dirname "$(realpath "$0")")
server-py/.venv/bin/python - <<'PY'
import hashlib,json,os,pathlib
b=pathlib.Path(os.environ['ARCH_RESTORE']);m=json.loads((b/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name);value=hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
 assert value in (row['before'],row['after']), name+' changed after release; refusing to overwrite'
PY
pm2 delete pyradar-read-model >/dev/null 2>&1 || true
pm2 stop pyradar-worker pyradar >/dev/null
server-py/.venv/bin/python - <<'PY'
import json,os,pathlib,shutil
b=pathlib.Path(os.environ['ARCH_RESTORE']);m=json.loads((b/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name)
 if row['before'] is None:p.unlink(missing_ok=True)
 else:
  temp=p.with_name(p.name+'.architecture-restore');shutil.copy2(b/p,temp);os.replace(temp,p)
p=pathlib.Path('data/architecture-runtime.json');temp=p.with_suffix('.restore')
shutil.copy2(b/'architecture-runtime.json',temp);temp.chmod(0o600);os.replace(temp,p)
PY
pm2 restart pyradar-worker pyradar >/dev/null
pm2 save >/dev/null
printf 'Architecture source restored; original SQLite history retained\n'
ROLLBACK
chmod 700 "$ARCH_BACKUP/rollback.sh"
ARCH_CHANGED=1
server-py/.venv/bin/python - <<'PY'
import json,os,pathlib,shutil
s=pathlib.Path(os.environ['ARCH_CANDIDATE']);m=json.loads((s/'release.json').read_text())
for name in m['files']:
 p=pathlib.Path(name);temp=p.with_name(p.name+'.architecture-release')
 shutil.copy2(s/p,temp);os.replace(temp,p)
print('Reviewed source published; public read switch remains disabled')
PY
pm2 start ecosystem.config.cjs --only pyradar-read-model >/dev/null
for attempt in {1..25}; do
 if server-py/.venv/bin/python - <<'PY'
import json,pathlib,time
p=pathlib.Path('data/read-model-health.json')
if not p.exists():raise SystemExit(1)
d=json.loads(p.read_text())
raise SystemExit(0 if d.get('status')=='ready' and time.time()*1000-d.get('lastVerifiedAt',0)<6000 else 1)
PY
 then break; fi
 sleep 1
done
PYTHONPATH=/opt/memedashboard/server-py server-py/.venv/bin/python /tmp/cliperx-architecture-acceptance.py verify "$ARCH_BACKUP/shadow-verification.json"
pm2 restart pyradar-worker >/dev/null
server-py/.venv/bin/python - <<'PY'
import json,os,pathlib
p=pathlib.Path('data/architecture-runtime.json');d=json.loads(p.read_text());d['enabled']=True
t=p.with_suffix('.enable');t.write_text(json.dumps(d,indent=2));t.chmod(0o600);os.replace(t,p)
print('Verified PostgreSQL/Redis public read switch enabled')
PY
# Refresh publisher health's startup flag after the activation.
pm2 restart pyradar-read-model >/dev/null
pm2 restart pyradar >/dev/null
wait_ready
server-py/.venv/bin/python /tmp/cliperx-architecture-acceptance.py sample "$ARCH_BACKUP/after-sample.json"
server-py/.venv/bin/python - <<'PY'
import hashlib,json,os,pathlib,subprocess
b=pathlib.Path(os.environ['ARCH_BACKUP']);m=json.loads((b/'release.json').read_text())
for name,row in m['files'].items():assert hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()==row['after'],name
before=json.loads((b/'pids.json').read_text())
rows={r['name']:r for r in json.loads(subprocess.check_output(['pm2','jlist']))}
unchanged={n:p for n,p in before.items() if n not in ('pyradar','pyradar-worker')}
assert all(rows[n].get('pid',0)==p for n,p in unchanged.items()),'unrelated runtime restarted'
for name in ('pyradar','pyradar-worker','pyradar-read-model'):
 assert rows[name]['pid']>0 and rows[name]['pm2_env']['status']=='online',name
(b/'receipt.json').write_text(json.dumps({'backup':str(b),'files':list(m['files']),
 'services':{n:{'pid':r.get('pid',0),'status':r['pm2_env']['status']} for n,r in rows.items()},
 'unchangedServices':list(unchanged),'publicReadsEnabled':True},indent=2))
print('API routes ready; independent live-market and other runtimes retain their original PIDs')
PY
pm2 save >/dev/null
ARCH_COMPLETE=1
printf 'Architecture deployed; rollback and acceptance: %s\n' "$ARCH_BACKUP"
