#!/usr/bin/env bash
# One-module API hotfix; do not restart collectors, the old API or Nginx.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
FROZEN="${1:?Pass the reviewed production API source SHA manifest}"
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o IdentitiesOnly=yes -i "${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}")
PACKAGE=$(mktemp /tmp/cliperx-market-api-hotfix.XXXXXX.tar.gz)
MANIFEST=$(mktemp /tmp/cliperx-market-api-hotfix.XXXXXX.json)
trap 'rm -f "$PACKAGE" "$MANIFEST"' EXIT
python3 - "$FROZEN" "$MANIFEST" "$PACKAGE" <<'PY'
import hashlib,json,pathlib,sys,tarfile
name='server-py/app/api/live_market.py';p=pathlib.Path(name)
frozen=json.loads(pathlib.Path(sys.argv[1]).read_text());before=frozen['sha256'][name]
assert len(before)==64 and all(c in '0123456789abcdef' for c in before)
assert p.is_file() and not p.is_symlink()
after=hashlib.sha256(p.read_bytes()).hexdigest();assert before!=after,'Hotfix source unchanged'
manifest={'path':name,'before':before,'after':after,'service':'pyradar-market-api'}
pathlib.Path(sys.argv[2]).write_text(json.dumps(manifest))
with tarfile.open(sys.argv[3],'w:gz') as out:
 out.add(p,arcname=name,recursive=False);out.add(sys.argv[2],arcname='release.json')
print('Reviewed one-file market API hotfix prepared')
PY
REMOTE_PACKAGE="/tmp/$(basename "$PACKAGE")"
ssh "${SSH_OPTS[@]}" "$SERVER" "cat > $REMOTE_PACKAGE" < "$PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$REMOTE_PACKAGE" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
exec 9>.releases/live-market-api-hotfix.lock
flock -n 9 || { printf 'Another market API hotfix is running\n' >&2; exit 1; }
STAGING=$(mktemp -d .releases/live-market-api-hotfix-staging.XXXXXX)
BACKUP=".releases/live-market-api-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
export STAGING BACKUP
CHANGED=0
COMPLETE=0
OLD_PID=$(pm2 pid pyradar-market-api)
test "${OLD_PID:-0}" -gt 0
export OLD_PID
pm2 jlist | python3 -c 'import json,os,pathlib,sys; names={"pyradar","pyradar-worker","pyradar-projection","pyradar-market","memedashboard"}; rows=json.load(sys.stdin);before={p["name"]:p["pid"] for p in rows if p.get("name") in names};assert set(before)==names;pathlib.Path(os.environ["BACKUP"],"unrelated-pids.json").write_text(json.dumps(before))'
finish() {
 result=$?
 trap - EXIT
 if [ "$CHANGED" -eq 1 ] && [ "$COMPLETE" -ne 1 ]; then
  set +e
  ROLLBACK_OK=1
  pm2 stop pyradar-market-api >/dev/null
  if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  python3 - <<'PY'
import hashlib,json,os,pathlib,shutil,tempfile
b=pathlib.Path(os.environ['BACKUP']);m=json.loads((b/'release.json').read_text());p=pathlib.Path(m['path'])
with tempfile.NamedTemporaryFile(dir=p.parent,delete=False) as f:tmp=pathlib.Path(f.name)
shutil.copy2(b/p,tmp);os.replace(tmp,p)
assert hashlib.sha256(p.read_bytes()).hexdigest()==m['before']
PY
  if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  pm2 restart pyradar-market-api >/dev/null
  if [ "$?" -ne 0 ]; then ROLLBACK_OK=0; fi
  READY=0
  for attempt in {1..20}; do
   if curl -fsS --max-time 5 http://127.0.0.1:8011/api/health/ready -o /dev/null; then READY=1; break; fi
   sleep 2
  done
  if [ "$READY" -ne 1 ]; then ROLLBACK_OK=0; fi
  if [ "$ROLLBACK_OK" -eq 1 ]; then printf 'Market API hotfix failed; prior source/API restored. Backup: %s\n' "$BACKUP" >&2;
  else printf 'Market API hotfix failed; rollback needs inspection: %s\n' "$BACKUP" >&2; fi
 fi
 rm -f "$REMOTE_PACKAGE" 2>/dev/null || true
 exit "$result"
}
REMOTE_PACKAGE="$1"
trap finish EXIT
python3 - "$REMOTE_PACKAGE" <<'PY'
import hashlib,json,os,pathlib,shutil,sys,tarfile
s=pathlib.Path(os.environ['STAGING']);b=pathlib.Path(os.environ['BACKUP'])
with tarfile.open(sys.argv[1]) as archive:
 m=json.load(archive.extractfile('release.json'))
 assert m['path']=='server-py/app/api/live_market.py' and m['service']=='pyradar-market-api'
 assert {item.name for item in archive.getmembers()}=={m['path'],'release.json'}
 assert all(item.isfile() for item in archive.getmembers())
 archive.extractall(s)
p=pathlib.Path(m['path']);assert hashlib.sha256(p.read_bytes()).hexdigest()==m['before'],'Production API source changed'
assert hashlib.sha256((s/p).read_bytes()).hexdigest()==m['after']
target=b/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target);shutil.copy2(s/'release.json',b/'release.json')
PY
server-py/.venv/bin/python -m py_compile "$STAGING/server-py/app/api/live_market.py"
curl -fsS --max-time 5 http://127.0.0.1:8011/api/live-market/status -o "$STAGING/before-status.json"
# Recheck after preflight, immediately before the single service is stopped.
python3 - <<'PY'
import hashlib,json,os,pathlib
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text())
assert hashlib.sha256(pathlib.Path(m['path']).read_bytes()).hexdigest()==m['before']
PY
CHANGED=1
pm2 stop pyradar-market-api >/dev/null
python3 - <<'PY'
import json,os,pathlib,shutil,tempfile
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text());p=pathlib.Path(m['path'])
with tempfile.NamedTemporaryFile(dir=p.parent,delete=False) as f:tmp=pathlib.Path(f.name)
shutil.copy2(s/p,tmp);os.replace(tmp,p)
PY
pm2 restart pyradar-market-api >/dev/null
READY=0
for attempt in {1..20}; do
 if curl -fsS --max-time 5 http://127.0.0.1:8011/api/health/ready -o /dev/null; then READY=1; break; fi
 sleep 2
done
test "$READY" -eq 1
NEW_PID=$(pm2 pid pyradar-market-api)
test "${NEW_PID:-0}" -gt 0
test "$NEW_PID" != "$OLD_PID"
curl -fsS --max-time 5 http://127.0.0.1:8011/api/live-market/status -o "$STAGING/after-status.json"
curl --compressed -fsS --max-time 10 https://cliperx.com/dashboard/api/live-market/status -o "$STAGING/public-status.json"
pm2 jlist | python3 -c 'import json,os,pathlib,sys;before=json.loads(pathlib.Path(os.environ["BACKUP"],"unrelated-pids.json").read_text());after={p["name"]:p["pid"] for p in json.load(sys.stdin) if p.get("name") in before};assert before==after,"Unrelated service PID changed"'
python3 - <<'PY'
import hashlib,json,os,pathlib
s=pathlib.Path(os.environ['STAGING']);m=json.loads((s/'release.json').read_text());assert hashlib.sha256(pathlib.Path(m['path']).read_bytes()).hexdigest()==m['after']
def identity(name):
 body=json.loads((s/name).read_text());assert body.get('enabled') is True and body.get('liveMarket') is True
 assert {r.get('chainId') for r in body.get('chains',[])}=={'196','56','4663'}
 return {r['chainId']:r['epoch'] for r in body['chains']}
assert identity('before-status.json')==identity('after-status.json')==identity('public-status.json'),'Journal identity changed across API-only hotfix'
print('API-only hotfix source, public routing and journal identities verified')
PY
COMPLETE=1
printf 'Market API hotfix live; only market API restarted; rollback: %s\n' "$BACKUP"
REMOTE
