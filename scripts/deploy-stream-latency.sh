#!/usr/bin/env bash
# Deploy the stream/checkpoint latency fix with exact-source guards and rollback.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
BASE_CHAIN_SHA=5dc083e841a4635642c4ab46fe2517f3e210b33d8d9f61300afc85963d558f9f
BASE_DB_SHA=7dfbee8f37dc11b1565fa18e6e53115cf978185d0599764820d9ce1ef32a6a37
NEW_CHAIN_SHA=$(shasum -a 256 server-py/app/collectors/chain_stream.py | cut -d' ' -f1)
NEW_DB_SHA=$(shasum -a 256 server-py/app/db.py | cut -d' ' -f1)
scp "${SSH_OPTS[@]}" server-py/app/collectors/chain_stream.py "$SERVER:/tmp/cliperx-stream-latency-chain.py"
scp "${SSH_OPTS[@]}" server-py/app/db.py "$SERVER:/tmp/cliperx-stream-latency-db.py"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$BASE_CHAIN_SHA" "$BASE_DB_SHA" "$NEW_CHAIN_SHA" "$NEW_DB_SHA" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
chain_target=server-py/app/collectors/chain_stream.py
db_target=server-py/app/db.py
test "$(sha256sum "$chain_target" | cut -d' ' -f1)" = "$1"
test "$(sha256sum "$db_target" | cut -d' ' -f1)" = "$2"
test "$(sha256sum /tmp/cliperx-stream-latency-chain.py | cut -d' ' -f1)" = "$3"
test "$(sha256sum /tmp/cliperx-stream-latency-db.py | cut -d' ' -f1)" = "$4"
server-py/.venv/bin/python -m py_compile /tmp/cliperx-stream-latency-chain.py /tmp/cliperx-stream-latency-db.py
backup_dir=".releases/stream-latency-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$backup_dir"
cp -p "$chain_target" "$db_target" "$backup_dir/"
changed=0
done=0
finish() {
  result=$?
  trap - EXIT
  if [ "$done" -ne 1 ] && [ "$changed" -eq 1 ]; then
    pm2 stop pyradar-worker >/dev/null 2>&1 || true
    cp -p "$backup_dir/chain_stream.py" "$chain_target"
    cp -p "$backup_dir/db.py" "$db_target"
    pm2 restart pyradar-worker >/dev/null 2>&1 || true
    printf 'Stream latency rollback: %s\n' "$backup_dir" >&2
  fi
  exit "$result"
}
trap finish EXIT
old_pid=$(pm2 pid pyradar-worker)
pm2 stop pyradar-worker >/dev/null
changed=1
cp -p /tmp/cliperx-stream-latency-chain.py "$chain_target"
cp -p /tmp/cliperx-stream-latency-db.py "$db_target"
pm2 restart pyradar-worker >/dev/null
for attempt in {1..36}; do
  new_pid=$(pm2 pid pyradar-worker)
  if [ "${new_pid:-0}" -gt 0 ] && [ "$new_pid" != "$old_pid" ] &&
     curl -fsS --max-time 8 http://127.0.0.1:8010/api/health/data -o /tmp/cliperx-stream-latency-health.json &&
     python3 - /tmp/cliperx-stream-latency-health.json "$new_pid" <<'PY'
import json,sys
h=json.load(open(sys.argv[1])); c=h.get('collector') or {}
assert c.get('pid')==int(sys.argv[2]) and c.get('updatedAt')
PY
  then
    done=1
    pm2 save >/dev/null
    printf 'Stream latency fix deployed; code rollback: %s\n' "$backup_dir"
    exit 0
  fi
  sleep 5
done
false
REMOTE
