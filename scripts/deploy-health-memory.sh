#!/usr/bin/env bash
# Switch health reads to bounded persisted snapshots without touching data.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
PYTHONPATH=server-py server-py/.venv/bin/python -m unittest server-py/tests/test_projection_process.py -q
scp "${SSH_OPTS[@]}" server-py/app/main.py "$SERVER:/tmp/cliperx-health-memory.py"

ssh "${SSH_OPTS[@]}" "$SERVER" bash -s <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
target=server-py/app/main.py
test "$(sha256sum "$target" | cut -d' ' -f1)" = cbbb13ddce30ad6db11ea4d7ebec74dc69c2dcc8f10ba27e1f64dbf696aecc39
server-py/.venv/bin/python -m py_compile /tmp/cliperx-health-memory.py
curl -fsS --max-time 30 http://127.0.0.1:8010/api/health/data -o /tmp/cliperx-health-before-memory.json
backup=".releases/health-memory-before-$(date -u +%Y%m%dT%H%M%SZ)-$$.py"
cp -p "$target" "$backup"
worker_pid=$(pm2 pid pyradar-worker)
projection_pid=$(pm2 pid pyradar-projection)
changed=0
complete=0
finish() {
  result=$?
  trap - EXIT
  if [ "$changed" -eq 1 ] && [ "$complete" -ne 1 ]; then
    pm2 stop pyradar >/dev/null 2>&1 || true
    cp -p "$backup" "$target"
    pm2 restart pyradar >/dev/null 2>&1 || true
    printf 'Health route rolled back from %s\n' "$backup" >&2
  fi
  exit "$result"
}
trap finish EXIT
pm2 stop pyradar >/dev/null
changed=1
cp -p /tmp/cliperx-health-memory.py "$target"
pm2 restart pyradar >/dev/null
for attempt in {1..30}; do
  if curl -fsS --max-time 10 http://127.0.0.1:8010/api/health/ready -o /dev/null &&
     curl -fsS --max-time 30 http://127.0.0.1:8010/api/health/data -o /tmp/cliperx-health-after-memory.json &&
     server-py/.venv/bin/python - <<'PY'
import json
before=json.load(open('/tmp/cliperx-health-before-memory.json'))
after=json.load(open('/tmp/cliperx-health-after-memory.json'))
assert not {'projection-snapshot-unavailable','health-storage-unavailable'} & set(after['issues'])
for name in ('assets','relations'):
    assert after[name] >= before[name] * .9, (name,before[name],after[name])
for name in ('candidates','stocks'):
    assert after['coverage'][name]['total'] >= before['coverage'][name]['total'] * .9
assert len(after['sources']) >= len(before['sources'])
assert len(after['chainStreams']) == len(before['chainStreams'])
print('health parity',after['assets'],after['relations'],after['coverage']['stocks']['total'],after['status'])
PY
  then
    test "$(pm2 pid pyradar-worker)" = "$worker_pid"
    test "$(pm2 pid pyradar-projection)" = "$projection_pid"
    complete=1
    pm2 save >/dev/null
    printf 'Bounded health route live; code rollback: %s\n' "$backup"
    exit 0
  fi
  sleep 5
done
false
REMOTE

curl --compressed -fsS --max-time 20 https://cliperx.com/dashboard/api/health/ready -o /dev/null
