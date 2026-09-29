#!/usr/bin/env bash
# Move newly discovered OKX stock identities from atomic JSON snapshots to
# Python-owned facts without giving Node a write connection to hot SQLite.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
PACKAGE=$(mktemp /tmp/cliperx-okx-catalogue.XXXXXX.tar.gz)
trap 'rm -f "$PACKAGE"' EXIT

PYTHONPATH=server-py server-py/.venv/bin/python -m unittest server-py/tests/test_okx_catalogue_sync.py -q
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$PACKAGE" \
  server-py/app/worker.py server-py/app/collectors/okx_catalogue.py
scp "${SSH_OPTS[@]}" "$PACKAGE" "$SERVER:/tmp/cliperx-okx-catalogue.tar.gz"

ssh "${SSH_OPTS[@]}" "$SERVER" bash -s <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
test "$(sha256sum server-py/app/worker.py | cut -d' ' -f1)" = 7354a102bb936a08e8809b3a5b92470518edc21519de2a84613dab26278d07a5
test ! -e server-py/app/collectors/okx_catalogue.py
staging=$(mktemp -d .releases/catalogue-staging.XXXXXX)
backup=".releases/catalogue-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$backup/server-py/app"
tar xzf /tmp/cliperx-okx-catalogue.tar.gz -C "$staging"
server-py/.venv/bin/python -m py_compile "$staging/server-py/app/worker.py" "$staging/server-py/app/collectors/okx_catalogue.py"
cp -p server-py/app/worker.py "$backup/server-py/app/worker.py"
old_pid=$(pm2 pid pyradar-worker)
api_pid=$(pm2 pid pyradar)
projection_pid=$(pm2 pid pyradar-projection)
changed=0
complete=0
finish() {
  result=$?
  trap - EXIT
  if [ "$changed" -eq 1 ] && [ "$complete" -ne 1 ]; then
    pm2 stop pyradar-worker >/dev/null 2>&1 || true
    cp -p "$backup/server-py/app/worker.py" server-py/app/worker.py
    rm -f server-py/app/collectors/okx_catalogue.py
    pm2 restart pyradar-worker >/dev/null 2>&1 || true
    printf 'Catalogue sync rolled back from %s\n' "$backup" >&2
  fi
  rm -rf "$staging"
  exit "$result"
}
trap finish EXIT
pm2 stop pyradar-worker >/dev/null
changed=1
cp -p "$staging/server-py/app/worker.py" server-py/app/worker.py
cp -p "$staging/server-py/app/collectors/okx_catalogue.py" server-py/app/collectors/okx_catalogue.py
pm2 restart pyradar-worker >/dev/null
for attempt in {1..48}; do
  new_pid=$(pm2 pid pyradar-worker)
  if [ "${new_pid:-0}" -gt 0 ] && [ "$new_pid" != "$old_pid" ] &&
     server-py/.venv/bin/python - "$new_pid" <<'PY'
import json, sys
from pathlib import Path
health=json.loads(Path('data/worker-health.json').read_text())
task=(health.get('tasks') or {}).get('okxCatalogue') or {}
assert health.get('pid') == int(sys.argv[1])
assert task.get('lastCompletedAt') and task.get('status') not in ('error','quota-blocked')
assert (task.get('result') or {}).get('failed', 0) == 0
print('catalogue sync',task.get('outcome'),task.get('result'))
PY
  then
    test "$(pm2 pid pyradar)" = "$api_pid"
    test "$(pm2 pid pyradar-projection)" = "$projection_pid"
    curl -fsS --max-time 15 http://127.0.0.1:8010/api/health/ready -o /dev/null
    complete=1
    pm2 save >/dev/null
    printf 'Catalogue sync live; code rollback: %s\n' "$backup"
    exit 0
  fi
  sleep 5
done
false
REMOTE

curl --compressed -fsS --max-time 20 https://cliperx.com/dashboard/api/health/ready -o /dev/null
