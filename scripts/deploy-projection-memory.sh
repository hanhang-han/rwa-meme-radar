#!/usr/bin/env bash
# Publish compact dashboard/feed projections without copying or migrating data.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
FILES=(server-py/app/realtime_projection.py server-py/app/api/dashboard.py
  server-py/app/api/misc.py server-py/app/api/stream.py server-py/app/main.py)
STAGING=$(mktemp -d /tmp/cliperx-projection-release.XXXXXX)
trap 'rm -rf "$STAGING"' EXIT

for file in "${FILES[@]}"; do
  printf '%s %s\n' "$(git show "HEAD:$file" | shasum -a 256 | cut -d' ' -f1)" "$file" >> "$STAGING/baseline.sha256"
done
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$STAGING/release.tar.gz" "${FILES[@]}"
scp "${SSH_OPTS[@]}" "$STAGING/baseline.sha256" "$STAGING/release.tar.gz" "$SERVER:/tmp/"

ssh "${SSH_OPTS[@]}" "$SERVER" bash -s <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
while read -r baseline file; do
  test -f "$file" && test ! -L "$file"
  test "$(sha256sum "$file" | cut -d' ' -f1)" = "$baseline"
done < /tmp/baseline.sha256
staging=$(mktemp -d .releases/projection-staging.XXXXXX)
backup=".releases/projection-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$backup"
tar xzf /tmp/release.tar.gz -C "$staging"
while read -r baseline file; do
  server-py/.venv/bin/python -m py_compile "$staging/$file"
  cp -a --parents "$file" "$backup"
done < /tmp/baseline.sha256
worker_pid=$(pm2 pid pyradar-worker)
changed=0
complete=0
finish() {
  result=$?
  trap - EXIT
  if [ "$complete" -ne 1 ] && [ "$changed" -eq 1 ]; then
    pm2 stop pyradar pyradar-projection >/dev/null 2>&1 || true
    while read -r baseline file; do cp -a "$backup/$file" "$file"; done < /tmp/baseline.sha256
    pm2 restart pyradar-projection pyradar >/dev/null 2>&1 || true
    printf 'Projection release rolled back from %s\n' "$backup" >&2
  fi
  rm -rf "$staging"
  exit "$result"
}
trap finish EXIT
while read -r baseline file; do cp -a "$staging/$file" "$file"; done < /tmp/baseline.sha256
changed=1
pm2 restart pyradar-projection >/dev/null
for attempt in {1..36}; do
  if server-py/.venv/bin/python - <<'PY'
import sqlite3
db = sqlite3.connect('file:data/research.sqlite?mode=ro', uri=True, timeout=2)
rows = db.execute("SELECT name, revision, cursor, input_cursor, built_at FROM dashboard_projection WHERE name IN ('full','overview','market','feed')").fetchall()
assert len(rows) == 4 and len({tuple(row[1:]) for row in rows}) == 1
PY
  then break; fi
  sleep 5
done
server-py/.venv/bin/python - <<'PY'
import sqlite3
db = sqlite3.connect('file:data/research.sqlite?mode=ro', uri=True, timeout=2)
rows = db.execute("SELECT name, revision, cursor, input_cursor, built_at FROM dashboard_projection WHERE name IN ('full','overview','market','feed')").fetchall()
assert len(rows) == 4 and len({tuple(row[1:]) for row in rows}) == 1
PY
pm2 restart pyradar >/dev/null
for attempt in {1..30}; do
  if curl -fsS --max-time 10 http://127.0.0.1:8010/api/health/ready -o /dev/null &&
     curl --compressed -fsS --max-time 15 'http://127.0.0.1:8010/api/dashboard?view=overview' -o /dev/null &&
     curl --compressed -fsS --max-time 15 'http://127.0.0.1:8010/api/dashboard?view=market' -o /dev/null &&
     curl --compressed -fsS --max-time 15 'http://127.0.0.1:8010/api/feed' -o /dev/null &&
     test "$(pm2 pid pyradar-worker)" = "$worker_pid"; then
    complete=1
    pm2 save >/dev/null
    printf 'Compact API projection live; code rollback: %s\n' "$backup"
    exit 0
  fi
  sleep 5
done
false
REMOTE

curl --compressed -fsS --max-time 20 https://cliperx.com/dashboard/api/health/ready -o /dev/null
printf 'Deployed compact projection: https://cliperx.com/dashboard/\n'
