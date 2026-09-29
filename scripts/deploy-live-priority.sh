#!/usr/bin/env bash
# Publish live-backlog scheduling and private checkpoint writes with rollback.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
files=(server-py/app/worker.py server-py/app/collectors/scheduler.py server-py/app/db.py)
old=(3ebc4229583a40e67f33322616f126b4bc079267e9d64d72f9930571a6119ac0
     40ec8c19df697951e4731d7782a02de65c6ef1573da14ed30db80e83d59ab27f
     7dfbee8f37dc11b1565fa18e6e53115cf978185d0599764820d9ce1ef32a6a37)
new=()
for i in "${!files[@]}"; do
  new+=("$(shasum -a 256 "${files[i]}" | cut -d' ' -f1)")
  scp "${SSH_OPTS[@]}" "${files[i]}" "$SERVER:/tmp/cliperx-live-priority-$i.py"
done
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "${old[@]}" "${new[@]}" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
files=(server-py/app/worker.py server-py/app/collectors/scheduler.py server-py/app/db.py)
expected_old=("$1" "$2" "$3")
expected_new=("$4" "$5" "$6")
for i in "${!files[@]}"; do
  test "$(sha256sum "${files[i]}" | cut -d' ' -f1)" = "${expected_old[i]}"
  staged="/tmp/cliperx-live-priority-$i.py"
  test "$(sha256sum "$staged" | cut -d' ' -f1)" = "${expected_new[i]}"
  server-py/.venv/bin/python -m py_compile "$staged"
done
backup=".releases/live-priority-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$backup"
for i in "${!files[@]}"; do cp -p "${files[i]}" "$backup/$i.py"; done
changed=0
done=0
finish() {
  result=$?
  trap - EXIT
  if [ "$done" -ne 1 ] && [ "$changed" -eq 1 ]; then
    pm2 stop pyradar-worker >/dev/null 2>&1 || true
    for i in "${!files[@]}"; do cp -p "$backup/$i.py" "${files[i]}"; done
    pm2 restart pyradar-worker >/dev/null 2>&1 || true
    printf 'Live priority rollback: %s\n' "$backup" >&2
  fi
  exit "$result"
}
trap finish EXIT
old_pid=$(pm2 pid pyradar-worker)
pm2 stop pyradar-worker >/dev/null
changed=1
for i in "${!files[@]}"; do cp -p "/tmp/cliperx-live-priority-$i.py" "${files[i]}"; done
pm2 restart pyradar-worker >/dev/null
for attempt in {1..36}; do
  new_pid=$(pm2 pid pyradar-worker)
  if [ "${new_pid:-0}" -gt 0 ] && [ "$new_pid" != "$old_pid" ] &&
     curl -fsS --max-time 8 http://127.0.0.1:8010/api/health/data -o /tmp/cliperx-live-priority-health.json &&
     python3 - /tmp/cliperx-live-priority-health.json "$new_pid" <<'PY'
import json,sys
h=json.load(open(sys.argv[1])); c=h.get('collector') or {}
assert c.get('pid')==int(sys.argv[2]) and c.get('updatedAt')
PY
  then
    done=1
    pm2 save >/dev/null
    printf 'Live priority deployed; code rollback: %s\n' "$backup"
    exit 0
  fi
  sleep 5
done
false
REMOTE
