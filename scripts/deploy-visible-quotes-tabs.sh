#!/usr/bin/env bash
# Frozen stock/Meme quote release only. No database, environment or Nginx changes.
# Usage: deploy-visible-quotes-tabs.sh /absolute/frozen-candidate /absolute/baseline.json
# Candidate release.json: files:{four backend paths:{before,after}}, tests:[test paths].
# Baseline: indexBefore plus all six services:{name:{pid,status}}.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
FROZEN="${1:?Pass the frozen candidate directory containing release.json and web/dist}"
BASELINE="${2:?Pass the production baseline JSON containing indexBefore and six service PIDs}"
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o IdentitiesOnly=yes -i "${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}")
PACKAGE=$(mktemp /tmp/cliperx-visible-quotes.XXXXXX.tar.gz)
MANIFEST=$(mktemp /tmp/cliperx-visible-quotes.XXXXXX.json)
trap 'rm -f "$PACKAGE" "$MANIFEST"' EXIT
python3 - "$FROZEN" "$BASELINE" "$MANIFEST" "$PACKAGE" <<'PY'
import hashlib,json,pathlib,re,sys,tarfile
root=pathlib.Path(sys.argv[1]).resolve(strict=True)
candidate=json.loads((root/'release.json').read_text())
baseline=json.loads(pathlib.Path(sys.argv[2]).read_text())
expected={'server-py/app/api/stream.py','server-py/app/collectors/live_quotes.py',
          'server-py/app/api/product_v2.py','server-py/app/dashboard_projection.py'}
names={'memedashboard','pyradar','pyradar-worker','pyradar-projection','pyradar-market','pyradar-market-api'}
digest=lambda value:isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None
before=baseline['indexBefore'];assert digest(before),'Invalid index baseline'
assert set(baseline['services'])==names,'Require the six known service baselines'
for name,row in baseline['services'].items():
 assert isinstance(row['pid'],int) and row['pid']>0 and row['status']=='online',f'Baseline service unavailable: {name}'
assert set(candidate['files'])==expected,'Candidate must contain exactly the four reviewed backend files'
def frozen_file(name):
 p=pathlib.PurePosixPath(name)
 assert not p.is_absolute() and '..' not in p.parts and str(p)==name,'Invalid frozen file name'
 target=root/name
 assert target.is_file() and not target.is_symlink() and target.resolve().is_relative_to(root),f'Unsafe frozen file: {name}'
 for parent in target.parents:
  if parent==root:break
  assert not parent.is_symlink(),f'Symlinked frozen directory: {name}'
 return target
backend={}
for name,row in candidate['files'].items():
 assert digest(row['before']) and digest(row['after']),f'Invalid source baseline: {name}'
 assert hashlib.sha256(frozen_file(name).read_bytes()).hexdigest()==row['after'],f'Frozen source changed: {name}'
 backend[name]={'before':row['before'],'after':row['after']}
tests=candidate['tests']
assert isinstance(tests,list) and tests and len(tests)==len(set(tests)) and len(tests)<=12,'Require bounded candidate test files'
assert {'server-py/tests/test_visible_quotes_stream.py','server-py/tests/test_product_v2.py'} <= set(tests),'Visible quote and directory route tests required'
test_hashes={}
for name in tests:
 assert re.fullmatch(r'server-py/tests/test_[A-Za-z0-9_]+\.py',name),f'Unexpected test path: {name}'
 test_hashes[name]=hashlib.sha256(frozen_file(name).read_bytes()).hexdigest()
index=frozen_file('web/dist/index.html')
assert '/dashboard/assets/' in index.read_text(),'Build the frozen frontend with the /dashboard/ base'
static={}
for p in sorted((root/'web/dist').rglob('*')):
 assert not p.is_symlink(),'Symlink in frozen static release'
 if p.is_file():
  name=p.relative_to(root).as_posix();static[name]=hashlib.sha256(frozen_file(name).read_bytes()).hexdigest()
assert static and static['web/dist/index.html']!=before,'Frontend index unchanged'
m={'schema':1,'kind':'visible-quotes-tabs','indexBefore':before,'indexAfter':static['web/dist/index.html'],
   'files':backend,'static':static,'tests':tests,'testHashes':test_hashes,'services':baseline['services']}
pathlib.Path(sys.argv[3]).write_text(json.dumps(m,indent=2))
with tarfile.open(sys.argv[4],'w:gz') as archive:
 for name in sorted(set(backend)|set(static)|set(tests)):archive.add(frozen_file(name),arcname=name,recursive=False)
 archive.add(sys.argv[3],arcname='release.json',recursive=False)
print(f'Frozen release verified: four backend files, {len(static)} static files and {len(tests)} tests')
PY
REMOTE_PACKAGE="/tmp/$(basename "$PACKAGE")"
ssh "${SSH_OPTS[@]}" "$SERVER" "cat > $REMOTE_PACKAGE" < "$PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$REMOTE_PACKAGE" "${DEPLOY_PREFLIGHT_ONLY:-0}" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
mkdir -p .releases
exec 9>.releases/visible-quotes-tabs-deploy.lock
exec 8>.releases/dashboard-web-only-deploy.lock
exec 7>.releases/pool-pipeline-repair.lock
flock -n 9 || { printf 'Another visible quote release is running\n' >&2; exit 1; }
flock -n 8 || { printf 'Another dashboard release is running\n' >&2; exit 1; }
flock -n 7 || { printf 'Another pool pipeline release is running\n' >&2; exit 1; }
STAGING=$(mktemp -d .releases/visible-quotes-staging.XXXXXX)
STAGING=$(realpath "$STAGING")
BACKUP="/opt/memedashboard/.releases/visible-quotes-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
export STAGING BACKUP
REMOTE_PACKAGE="$1"
PREFLIGHT_ONLY="${2:-0}"
BACKEND_CHANGED=0
STATIC_CHANGED=0
COMPLETE=0
RESTART_SERVICES=(pyradar pyradar-worker pyradar-projection)
wait_ready() {
 for attempt in {1..45}; do
  if curl -fsS --max-time 2 http://127.0.0.1:8010/api/health/ready -o "$STAGING/local-ready.json" 2>/dev/null &&
     python3 -c 'import json,os,pathlib;d=json.loads((pathlib.Path(os.environ["STAGING"])/"local-ready.json").read_text());assert d.get("ok") is True and d.get("storage")=="ready"' 2>/dev/null; then return 0; fi
  sleep 2
 done
 return 1
}
finish() {
 result=$?
 trap - EXIT
 if [ "$COMPLETE" -ne 1 ] && { [ "$BACKEND_CHANGED" -eq 1 ] || [ "$STATIC_CHANGED" -eq 1 ]; }; then
  set +e
  rollback=0
  if [ "$BACKEND_CHANGED" -eq 1 ]; then pm2 stop "${RESTART_SERVICES[@]}" >/dev/null 2>&1 || rollback=1; fi
  export BACKEND_CHANGED STATIC_CHANGED
  python3 - <<'PYROLLBACK'
import ctypes,hashlib,json,os,pathlib,shutil,tempfile
b=pathlib.Path(os.environ['BACKUP']);m=json.loads((b/'release.json').read_text())
if os.environ['BACKEND_CHANGED']=='1':
 for name,row in m['files'].items():assert hashlib.sha256((b/name).read_bytes()).hexdigest()==row['before'],f'Backup corrupted: {name}'
 for name,row in m['files'].items():
  target=pathlib.Path(name);saved=b/name
  fd,temp=tempfile.mkstemp(prefix='.visible-quotes-restore-',dir=target.parent);os.close(fd)
  try:shutil.copy2(saved,temp);os.replace(temp,target)
  finally:pathlib.Path(temp).unlink(missing_ok=True)
  assert hashlib.sha256(target.read_bytes()).hexdigest()==row['before']
if os.environ['STATIC_CHANGED']=='1':
 for p in pathlib.Path('web/dist/assets').rglob('*'):
  if not p.is_file():continue
  target=b/'web-dist/assets'/p.relative_to('web/dist/assets');target.parent.mkdir(parents=True,exist_ok=True)
  if target.exists():assert target.read_bytes()==p.read_bytes(),f'Asset collision on restore: {p.name}'
  else:shutil.copy2(p,target)
 libc=ctypes.CDLL(None,use_errno=True)
 if libc.renameat2(-100,b'web/dist',-100,os.fsencode(str(b/'web-dist')),2):raise OSError(ctypes.get_errno(),'Static rollback failed')
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore']
PYROLLBACK
  if [ "$?" -ne 0 ]; then rollback=1; fi
  if [ "$BACKEND_CHANGED" -eq 1 ]; then pm2 restart "${RESTART_SERVICES[@]}" >/dev/null 2>&1 || rollback=1; fi
  wait_ready || rollback=1
  python3 - <<'PYROLLBACKCHECK'
import json,os,pathlib,subprocess,urllib.request,hashlib
b=pathlib.Path(os.environ['BACKUP']);m=json.loads((b/'release.json').read_text())
names={'memedashboard','pyradar','pyradar-worker','pyradar-projection','pyradar-market','pyradar-market-api'}
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True));actual={r['name']:r for r in rows if r.get('name') in names}
assert set(actual)==names and all(r['pid']>0 and r['pm2_env']['status']=='online' for r in actual.values())
for name in ('memedashboard','pyradar-market','pyradar-market-api'):assert actual[name]['pid']==m['services'][name]['pid'],f'Unrelated service changed: {name}'
request=urllib.request.Request('https://cliperx.com/dashboard/',headers={'Cache-Control':'no-cache'})
with urllib.request.urlopen(request,timeout=15) as response:html=response.read()
assert hashlib.sha256(html).hexdigest()==m['indexBefore'],'Public index rollback unconfirmed'
PYROLLBACKCHECK
  if [ "$?" -ne 0 ]; then rollback=1; fi
  if [ "$rollback" -eq 0 ]; then printf 'Quote release failed; four files and old dashboard restored. Backup: %s\n' "$BACKUP" >&2;
  else printf 'Quote release failed; rollback needs inspection: %s\n' "$BACKUP" >&2; fi
 fi
 rm -f "$REMOTE_PACKAGE" 2>/dev/null || true
 exit "$result"
}
trap finish EXIT
python3 - "$REMOTE_PACKAGE" <<'PYEXTRACT'
import ctypes,hashlib,json,os,pathlib,shutil,subprocess,sys,tarfile
s=pathlib.Path(os.environ['STAGING']);b=pathlib.Path(os.environ['BACKUP'])
expected={'server-py/app/api/stream.py','server-py/app/collectors/live_quotes.py','server-py/app/api/product_v2.py','server-py/app/dashboard_projection.py'}
with tarfile.open(sys.argv[1]) as archive:
 m=json.load(archive.extractfile('release.json'))
 assert m['schema']==1 and m['kind']=='visible-quotes-tabs' and set(m['files'])==expected
 allowed=set(m['files'])|set(m['static'])|set(m['testHashes'])|{'release.json'}
 members=archive.getmembers();assert len(members)==len(allowed) and {item.name for item in members}==allowed,'Unexpected package files'
 for item in members:
  p=pathlib.PurePosixPath(item.name)
  assert not p.is_absolute() and '..' not in p.parts and item.isfile(),'Unsafe package member'
  assert item.name=='release.json' or item.name in expected or item.name.startswith('web/dist/') or item.name in m['tests'],'Unexpected mutation scope'
 archive.extractall(s)
assert hasattr(ctypes.CDLL(None),'renameat2'),'Atomic directory exchange unavailable'
for name,row in m['files'].items():
 p=pathlib.Path(name);assert p.is_file() and not p.is_symlink(),f'Unsafe production source: {name}'
 assert hashlib.sha256(p.read_bytes()).hexdigest()==row['before'],f'Production source changed: {name}'
 assert hashlib.sha256((s/p).read_bytes()).hexdigest()==row['after'],f'Staged source mismatch: {name}'
 target=b/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
for group in ('static','testHashes'):
 for name,digest in m[group].items():assert hashlib.sha256((s/name).read_bytes()).hexdigest()==digest,f'Staged file mismatch: {name}'
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore'],'Dashboard baseline changed'
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True));actual={r['name']:r for r in rows if r.get('name') in m['services']}
assert set(actual)==set(m['services']) and all(actual[name]['pid']==before['pid'] and actual[name]['pm2_env']['status']=='online' for name,before in m['services'].items()),'Six service baselines changed'
shutil.copy2(s/'release.json',b/'release.json');shutil.copytree('web/dist',b/'web-dist')
# Build an isolated validation tree, overlaying only the four candidate files.
# Catalogues are read-only prerequisites; nothing here is copied to production.
v=s/'validation';shutil.copytree('server-py/app',v/'server-py/app',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
shutil.copytree('src/catalogues',v/'src/catalogues')
if pathlib.Path('contracts/artifacts').is_dir():shutil.copytree('contracts/artifacts',v/'contracts/artifacts')
for name in list(m['files'])+m['tests']:
 target=v/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(s/name,target)
print('Four source baselines, frozen static hashes and six service PIDs verified; rollback prepared')
PYEXTRACT
NODE_ENV=test PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$STAGING/validation/server-py" server-py/.venv/bin/python - <<'PYTEST'
import asyncio,json,os,pathlib,py_compile,tempfile,unittest
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text());v=s/'validation'
for name in m['files']:
 py_compile.compile(str(v/name),cfile=str(s/(pathlib.Path(name).name+'.candidate.pyc')),doraise=True)
with tempfile.TemporaryDirectory(prefix='visible-quotes-test-',dir=s) as folder:
 os.chdir(folder)
 os.environ['RESEARCH_DB']=folder+'/research.sqlite';os.environ['STREAM_LEDGER_PATH']=folder+'/stream.sqlite'
 from app import db
 db.DB_PATH=os.environ['RESEARCH_DB']
 suite=unittest.TestSuite()
 selected=m['tests'] if (v/'contracts/artifacts/PairRegistry.json').is_file() else [name for name in m['tests'] if pathlib.Path(name).name in ('test_visible_quotes_stream.py','test_product_v2.py')]
 if len(selected)<len(m['tests']):print('Public ABI fixture unavailable; running quote subscription and directory route tests only')
 for name in selected:suite.addTests(unittest.defaultTestLoader.discover(str(v/'server-py/tests'),pattern=pathlib.Path(name).name))
 assert suite.countTestCases()>0,'Candidate test suite is empty'
 result=unittest.TextTestRunner(verbosity=1).run(suite)
 asyncio.run(db.close_all())
 assert result.wasSuccessful(),'Candidate quote tests failed'
print('Server venv compiled the four candidate files and passed isolated quote tests')
PYTEST
if [ "$PREFLIGHT_ONLY" = "1" ]; then COMPLETE=1; printf 'Preflight passed; no running service or production file changed\n'; exit 0; fi
# Recheck immediately before stopping the three permitted services.
python3 - <<'PYBASELINE'
import hashlib,json,os,pathlib,subprocess
m=json.loads((pathlib.Path(os.environ['STAGING'])/'release.json').read_text())
for name,row in m['files'].items():assert hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()==row['before'],f'Source baseline changed: {name}'
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore']
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True));actual={r['name']:r for r in rows if r.get('name') in m['services']}
assert set(actual)==set(m['services']) and all(actual[name]['pid']==before['pid'] and actual[name]['pm2_env']['status']=='online' for name,before in m['services'].items()),'Service baseline changed'
PYBASELINE
BACKEND_CHANGED=1
pm2 stop "${RESTART_SERVICES[@]}" >/dev/null
python3 - <<'PYPUBLISH'
import json,os,pathlib,shutil,tempfile
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for name in m['files']:
 target=pathlib.Path(name);fd,temp=tempfile.mkstemp(prefix='.visible-quotes-publish-',dir=target.parent);os.close(fd)
 try:shutil.copy2(s/name,temp);os.replace(temp,target)
 finally:pathlib.Path(temp).unlink(missing_ok=True)
PYPUBLISH
pm2 restart pyradar >/dev/null
wait_ready
pm2 restart pyradar-worker pyradar-projection >/dev/null
python3 - <<'PYSMOKE'
import json,urllib.error,urllib.parse,urllib.request
tokens='196:0x'+'ab'*20
for base in ('http://127.0.0.1:8010/api/','https://cliperx.com/dashboard/api/'):
 with urllib.request.urlopen(base+'health/ready',timeout=15) as response:d=json.load(response)
 assert d.get('ok') is True and d.get('storage')=='ready',f'Readiness failed: {base}'
 for route in ('stream','v2/stream'):
  query={'protocol':'2','scope':'quotes','snapshot':'false','trades':'none','candles':'none','tokens':tokens}
  request=urllib.request.Request(base+route+'?'+urllib.parse.urlencode(query),headers={'Accept':'text/event-stream'})
  with urllib.request.urlopen(request,timeout=12) as response:
   assert response.status==200 and response.headers.get_content_type()=='text/event-stream'
   lines=[]
   for _ in range(16):
    line=response.readline(65536)
    assert line and len(line)<65536,'Invalid/buffered SSE greeting'
    lines.append(line)
    if line.strip()==b'':break
   frame=b''.join(lines)
   assert b'event: hello\n' in frame,'Quote subscription greeting missing'
   body=json.loads(next(line[5:].strip() for line in lines if line.startswith(b'data:')))
   assert isinstance(body,dict) and isinstance(body.get('cursor'),int) and body['cursor']>=0,'Invalid quote stream cursor'
  invalid={**query,'tokens':'all'}
  try:
   with urllib.request.urlopen(base+route+'?'+urllib.parse.urlencode(invalid),timeout=12):pass
  except urllib.error.HTTPError as error:assert error.code==422,f'Invalid subscription status: {error.code}'
  else:raise AssertionError('Unbounded subscription was accepted')
  print('Quote-stream hello and invalid-subscription 422 verified:',base+route)
 print('Readiness verified:',base)
PYSMOKE
# Preserve both generations of immutable assets and publish HTML atomically.
python3 - <<'PYSTATICPREP'
import json,os,pathlib,shutil,hashlib
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore'],'Dashboard changed before static publication'
for p in pathlib.Path('web/dist/assets').rglob('*'):
 if not p.is_file():continue
 target=s/'web/dist/assets'/p.relative_to('web/dist/assets');target.parent.mkdir(parents=True,exist_ok=True)
 if target.exists():assert target.read_bytes()==p.read_bytes(),f'Immutable asset collision: {p.name}'
 else:shutil.copy2(p,target)
PYSTATICPREP
STATIC_CHANGED=1
python3 - <<'PYSTATICSWAP'
import ctypes,os
libc=ctypes.CDLL(None,use_errno=True)
if libc.renameat2(-100,b'web/dist',-100,os.fsencode(os.environ['STAGING']+'/web/dist'),2):raise OSError(ctypes.get_errno(),'Static exchange failed')
PYSTATICSWAP
PUBLIC_MATCH=0
for attempt in {1..5}; do
 if curl --compressed -fsS --max-time 20 -H 'Cache-Control: no-cache' https://cliperx.com/dashboard/ -o "$STAGING/public-index.html" &&
    python3 - <<'PYPUBLICINDEX'
import hashlib,json,os,pathlib
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
assert hashlib.sha256((s/'public-index.html').read_bytes()).hexdigest()==m['indexAfter']
PYPUBLICINDEX
 then PUBLIC_MATCH=1; break; fi
 sleep 2
done
test "$PUBLIC_MATCH" -eq 1
python3 - <<'PYFINAL'
import hashlib,json,os,pathlib,subprocess,time
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text());restart={'pyradar','pyradar-worker','pyradar-projection'}
for name,row in m['files'].items():assert hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()==row['after'],f'Published source mismatch: {name}'
for name,digest in m['static'].items():assert hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()==digest,f'Published static mismatch: {name}'
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True));actual={r['name']:r for r in rows if r.get('name') in m['services']}
assert set(actual)==set(m['services']) and all(r['pid']>0 and r['pm2_env']['status']=='online' for r in actual.values())
for name,before in m['services'].items():
 if name in restart:assert actual[name]['pid']!=before['pid'],f'Service restart not observed: {name}'
 else:assert actual[name]['pid']==before['pid'],f'Unrelated service restarted: {name}'
safe={name:{'pid':row['pid'],'status':row['pm2_env']['status']} for name,row in actual.items()}
print(json.dumps({'at':int(time.time()*1000),'indexAfter':m['indexAfter'],'services':safe,'backup':os.environ['BACKUP']},ensure_ascii=False))
PYFINAL
COMPLETE=1
printf 'Stock/Meme quote release live; only pyradar, worker and projection restarted; rollback: %s\n' "$BACKUP"
REMOTE
