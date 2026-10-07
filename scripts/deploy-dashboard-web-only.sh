#!/usr/bin/env bash
# Static dashboard only. Never restart a service or change Nginx/backend.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
BASELINE="${1:?Pass the current dashboard-only production baseline JSON}"
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o IdentitiesOnly=yes -i "${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}")
PACKAGE=$(mktemp /tmp/cliperx-dashboard-only.XXXXXX.tar.gz)
MANIFEST=$(mktemp /tmp/cliperx-dashboard-only.XXXXXX.json)
trap 'rm -f "$PACKAGE" "$MANIFEST"' EXIT
python3 - "$BASELINE" "$MANIFEST" "$PACKAGE" <<'PY'
import hashlib,json,pathlib,sys,tarfile
baseline=json.loads(pathlib.Path(sys.argv[1]).read_text())
before=baseline['indexBefore'];assert len(before)==64 and all(c in '0123456789abcdef' for c in before)
names={'memedashboard','pyradar','pyradar-worker','pyradar-projection','pyradar-market','pyradar-market-api'}
assert set(baseline['services'])==names
root=pathlib.Path('web/dist');index=root/'index.html'
assert index.is_file() and not index.is_symlink() and '/dashboard/assets/' in index.read_text(),'Build with /dashboard/ base first'
files={}
for p in sorted(root.rglob('*')):
 assert not p.is_symlink(),'Symlink in static release'
 if p.is_file():files[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
assert files and files[str(index)]!=before,'Dashboard index unchanged'
manifest={'indexBefore':before,'indexAfter':files[str(index)],'files':files,
 'services':baseline['services']}
pathlib.Path(sys.argv[2]).write_text(json.dumps(manifest))
with tarfile.open(sys.argv[3],'w:gz') as out:
 out.add(root,arcname='web/dist');out.add(sys.argv[2],arcname='release.json')
print('Reviewed static release:',len(files),'files; backend and Nginx excluded')
PY
REMOTE_PACKAGE="/tmp/$(basename "$PACKAGE")"
ssh "${SSH_OPTS[@]}" "$SERVER" "cat > $REMOTE_PACKAGE" < "$PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$REMOTE_PACKAGE" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
exec 9>.releases/dashboard-web-only-deploy.lock
flock -n 9 || { printf 'Another static dashboard release is running\n' >&2; exit 1; }
STAGING=$(mktemp -d .releases/dashboard-web-only-staging.XXXXXX)
BACKUP=".releases/dashboard-web-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
export STAGING BACKUP
MOVED=0
COMPLETE=0
finish() {
 result=$?
 trap - EXIT
 if [ "$MOVED" -eq 1 ] && [ "$COMPLETE" -ne 1 ]; then
  set +e
  ROLLBACK_OK=1
  # Cached new HTML remains able to fetch its assets after the old index is
  # restored. Keep immutable asset files from both release generations.
  cp -an web/dist/assets/. "$BACKUP/web-dist/assets/"
  if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  python3 - <<'PY'
import ctypes,hashlib,json,os,pathlib
b=pathlib.Path(os.environ['BACKUP']);m=json.loads((b/'release.json').read_text());libc=ctypes.CDLL(None,use_errno=True)
if libc.renameat2(-100,b'web/dist',-100,os.fsencode(str(b/'web-dist')),2):raise OSError(ctypes.get_errno(),'Static dashboard restore failed')
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore']
PY
  if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  if [ "$ROLLBACK_OK" -eq 1 ]; then printf 'Static release failed; old index/assets restored. Backup: %s\n' "$BACKUP" >&2;
  else printf 'Static release failed; rollback needs inspection: %s\n' "$BACKUP" >&2; fi
 fi
 rm -f "$REMOTE_PACKAGE" 2>/dev/null || true
 exit "$result"
}
REMOTE_PACKAGE="$1"
trap finish EXIT
python3 - "$REMOTE_PACKAGE" <<'PY'
import ctypes,hashlib,json,os,pathlib,shutil,subprocess,sys,tarfile
s=pathlib.Path(os.environ['STAGING']);b=pathlib.Path(os.environ['BACKUP'])
with tarfile.open(sys.argv[1]) as archive:
 m=json.load(archive.extractfile('release.json'))
 for item in archive.getmembers():
  p=pathlib.PurePosixPath(item.name)
  assert not p.is_absolute() and '..' not in p.parts and (item.isfile() or item.isdir()),'Unsafe static package member'
  assert item.name=='release.json' or item.name=='web/dist' or item.name.startswith('web/dist/'),'Backend/config in static package'
 archive.extractall(s)
assert hasattr(ctypes.CDLL(None),'renameat2'),'Atomic static exchange unavailable'
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore'],'Production index changed'
for name,digest in m['files'].items():
 assert name.startswith('web/dist/') and pathlib.PurePosixPath(name).parts[:2]==('web','dist') and '..' not in pathlib.PurePosixPath(name).parts
 assert hashlib.sha256((s/name).read_bytes()).hexdigest()==digest,f'Staged static hash mismatch: {name}'
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True));expected={n:r['pid'] for n,r in m['services'].items()}
actual={r['name']:r['pid'] for r in rows if r.get('name') in expected};assert actual==expected,'Service PID changed after baseline'
shutil.copy2(s/'release.json',b/'release.json');shutil.copytree('web/dist',b/'web-dist')
# Detect a collision before carrying forward previously cached assets.
for p in pathlib.Path('web/dist/assets').rglob('*'):
 if not p.is_file():continue
 target=s/'web/dist/assets'/p.relative_to('web/dist/assets')
 if target.exists():assert p.read_bytes()==target.read_bytes(),f'Immutable asset collision: {p.name}'
 else:target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
print('Static package, prior index and six service PIDs verified')
PY
# Check immediately before the atomic directory exchange.
python3 - <<'PY'
import hashlib,json,os,pathlib,subprocess
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexBefore']
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True));expected={n:r['pid'] for n,r in m['services'].items()};assert {r['name']:r['pid'] for r in rows if r.get('name') in expected}==expected
PY
python3 - <<'PY'
import ctypes,os
libc=ctypes.CDLL(None,use_errno=True)
if libc.renameat2(-100,b'web/dist',-100,os.fsencode(os.environ['STAGING']+'/web/dist'),2):raise OSError(ctypes.get_errno(),'Static dashboard exchange failed')
PY
MOVED=1
PUBLIC_MATCH=0
for attempt in {1..5}; do
 if curl --compressed -fsS --max-time 20 https://cliperx.com/dashboard/ -o "$STAGING/public-index.html" &&
    python3 - <<'PY'
import hashlib,json,os,pathlib
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
assert hashlib.sha256((s/'public-index.html').read_bytes()).hexdigest()==m['indexAfter']
PY
 then PUBLIC_MATCH=1; break; fi
 sleep 2
done
test "$PUBLIC_MATCH" -eq 1
python3 - <<'PY'
import hashlib,json,os,pathlib,subprocess
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
for name,digest in m['files'].items():assert hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()==digest,f'Published static hash mismatch: {name}'
assert hashlib.sha256(pathlib.Path('web/dist/index.html').read_bytes()).hexdigest()==m['indexAfter']
rows=json.loads(subprocess.check_output(['pm2','jlist'],text=True));expected={n:r['pid'] for n,r in m['services'].items()}
assert {r['name']:r['pid'] for r in rows if r.get('name') in expected}==expected,'A service PID changed during static release'
print('Public index, all static file hashes and six unchanged service PIDs verified')
PY
COMPLETE=1
printf 'Dashboard static release live; no service restart/config changes; rollback: %s\n' "$BACKUP"
REMOTE
