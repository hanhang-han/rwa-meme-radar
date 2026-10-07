#!/usr/bin/env bash
# Publish the reviewed directory API and dashboard without restarting collectors.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
BASELINE="${1:?Pass the production source baseline JSON}"
FILES="${2:?Pass the server source whitelist JSON}"
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o IdentitiesOnly=yes -i "${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}")
PACKAGE=$(mktemp /tmp/cliperx-meme-release.XXXXXX.tar.gz)
MANIFEST=$(mktemp /tmp/cliperx-meme-manifest.XXXXXX.json)
trap 'rm -f "$PACKAGE" "$MANIFEST"' EXIT
python3 - "$BASELINE" "$FILES" "$MANIFEST" "$PACKAGE" <<'PY'
import hashlib,json,pathlib,sys,tarfile
baseline=json.loads(pathlib.Path(sys.argv[1]).read_text())
paths=json.loads(pathlib.Path(sys.argv[2]).read_text())
assert paths and len(paths)==len(set(paths))
for name in paths:
 p=pathlib.Path(name)
 assert name.startswith('server-py/app/') and p.suffix=='.py' and p.is_file()
 assert name in baseline, f'Missing explicit baseline for {name}'
index=pathlib.Path('web/dist/index.html')
assert index.is_file() and '/dashboard/assets/' in index.read_text()
manifest={'files':{p:{'before':baseline[p],'after':hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()} for p in paths},
 'index':hashlib.sha256(index.read_bytes()).hexdigest()}
pathlib.Path(sys.argv[3]).write_text(json.dumps(manifest))
with tarfile.open(sys.argv[4],'w:gz') as out:
 for p in paths:out.add(p,arcname=p)
 out.add('web/dist',arcname='web/dist')
 out.add(sys.argv[3],arcname='release.json')
print('Reviewed server files:',len(paths))
PY
REMOTE_PACKAGE="/tmp/$(basename "$PACKAGE")"
ssh "${SSH_OPTS[@]}" "$SERVER" "cat > $REMOTE_PACKAGE" < "$PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$REMOTE_PACKAGE" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
mkdir -p .releases
exec 9>.releases/meme-pagination-deploy.lock
flock -n 9 || { printf 'Another Meme release is running\n' >&2; exit 1; }
STAGING=$(mktemp -d .releases/meme-pagination-staging.XXXXXX)
BACKUP=".releases/meme-pagination-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
export STAGING BACKUP
CHANGED=0
WEB_MOVED=0
finish() {
 result=$?
 trap - EXIT
 if [ "$result" -ne 0 ] && [ "$CHANGED" -eq 1 ]; then
  set +e
  ROLLBACK_OK=1
  python3 - <<'PY'
import json,os,pathlib,shutil
b=pathlib.Path(os.environ['BACKUP']);m=json.loads((b/'release.json').read_text())
errors=[]
for name,row in m['files'].items():
 try:
  p=pathlib.Path(name)
  if row['before'] is None:p.unlink(missing_ok=True)
  else:shutil.copy2(b/p,p)
 except Exception as error:errors.append(f'{name}: {error}')
if errors:raise RuntimeError('; '.join(errors))
PY
  if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  if [ "$WEB_MOVED" -eq 1 ]; then
   cp -an web/dist/assets/. "$BACKUP/web-dist/assets/"
   if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
   python3 - <<'PY'
import ctypes,os
libc=ctypes.CDLL(None,use_errno=True)
result=libc.renameat2(-100,b'web/dist',-100,os.fsencode(os.environ['BACKUP']+'/web-dist'),2)
if result:raise OSError(ctypes.get_errno(),'Atomic dashboard restore failed')
PY
   if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  fi
  pm2 restart pyradar >/dev/null
  if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  API_READY=0
  for attempt in {1..30}; do
   if curl -fsS --max-time 3 http://127.0.0.1:8010/api/health/ready -o /dev/null; then API_READY=1; break; fi
   sleep 2
  done
  if [ "$API_READY" -ne 1 ]; then ROLLBACK_OK=0; fi
  python3 - <<'PY'
import hashlib,json,os,pathlib
b=pathlib.Path(os.environ['BACKUP']);m=json.loads((b/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name);actual=hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
 assert actual==row['before'],f'Restore mismatch: {name}'
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==(b/'previous-index.sha256').read_text()
PY
  if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  if [ "$ROLLBACK_OK" -eq 1 ]; then
   printf 'Release failed; API source, dashboard and readiness restored. Backup: %s\n' "$BACKUP" >&2
  else
   printf 'Release failed; rollback incomplete. Inspect backup: %s\n' "$BACKUP" >&2
  fi
 fi
 exit "$result"
}
trap finish EXIT
tar xzf "$1" -C "$STAGING"
mkdir -p "$STAGING/validation/server-py"
cp -a server-py/app "$STAGING/validation/server-py/"
python3 - <<'PY'
import ctypes,hashlib,json,os,pathlib,shutil
s=pathlib.Path(os.environ['STAGING']);b=pathlib.Path(os.environ['BACKUP'])
m=json.loads((s/'release.json').read_text())
for name,row in m['files'].items():
 p=pathlib.Path(name);actual=hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
 assert actual==row['before'],f'Production file changed: {name}'
 assert hashlib.sha256((s/name).read_bytes()).hexdigest()==row['after']
 if p.exists():
  target=b/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
 target=s/'validation'/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(s/p,target)
assert hashlib.sha256((s/'web/dist/index.html').read_bytes()).hexdigest()==m['index']
shutil.copy2(s/'release.json',b/'release.json')
assert hasattr(ctypes.CDLL(None),'renameat2'),'Atomic directory exchange unavailable'
(b/'previous-index.sha256').write_text(hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest())
shutil.copytree('web/dist',b/'web-dist')
PY
PYTHONPATH="$STAGING/validation/server-py" server-py/.venv/bin/python - <<'PY'
from app.api import product_v2,stream
assert any(r.path=='/memes' for r in product_v2.router.routes)
print('Staged paged directory imports passed')
PY
CHANGED=1
pm2 stop pyradar >/dev/null
python3 - <<'PY'
import json,os,pathlib,shutil
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for name in m['files']:
 p=pathlib.Path(name);p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(s/p,p)
PY
pm2 restart pyradar >/dev/null
for attempt in {1..60}; do
 if curl -fsS --max-time 3 http://127.0.0.1:8010/api/health/ready -o /dev/null; then break; fi
 if [ "$attempt" -eq 60 ]; then false; fi
 sleep 2
done
curl --compressed -fsS --max-time 45 'http://127.0.0.1:8010/api/v2/memes?chain=all&limit=20&offset=0' -o "$STAGING/memes.json"
python3 - <<'PY'
import json,os,pathlib
p=pathlib.Path(os.environ['STAGING'])/'memes.json';d=json.loads(p.read_text())
assert len(d['groups'])<=20 and d['directory']['catalogTotal']>=d['directory']['filteredTotal']
assert not d.get('unified',{}).get('stockTokens')
assert p.stat().st_size<500_000,'Paged response exceeds the reviewed delivery bound'
print('Live directory groups:',len(d['groups']),'JSON bytes:',p.stat().st_size)
PY
if [ -d web/dist/assets ]; then cp -an web/dist/assets/. "$STAGING/web/dist/assets/"; fi
python3 - <<'PY'
import ctypes,os
libc=ctypes.CDLL(None,use_errno=True)
result=libc.renameat2(-100,b'web/dist',-100,os.fsencode(os.environ['STAGING']+'/web/dist'),2)
if result:raise OSError(ctypes.get_errno(),'Atomic dashboard activation failed')
PY
WEB_MOVED=1
for attempt in {1..5}; do
 if curl --compressed -fsS --max-time 20 https://cliperx.com/dashboard/ -o "$STAGING/public-index.html" &&
    python3 - <<'PY'
import hashlib,json,os,pathlib
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
assert hashlib.sha256((s/'public-index.html').read_bytes()).hexdigest()==m['index']
PY
 then break; fi
 if [ "$attempt" -eq 5 ]; then false; fi
 sleep 2
done
python3 - <<'PY'
import hashlib,json,os,pathlib
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for name,row in m['files'].items():assert hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()==row['after']
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['index']
PY
trap - EXIT
printf 'Meme pagination live; rollback directory: %s\n' "$BACKUP"
REMOTE
printf 'Published: https://cliperx.com/dashboard/#/meme?chain=all\n'
