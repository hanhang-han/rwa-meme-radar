#!/usr/bin/env bash
# Publish derived-work cooldown and less frequent full comparison scans.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
BASE_SHA=a2aa75e725e8a17119e481faf1904dc5c46bd1aa29d91550e60da8889a8de95b
NEW_SHA=$(shasum -a 256 server-py/app/projection_worker.py | cut -d' ' -f1)
scp "${SSH_OPTS[@]}" server-py/app/projection_worker.py "$SERVER:/tmp/cliperx-projection-cadence.py"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$BASE_SHA" "$NEW_SHA" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
old_sha="$1"
new_sha="$2"
target=server-py/app/projection_worker.py
test "$(sha256sum "$target" | cut -d' ' -f1)" = "$old_sha"
test "$(sha256sum /tmp/cliperx-projection-cadence.py | cut -d' ' -f1)" = "$new_sha"
server-py/.venv/bin/python -m py_compile /tmp/cliperx-projection-cadence.py
backup=".releases/projection-cadence-before-$(date -u +%Y%m%dT%H%M%SZ)-$$.py"
cp -p "$target" "$backup"
changed=0
done=0
finish() {
  result=$?
  trap - EXIT
  if [ "$done" -ne 1 ] && [ "$changed" -eq 1 ]; then
    pm2 stop pyradar-projection >/dev/null 2>&1 || true
    cp -p "$backup" "$target"
    pm2 restart pyradar-projection >/dev/null 2>&1 || true
    printf 'Projection cadence rollback: %s\n' "$backup" >&2
  fi
  exit "$result"
}
trap finish EXIT
old_pid=$(pm2 pid pyradar-projection)
pm2 stop pyradar-projection >/dev/null
changed=1
cp -p /tmp/cliperx-projection-cadence.py "$target"
pm2 restart pyradar-projection >/dev/null
for attempt in {1..36}; do
  new_pid=$(pm2 pid pyradar-projection)
  if [ "${new_pid:-0}" -gt 0 ] && [ "$new_pid" != "$old_pid" ] &&
     curl -fsS --max-time 8 http://127.0.0.1:8010/api/health/data -o /tmp/cliperx-projection-cadence-health.json &&
     python3 - /tmp/cliperx-projection-cadence-health.json "$new_pid" <<'PY'
import json,sys
h=json.load(open(sys.argv[1])); p=h.get('projection') or {}
assert p.get('pid')==int(sys.argv[2]) and p.get('updatedAt')
PY
  then
    done=1
    pm2 save >/dev/null
    printf 'Projection cadence live; code rollback: %s\n' "$backup"
    exit 0
  fi
  sleep 5
done
false
REMOTE
