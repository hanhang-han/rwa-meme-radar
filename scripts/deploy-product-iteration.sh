#!/usr/bin/env bash
# Release only changed application code plus the built dashboard; keep market data intact.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
BASELINE="${1:?Pass the read-only server baseline JSON captured before this release}"
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
python3 - "$BASELINE" "$MANIFEST" "$PACKAGE" <<'PY'
import hashlib,json,pathlib,subprocess,sys,tarfile
baseline=json.load(open(sys.argv[1]))
changed=subprocess.check_output(['git','diff','--name-only','--','server-py/app','web/src']).decode().splitlines()
new=subprocess.check_output(['git','ls-files','--others','--exclude-standard','server-py/app','web/src']).decode().splitlines()
paths=sorted(set(changed+new))
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
CHANGED=0
WEB_MOVED=0
COMPLETE=0
finish() {
  result=$?
  trap - EXIT
  if [ "$CHANGED" -eq 1 ] && [ "$COMPLETE" -eq 0 ]; then
    pm2 stop pyradar-worker pyradar-projection pyradar >/dev/null 2>&1 || true
    python3 - <<'PY'
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
    pm2 restart pyradar >/dev/null 2>&1 || true
    for attempt in {1..120}; do
      if curl -fsS --max-time 3 http://127.0.0.1:8010/api/health/ready -o /dev/null 2>/dev/null; then break; fi
      sleep 3
    done
    pm2 restart pyradar-projection >/dev/null 2>&1 || true
    pm2 restart pyradar-worker >/dev/null 2>&1 || true
    printf 'Release rolled back: %s\n' "$BACKUP" >&2
  fi
  exit "$result"
}
trap finish EXIT
CHANGED=1
pm2 stop pyradar-worker pyradar-projection pyradar >/dev/null
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
     python3 - "$STAGING/overview.json" <<'PY'
import json,pathlib,sys
p=pathlib.Path(sys.argv[1]);d=json.loads(p.read_text());u=d['unified']
assert p.stat().st_size<=50000 and u['snapshotScope']=='overview'
assert 'newAssets24h' in u['metrics'] and 'hotStocks' in u
print('Overview bytes:',p.stat().st_size)
PY
  then break; fi
  if [ "$attempt" -eq 24 ]; then false; fi
  sleep 3
done
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
pm2 save >/dev/null
printf 'Product iteration deployed; rollback: %s\n' "$BACKUP"
REMOTE
