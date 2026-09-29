#!/usr/bin/env bash
# Deploy dedicated writer connections with checksum guards and automatic rollback.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
BASE_CHAIN_SHA=8970a787af4dd94da35829d47e7dcbec25780886f5eb994b57b6007833e4aff5
BASE_FACTORY_SHA=fe8965dc60839e0e1dcb190f5cf440573bb81fe82b32b45453443cc6b987cfe0
NEW_CHAIN_SHA=$(shasum -a 256 server-py/app/collectors/chain_stream.py | cut -d' ' -f1)
NEW_FACTORY_SHA=$(shasum -a 256 server-py/app/collectors/factory_discovery.py | cut -d' ' -f1)
scp "${SSH_OPTS[@]}" server-py/app/collectors/chain_stream.py "$SERVER:/tmp/cliperx-chain-writer.py"
scp "${SSH_OPTS[@]}" server-py/app/collectors/factory_discovery.py "$SERVER:/tmp/cliperx-factory-writer.py"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$BASE_CHAIN_SHA" "$NEW_CHAIN_SHA" "$BASE_FACTORY_SHA" "$NEW_FACTORY_SHA" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
chain=server-py/app/collectors/chain_stream.py
factory=server-py/app/collectors/factory_discovery.py
test "$(sha256sum "$chain" | cut -d' ' -f1)" = "$1"
test "$(sha256sum /tmp/cliperx-chain-writer.py | cut -d' ' -f1)" = "$2"
test "$(sha256sum "$factory" | cut -d' ' -f1)" = "$3"
test "$(sha256sum /tmp/cliperx-factory-writer.py | cut -d' ' -f1)" = "$4"
server-py/.venv/bin/python -m py_compile /tmp/cliperx-chain-writer.py /tmp/cliperx-factory-writer.py
backup=".releases/writer-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
cp -p "$chain" "$backup-chain.py"
cp -p "$factory" "$backup-factory.py"
changed=0
done=0
finish() {
  result=$?
  trap - EXIT
  if [ "$done" -ne 1 ] && [ "$changed" -eq 1 ]; then
    pm2 stop pyradar-worker >/dev/null 2>&1 || true
    cp -p "$backup-chain.py" "$chain"
    cp -p "$backup-factory.py" "$factory"
    pm2 restart pyradar-worker >/dev/null 2>&1 || true
    printf 'Writer fix rollback: %s\n' "$backup" >&2
  fi
  exit "$result"
}
trap finish EXIT
old_pid=$(pm2 pid pyradar-worker)
pm2 stop pyradar-worker >/dev/null
changed=1
cp -p /tmp/cliperx-chain-writer.py "$chain"
cp -p /tmp/cliperx-factory-writer.py "$factory"
pm2 restart pyradar-worker >/dev/null
for attempt in {1..36}; do
  new_pid=$(pm2 pid pyradar-worker)
  if [ "${new_pid:-0}" -gt 0 ] && [ "$new_pid" != "$old_pid" ] &&
     curl -fsS --max-time 8 http://127.0.0.1:8010/api/health/data -o /tmp/cliperx-writer-health.json &&
     python3 - /tmp/cliperx-writer-health.json "$new_pid" <<'PY'
import json,sys
h=json.load(open(sys.argv[1])); c=h.get('collector') or {}
assert c.get('pid')==int(sys.argv[2]) and c.get('updatedAt')
PY
  then
    done=1
    pm2 save >/dev/null
    printf 'Writer fixes deployed; code rollback: %s\n' "$backup"
    exit 0
  fi
  sleep 5
done
false
REMOTE
