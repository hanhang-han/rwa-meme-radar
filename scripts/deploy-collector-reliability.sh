#!/usr/bin/env bash
# Publish the collector fix without copying or compacting the live SQLite file.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
FILES=(app/db.py app/worker.py app/collectors/queue.py app/collectors/chain_stream.py)
BASE_REV=9ae430b
PACKAGE=$(mktemp /tmp/cliperx-collector-fix.XXXXXX.tar.gz)
MANIFEST=$(mktemp /tmp/cliperx-collector-fix.XXXXXX.manifest)
trap 'rm -f "$PACKAGE" "$MANIFEST"' EXIT
for file in "${FILES[@]}"; do
  before=$(git show "$BASE_REV:server-py/$file" | shasum -a 256 | cut -d' ' -f1)
  after=$(shasum -a 256 "server-py/$file" | cut -d' ' -f1)
  printf '%s %s %s\n' "$file" "$before" "$after" >> "$MANIFEST"
done
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$PACKAGE" -C server-py "${FILES[@]}"
scp "${SSH_OPTS[@]}" "$PACKAGE" "$SERVER:/tmp/cliperx-collector-fix.tar.gz"
scp "${SSH_OPTS[@]}" "$MANIFEST" "$SERVER:/tmp/cliperx-collector-fix.manifest"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s <<'REMOTE'
set -Eeuo pipefail
export PATH="/www/server/nodejs/v22.22.0/bin:$PATH"
cd /opt/memedashboard
STAGING=$(mktemp -d .releases/collector-staging.XXXXXX)
BACKUP=".releases/collector-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
TOUCHED=0
DONE=0
restore() {
  result=$?
  trap - EXIT
  if [ "$DONE" -ne 1 ] && [ "$TOUCHED" -eq 1 ]; then
    pm2 stop pyradar-worker >/dev/null 2>&1 || true
    while read -r file before after; do cp -p "$BACKUP/$file" "server-py/$file"; done < /tmp/cliperx-collector-fix.manifest
    pm2 restart pyradar-worker >/dev/null 2>&1 || true
    printf 'Collector rollback: %s\n' "$BACKUP" >&2
  fi
  rm -rf "$STAGING"
  exit "$result"
}
trap restore EXIT
tar xzf /tmp/cliperx-collector-fix.tar.gz -C "$STAGING"
while read -r file before after; do
  test "$(sha256sum "server-py/$file" | cut -d' ' -f1)" = "$before"
  test "$(sha256sum "$STAGING/$file" | cut -d' ' -f1)" = "$after"
  mkdir -p "$BACKUP/$(dirname "$file")"
  cp -p "server-py/$file" "$BACKUP/$file"
done < /tmp/cliperx-collector-fix.manifest
PYTHONDONTWRITEBYTECODE=1 server-py/.venv/bin/python -m compileall -q "$STAGING/app"
pm2 describe pyradar-worker >/dev/null
old_pid=$(pm2 pid pyradar-worker)
pm2 stop pyradar-worker
TOUCHED=1
while read -r file before after; do cp -p "$STAGING/$file" "server-py/$file"; done < /tmp/cliperx-collector-fix.manifest
pm2 restart pyradar-worker
for attempt in {1..36}; do
  new_pid=$(pm2 pid pyradar-worker)
  if [ "${new_pid:-0}" -gt 0 ] && [ "$new_pid" != "$old_pid" ] &&
     curl -fsS --max-time 8 http://127.0.0.1:8010/api/health/data -o "$STAGING/health.json" &&
     python3 - "$STAGING/health.json" "$new_pid" <<'PY'
import json,sys
h=json.load(open(sys.argv[1])); w=h.get('worker') or {}
assert w.get('pid')==int(sys.argv[2]) and w.get('updatedAt')
PY
  then
    DONE=1
    pm2 save >/dev/null
    printf 'Collector fix live; code rollback: %s\n' "$BACKUP"
    exit 0
  fi
  sleep 5
done
false
REMOTE
