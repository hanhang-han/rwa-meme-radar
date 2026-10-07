#!/usr/bin/env bash
# Frozen watch read API only. Static UI is published separately after this succeeds.
set -Eeuo pipefail
FROZEN="${1:?Pass the frozen backend candidate directory}"
BASELINE="${2:?Pass the current production baseline JSON}"
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o IdentitiesOnly=yes -i "${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}")
PACKAGE=$(mktemp /tmp/cliperx-watch-api.XXXXXX.tar.gz)
trap 'rm -f "$PACKAGE"' EXIT
python3 - "$FROZEN" "$BASELINE" "$PACKAGE" <<'PY'
import hashlib,json,pathlib,re,sys,tarfile
root=pathlib.Path(sys.argv[1]).resolve();candidate=json.loads((root/'release.json').read_text());baseline=json.loads(pathlib.Path(sys.argv[2]).read_text())
allowed={'server-py/app/api/product_v2.py','server-py/app/api/watch.py','server-py/app/watch_reads.py'}
assert {'server-py/app/api/product_v2.py','server-py/app/api/watch.py'}<=set(candidate['files'])<=allowed
names={'memedashboard','pyradar','pyradar-worker','pyradar-projection','pyradar-market','pyradar-market-api'}
assert set(baseline['services'])==names and all(r['pid']>0 and r['status']=='online' for r in baseline['services'].values())
def frozen(name):
 p=pathlib.PurePosixPath(name);assert not p.is_absolute() and '..' not in p.parts
 f=root/name;assert f.is_file() and not f.is_symlink() and f.resolve().is_relative_to(root)
 return f
for name,row in candidate['files'].items():
 assert row['before']==baseline['files'][name]
 assert row['before'] is None or re.fullmatch('[0-9a-f]{64}',row['before'])
 assert re.fullmatch('[0-9a-f]{64}',row['after'])
 assert hashlib.sha256(frozen(name).read_bytes()).hexdigest()==row['after']
tests=candidate['tests'];assert 1<=len(tests)<=8 and len(tests)==len(set(tests))
assert any(pathlib.PurePosixPath(name).name.startswith('test_watch_') for name in tests)
assert {'server-py/tests/test_product_v2.py','server-py/tests/test_visible_quotes_stream.py'}<=set(tests)
for name in tests:assert re.fullmatch(r'server-py/tests/test_[A-Za-z0-9_]+\.py',name)
manifest={'schema':1,'kind':'watch-read-api','files':candidate['files'],'tests':tests,
 'testHashes':{n:hashlib.sha256(frozen(n).read_bytes()).hexdigest() for n in tests},
 'indexBefore':baseline['indexBefore'],'services':baseline['services'],'databasePath':baseline['database']['path']}
(root/'deploy-release.json').write_text(json.dumps(manifest,indent=2))
with tarfile.open(sys.argv[3],'w:gz') as out:
 for name in sorted(set(manifest['files'])|set(tests)):out.add(frozen(name),arcname=name,recursive=False)
 out.add(root/'deploy-release.json',arcname='release.json',recursive=False)
print('Frozen watch API package verified:',len(manifest['files']),'source files;',len(tests),'test files')
PY
REMOTE_PACKAGE="/tmp/$(basename "$PACKAGE")"
ssh "${SSH_OPTS[@]}" "$SERVER" "cat > $REMOTE_PACKAGE" < "$PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$REMOTE_PACKAGE" "${DEPLOY_PREFLIGHT_ONLY:-0}" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
mkdir -p .releases
exec 9>.releases/watch-account-api.lock
exec 8>.releases/dashboard-web-only-deploy.lock
exec 7>.releases/visible-quotes-tabs-deploy.lock
exec 6>.releases/pool-pipeline-repair.lock
flock -n 9 && flock -n 8 && flock -n 7 && flock -n 6 || { printf 'Another related release is active\n' >&2; exit 1; }
STAGING=$(realpath "$(mktemp -d .releases/watch-api-staging.XXXXXX)")
BACKUP="/opt/memedashboard/.releases/watch-api-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
export STAGING BACKUP
REMOTE_PACKAGE="$1";PREFLIGHT_ONLY="$2";CHANGED=0;COMPLETE=0
wait_ready() {
 for attempt in {1..30}; do
  if curl -fsS --max-time 2 http://127.0.0.1:8010/api/health/ready -o "$STAGING/ready.json" &&
   python3 -c 'import json,os,pathlib;r=json.loads((pathlib.Path(os.environ["STAGING"])/"ready.json").read_text());assert r.get("ok") is True and r.get("storage")=="ready"'; then return 0; fi
  sleep 1
 done
 return 1
}
finish() {
 result=$?;trap - EXIT
 if [ "$CHANGED" -eq 1 ] && [ "$COMPLETE" -ne 1 ]; then
  set +e
  rollback=0
  pm2 stop pyradar >/dev/null 2>&1 || rollback=1
  python3 - <<'PY'
import hashlib,json,os,pathlib,shutil,tempfile
b=pathlib.Path(os.environ['BACKUP']);m=json.loads((b/'release.json').read_text())
for name,row in m['files'].items():
 target=pathlib.Path(name)
 if row['before'] is None:target.unlink(missing_ok=True);continue
 saved=b/name;assert hashlib.sha256(saved.read_bytes()).hexdigest()==row['before']
 fd,tmp=tempfile.mkstemp(prefix='.watch-api-restore-',dir=target.parent);os.close(fd)
 try:shutil.copy2(saved,tmp);os.replace(tmp,target)
 finally:pathlib.Path(tmp).unlink(missing_ok=True)
 assert hashlib.sha256(target.read_bytes()).hexdigest()==row['before']
PY
  if [ "$?" -ne 0 ]; then rollback=1; fi
  pm2 restart pyradar >/dev/null 2>&1 || rollback=1
  wait_ready || rollback=1
  if [ "$rollback" -eq 0 ]; then printf 'Watch API failed; prior API source restored. Existing data and prior event indexes retained. Backup: %s\n' "$BACKUP" >&2;
  else printf 'Watch API failed; rollback requires inspection: %s\n' "$BACKUP" >&2; fi
 fi
 rm -f "$REMOTE_PACKAGE" 2>/dev/null || true
 exit "$result"
}
trap finish EXIT
python3 - "$REMOTE_PACKAGE" <<'PY'
import hashlib,json,os,pathlib,shutil,subprocess,sys,tarfile
s=pathlib.Path(os.environ['STAGING']);b=pathlib.Path(os.environ['BACKUP'])
with tarfile.open(sys.argv[1]) as archive:
 m=json.load(archive.extractfile('release.json'));assert m['schema']==1 and m['kind']=='watch-read-api'
 allowed=set(m['files'])|set(m['testHashes'])|{'release.json'}
 members=archive.getmembers();assert len(members)==len(allowed) and {i.name for i in members}==allowed
 for i in members:
  p=pathlib.PurePosixPath(i.name);assert not p.is_absolute() and '..' not in p.parts and i.isfile()
 archive.extractall(s)
for name,row in m['files'].items():
 p=pathlib.Path(name)
 assert (p.exists() and not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest()==row['before']) if row['before'] else not p.exists()
 assert hashlib.sha256((s/name).read_bytes()).hexdigest()==row['after']
 if row['before']:
  target=b/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
for name,digest in m['testHashes'].items():assert hashlib.sha256((s/name).read_bytes()).hexdigest()==digest
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore']
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True));actual={r['name']:r for r in rows if r.get('name') in m['services']}
assert set(actual)==set(m['services']) and all(actual[n]['pid']==r['pid'] and actual[n]['pm2_env']['status']=='online' for n,r in m['services'].items())
shutil.copy2(s/'release.json',b/'release.json')
v=s/'validation';shutil.copytree('server-py/app',v/'server-py/app',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
shutil.copytree('src/catalogues',v/'src/catalogues')
if pathlib.Path('contracts/artifacts').is_dir():shutil.copytree('contracts/artifacts',v/'contracts/artifacts')
for name in list(m['files'])+m['tests']:
 p=v/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(s/name,p)
print('API source, dashboard and six service baselines verified; isolated validation prepared')
PY
NODE_ENV=test PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$STAGING/validation/server-py" server-py/.venv/bin/python - <<'PY'
import asyncio,json,os,pathlib,py_compile,tempfile,unittest
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text());v=s/'validation'
for name in m['files']:py_compile.compile(str(v/name),cfile=str(s/(pathlib.Path(name).name+'.pyc')),doraise=True)
with tempfile.TemporaryDirectory(prefix='watch-api-tests-',dir=s) as folder:
 os.chdir(folder);os.environ['RESEARCH_DB']=folder+'/research.sqlite';os.environ['STREAM_LEDGER_PATH']=folder+'/stream.sqlite'
 from app import db
 db.DB_PATH=os.environ['RESEARCH_DB']
 suite=unittest.TestSuite()
 for name in m['tests']:suite.addTests(unittest.defaultTestLoader.discover(str(v/'server-py/tests'),pattern=pathlib.Path(name).name))
 assert suite.countTestCases()>0
 result=unittest.TextTestRunner(verbosity=1).run(suite);asyncio.run(db.close_all());assert result.wasSuccessful()
print('Server runtime passed isolated watch and prior quote/directory tests')
PY
if [ "$PREFLIGHT_ONLY" = "1" ]; then COMPLETE=1; printf 'API preflight passed; running services, source and database unchanged\n'; exit 0; fi
# Create only reviewed event-table lookup indexes, never in an HTTP handler.
NODE_ENV=test PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$STAGING/validation/server-py" server-py/.venv/bin/python - <<'PY'
import json,os,pathlib,sqlite3,time
s=pathlib.Path(os.environ['STAGING']);b=pathlib.Path(os.environ['BACKUP']);m=json.loads((s/'release.json').read_text())
from app.api.watch import WATCH_EVENT_INDEX_SQL
assert 1<=len(WATCH_EVENT_INDEX_SQL)<=6
conn=sqlite3.connect(m['databasePath'],timeout=3,isolation_level=None);conn.execute('PRAGMA busy_timeout=3000');conn.execute('PRAGMA cache_size=-4096');conn.execute('PRAGMA temp_store=FILE')
before=[{'name':r[0],'sql':r[1]} for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='events'")]
(b/'event-indexes-before.json').write_text(json.dumps(before,indent=2))
report=[]
try:
 for sql in WATCH_EVENT_INDEX_SQL:
  assert sql.strip().upper().startswith('CREATE INDEX IF NOT EXISTS ') and ' ON events' in sql and ';' not in sql
  started=time.monotonic();conn.set_progress_handler(lambda:int(time.monotonic()-started>10),1000)
  conn.execute(sql);report.append({'sql':sql,'elapsedMs':round((time.monotonic()-started)*1000)})
finally:
 conn.set_progress_handler(None,0);conn.close();(b/'event-index-preparation.json').write_text(json.dumps(report,indent=2))
print(json.dumps({'eventIndexesPrepared':len(report),'elapsedMs':sum(r['elapsedMs'] for r in report)}))
PY
python3 - <<'PY'
import hashlib,json,os,pathlib,subprocess
m=json.loads((pathlib.Path(os.environ['STAGING'])/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name);assert (p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()==row['before']) if row['before'] else not p.exists()
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore']
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True));actual={r['name']:r for r in rows if r.get('name') in m['services']}
assert set(actual)==set(m['services']) and all(actual[n]['pid']==r['pid'] and actual[n]['pm2_env']['status']=='online' for n,r in m['services'].items())
PY
CHANGED=1
pm2 stop pyradar >/dev/null
python3 - <<'PY'
import json,os,pathlib,shutil,tempfile
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for name in m['files']:
 p=pathlib.Path(name);fd,tmp=tempfile.mkstemp(prefix='.watch-api-publish-',dir=p.parent);os.close(fd)
 try:shutil.copy2(s/name,tmp);os.replace(tmp,p)
 finally:pathlib.Path(tmp).unlink(missing_ok=True)
PY
pm2 restart pyradar >/dev/null
wait_ready
python3 - <<'PY'
import hashlib,json,os,pathlib,subprocess,time,urllib.request
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for base in ('http://127.0.0.1:8010/api/','https://cliperx.com/dashboard/api/'):
 for route in ('v2/watch/summary','v2/watch/events'):
  request=urllib.request.Request(base+route,data=json.dumps({'keys':[],'chain':'all','limit':20}).encode(),headers={'Content-Type':'application/json'})
  with urllib.request.urlopen(request,timeout=15) as response:d=json.load(response)
  assert response.status==200 and d.get('items')==[],route
 print('Empty watch reads verified:',base)
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True));actual={r['name']:r for r in rows if r.get('name') in m['services']}
assert set(actual)==set(m['services']) and all(r['pid']>0 and r['pm2_env']['status']=='online' for r in actual.values())
for n,before in m['services'].items():
 if n!='pyradar':assert actual[n]['pid']==before['pid'],f'Unrelated service changed: {n}'
for name,row in m['files'].items():assert hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()==row['after']
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore']
print(json.dumps({'checkedAt':int(time.time()*1000),'services':{n:{'pid':r['pid'],'status':r['pm2_env']['status']} for n,r in actual.items()},'backup':os.environ['BACKUP']}))
PY
COMPLETE=1
printf 'Watch read API live; only pyradar restarted; dashboard unchanged. Rollback: %s\n' "$BACKUP"
REMOTE
