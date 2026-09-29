#!/usr/bin/env bash
# Publish the live chain-health read without restarting collectors or projection.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
TARGET=server-py/app/main.py
NEW_SHA=$(shasum -a 256 "$TARGET" | cut -d' ' -f1)
scp "${SSH_OPTS[@]}" "$TARGET" "$SERVER:/tmp/cliperx-live-health.py"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$NEW_SHA" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
target=server-py/app/main.py
test "$(sha256sum "$target" | cut -d' ' -f1)" = f54ee5a8f883e664890b97e802950e1c1c554f996add31a0a01fe816c037be4f
test "$(sha256sum /tmp/cliperx-live-health.py | cut -d' ' -f1)" = "$1"
server-py/.venv/bin/python -m py_compile /tmp/cliperx-live-health.py
worker_pid=$(pm2 pid pyradar-worker)
projection_pid=$(pm2 pid pyradar-projection)
backup=".releases/api-health-before-$(date -u +%Y%m%dT%H%M%SZ)-$$.py"
cp -p "$target" "$backup"
changed=0
complete=0
finish() {
  result=$?
  trap - EXIT
  if [ "$changed" -eq 1 ] && [ "$complete" -ne 1 ]; then
    pm2 stop pyradar >/dev/null 2>&1 || true
    cp -p "$backup" "$target"
    pm2 restart pyradar >/dev/null 2>&1 || true
    printf 'Live health API rolled back from %s\n' "$backup" >&2
  fi
  exit "$result"
}
trap finish EXIT
pm2 stop pyradar >/dev/null
changed=1
cp -p /tmp/cliperx-live-health.py "$target"
pm2 restart pyradar >/dev/null
for attempt in {1..36}; do
  if curl -fsS --max-time 8 http://127.0.0.1:8010/api/health/ready -o /dev/null &&
     curl -fsS --max-time 20 http://127.0.0.1:8010/api/health/data -o /tmp/cliperx-live-health.json &&
     server-py/.venv/bin/python - <<'PY'
import json,time
body=json.load(open('/tmp/cliperx-live-health.json'))
rows={str(row.get('chainId')):row for row in body.get('chainStreams',[])}
assert '56' in rows and '196' in rows and '4663' in rows
assert all(isinstance(row.get('sampleAgeMs'),int) for row in rows.values())
assert all(isinstance(row.get('degraded'),bool) for row in rows.values())
assert rows['56']['sampleAgeMs']<120000
print('chain health', body['status'], {chain:row['queueDepth'] for chain,row in rows.items()})
PY
  then
    test "$(pm2 pid pyradar-worker)" = "$worker_pid"
    test "$(pm2 pid pyradar-projection)" = "$projection_pid"
    complete=1
    pm2 save >/dev/null
    printf 'Live chain-health API live; code rollback: %s\n' "$backup"
    exit 0
  fi
  sleep 5
done
false
REMOTE
curl --compressed -fsS --max-time 20 https://cliperx.com/dashboard/api/health/ready -o /dev/null
