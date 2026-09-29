#!/usr/bin/env bash
# Deploy the isolated live chain write path with checksum guard and rollback.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
BASE_SHA=acc08f21300fc0c64ca3cf2602fb6d0284817c013afcb08977a3034426ab10ee
NEW_SHA=$(shasum -a 256 server-py/app/collectors/chain_stream.py | cut -d' ' -f1)
scp "${SSH_OPTS[@]}" server-py/app/collectors/chain_stream.py "$SERVER:/tmp/cliperx-chain-write-path.py"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$BASE_SHA" "$NEW_SHA" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
target=server-py/app/collectors/chain_stream.py
test "$(sha256sum "$target" | cut -d' ' -f1)" = "$1"
test "$(sha256sum /tmp/cliperx-chain-write-path.py | cut -d' ' -f1)" = "$2"
server-py/.venv/bin/python -m py_compile /tmp/cliperx-chain-write-path.py
backup=".releases/chain-write-before-$(date -u +%Y%m%dT%H%M%SZ)-$$.py"
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
    printf 'Chain write-path rollback: %s\n' "$backup" >&2
  fi
  exit "$result"
}
trap finish EXIT
old_pid=$(pm2 pid pyradar-worker)
pm2 stop pyradar-worker >/dev/null
changed=1
cp -p /tmp/cliperx-chain-write-path.py "$target"
pm2 restart pyradar-worker >/dev/null
for attempt in {1..36}; do
  new_pid=$(pm2 pid pyradar-worker)
  if [ "${new_pid:-0}" -gt 0 ] && [ "$new_pid" != "$old_pid" ] &&
     curl -fsS --max-time 8 http://127.0.0.1:8010/api/health/data -o /tmp/cliperx-chain-write-health.json &&
     python3 - /tmp/cliperx-chain-write-health.json "$new_pid" <<'PY'
import json,sys
h=json.load(open(sys.argv[1])); c=h.get('collector') or {}
assert c.get('pid')==int(sys.argv[2]) and c.get('updatedAt')
PY
  then
    done=1
    pm2 save >/dev/null
    printf 'Chain write-path fix deployed; code rollback: %s\n' "$backup"
    exit 0
  fi
  sleep 5
done
false
REMOTE
