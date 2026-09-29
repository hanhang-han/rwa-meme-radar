#!/usr/bin/env bash
# Deploy a verified collector update with checksum guard and automatic rollback.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
BASE_SHA=423abbebd867eadfb8b51ce973cc592e570ac535459b5b880dd1d663bacc649f
BASE_ALPHA_SHA=8685e17edc7b030a0eec64d780c47e54bd2ecec3d5b2c70bd4deeac5444c50e5
NEW_SHA=$(shasum -a 256 server-py/app/collectors/chain_stream.py | cut -d' ' -f1)
NEW_ALPHA_SHA=$(shasum -a 256 server-py/app/collectors/binance_alpha.py | cut -d' ' -f1)
scp "${SSH_OPTS[@]}" server-py/app/collectors/chain_stream.py "$SERVER:/tmp/cliperx-chain-throughput.py"
scp "${SSH_OPTS[@]}" server-py/app/collectors/binance_alpha.py "$SERVER:/tmp/cliperx-alpha-directory.py"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$BASE_SHA" "$NEW_SHA" "$BASE_ALPHA_SHA" "$NEW_ALPHA_SHA" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
old_sha="$1"
new_sha="$2"
old_alpha_sha="$3"
new_alpha_sha="$4"
target=server-py/app/collectors/chain_stream.py
alpha_target=server-py/app/collectors/binance_alpha.py
test "$(sha256sum "$target" | cut -d' ' -f1)" = "$old_sha"
test "$(sha256sum "$alpha_target" | cut -d' ' -f1)" = "$old_alpha_sha"
test "$(sha256sum /tmp/cliperx-chain-throughput.py | cut -d' ' -f1)" = "$new_sha"
test "$(sha256sum /tmp/cliperx-alpha-directory.py | cut -d' ' -f1)" = "$new_alpha_sha"
server-py/.venv/bin/python -m py_compile /tmp/cliperx-chain-throughput.py
server-py/.venv/bin/python -m py_compile /tmp/cliperx-alpha-directory.py
backup=".releases/chain-before-$(date -u +%Y%m%dT%H%M%SZ)-$$.py"
alpha_backup="${backup%.py}-alpha.py"
cp -p "$target" "$backup"
cp -p "$alpha_target" "$alpha_backup"
changed=0
done=0
finish() {
  result=$?
  trap - EXIT
  if [ "$done" -ne 1 ] && [ "$changed" -eq 1 ]; then
    pm2 stop pyradar-worker >/dev/null 2>&1 || true
    cp -p "$backup" "$target"
    cp -p "$alpha_backup" "$alpha_target"
    pm2 restart pyradar-worker >/dev/null 2>&1 || true
    printf 'Live collector rollback: %s %s\n' "$backup" "$alpha_backup" >&2
  fi
  exit "$result"
}
trap finish EXIT
old_pid=$(pm2 pid pyradar-worker)
pm2 stop pyradar-worker >/dev/null
changed=1
cp -p /tmp/cliperx-chain-throughput.py "$target"
cp -p /tmp/cliperx-alpha-directory.py "$alpha_target"
pm2 restart pyradar-worker >/dev/null
for attempt in {1..36}; do
  new_pid=$(pm2 pid pyradar-worker)
  if [ "${new_pid:-0}" -gt 0 ] && [ "$new_pid" != "$old_pid" ] &&
     curl -fsS --max-time 8 http://127.0.0.1:8010/api/health/data -o /tmp/cliperx-chain-throughput-health.json &&
     python3 - /tmp/cliperx-chain-throughput-health.json "$new_pid" <<'PY'
import json,sys
h=json.load(open(sys.argv[1])); c=h.get('collector') or {}
assert c.get('pid')==int(sys.argv[2]) and c.get('updatedAt')
PY
  then
    done=1
    pm2 save >/dev/null
    printf 'Live collector fixes deployed; code rollback: %s %s\n' "$backup" "$alpha_backup"
    exit 0
  fi
  sleep 5
done
false
REMOTE
