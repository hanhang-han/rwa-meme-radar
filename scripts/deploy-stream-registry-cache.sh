#!/usr/bin/env bash
# Deploy cached market registry writes and transaction timing telemetry.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
BASE_SHA=6cdc5c30a2ee32ec8187fae767581b286b76c6ca08abe8119b88feccb2ac864f
NEW_SHA=$(shasum -a 256 server-py/app/collectors/chain_stream.py | cut -d' ' -f1)
scp "${SSH_OPTS[@]}" server-py/app/collectors/chain_stream.py "$SERVER:/tmp/cliperx-stream-registry-cache.py"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$BASE_SHA" "$NEW_SHA" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
target=server-py/app/collectors/chain_stream.py
test "$(sha256sum "$target" | cut -d' ' -f1)" = "$1"
test "$(sha256sum /tmp/cliperx-stream-registry-cache.py | cut -d' ' -f1)" = "$2"
server-py/.venv/bin/python -m py_compile /tmp/cliperx-stream-registry-cache.py
backup=".releases/stream-registry-cache-before-$(date -u +%Y%m%dT%H%M%SZ)-$$.py"
cp -p "$target" "$backup"
changed=0
done=0
finish() {
  result=$?
  trap - EXIT
  if [ "$done" -ne 1 ] && [ "$changed" -eq 1 ]; then
    pm2 stop pyradar-worker >/dev/null 2>&1 || true
    cp -p "$backup" "$target"
    pm2 restart pyradar-worker >/dev/null 2>&1 || true
    printf 'Stream telemetry rollback: %s\n' "$backup" >&2
  fi
  exit "$result"
}
trap finish EXIT
old_pid=$(pm2 pid pyradar-worker)
pm2 stop pyradar-worker >/dev/null
changed=1
cp -p /tmp/cliperx-stream-registry-cache.py "$target"
pm2 restart pyradar-worker >/dev/null
for attempt in {1..36}; do
  new_pid=$(pm2 pid pyradar-worker)
  if [ "${new_pid:-0}" -gt 0 ] && [ "$new_pid" != "$old_pid" ] &&
     curl -fsS --max-time 8 http://127.0.0.1:8010/api/health/data -o /tmp/cliperx-stream-registry-cache-health.json &&
     python3 - /tmp/cliperx-stream-registry-cache-health.json "$new_pid" <<'PY'
import json,sys
h=json.load(open(sys.argv[1])); c=h.get('collector') or {}
assert c.get('pid')==int(sys.argv[2]) and c.get('updatedAt')
PY
  then
    done=1
    pm2 save >/dev/null
    printf 'Stream registry cache deployed; code rollback: %s\n' "$backup"
    exit 0
  fi
  sleep 5
done
false
REMOTE
