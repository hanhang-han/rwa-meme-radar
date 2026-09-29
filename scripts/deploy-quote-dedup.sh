#!/usr/bin/env bash
# Reduce duplicate quote writes without changing collector budget or database.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
PACKAGE=$(mktemp /tmp/cliperx-quote-dedup.XXXXXX.tar.gz)
trap 'rm -f "$PACKAGE"' EXIT
FILES=(server-py/app/db.py server-py/app/collectors/assets.py server-py/app/collectors/live_quotes.py)
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$PACKAGE" "${FILES[@]}"
scp "${SSH_OPTS[@]}" "$PACKAGE" "$SERVER:/tmp/cliperx-quote-dedup.tar.gz"

ssh "${SSH_OPTS[@]}" "$SERVER" bash -s <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
test "$(sha256sum server-py/app/db.py | cut -d' ' -f1)" = 918584c7f0ccef095550376dfac7aa9604dde07ba2b8cd94c3a9ac7a384e4230
test "$(sha256sum server-py/app/collectors/assets.py | cut -d' ' -f1)" = 711f94c5f53500ab8b003e2a03ccb9c87a0420f7c60c3443f0d6946cb7d6103f
test "$(sha256sum server-py/app/collectors/live_quotes.py | cut -d' ' -f1)" = 0266635d1c50ca264ed0e4d252344c9ed0b0952a04ff2c0f0706995dfbc5c26d
staging=$(mktemp -d .releases/quote-staging.XXXXXX)
backup=".releases/quote-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$backup"
tar xzf /tmp/cliperx-quote-dedup.tar.gz -C "$staging"
for file in server-py/app/db.py server-py/app/collectors/assets.py server-py/app/collectors/live_quotes.py; do
  server-py/.venv/bin/python -m py_compile "$staging/$file"
  cp -a --parents "$file" "$backup"
done
changed=0
complete=0
finish() {
  result=$?
  trap - EXIT
  if [ "$changed" -eq 1 ] && [ "$complete" -ne 1 ]; then
    pm2 stop pyradar-worker >/dev/null 2>&1 || true
    for file in server-py/app/db.py server-py/app/collectors/assets.py server-py/app/collectors/live_quotes.py; do
      cp -a "$backup/$file" "$file"
    done
    pm2 restart pyradar-worker >/dev/null 2>&1 || true
    printf 'Quote release rolled back from %s\n' "$backup" >&2
  fi
  rm -rf "$staging"
  exit "$result"
}
trap finish EXIT
old_pid=$(pm2 pid pyradar-worker)
pm2 stop pyradar-worker >/dev/null
changed=1
for file in server-py/app/db.py server-py/app/collectors/assets.py server-py/app/collectors/live_quotes.py; do
  cp -a "$staging/$file" "$file"
done
pm2 restart pyradar-worker >/dev/null
for attempt in {1..36}; do
  new_pid=$(pm2 pid pyradar-worker)
  if [ "${new_pid:-0}" -gt 0 ] && [ "$new_pid" != "$old_pid" ] &&
     curl -fsS --max-time 10 http://127.0.0.1:8010/api/health/data -o /tmp/cliperx-quote-health.json &&
     server-py/.venv/bin/python - /tmp/cliperx-quote-health.json "$new_pid" <<'PY'
import json, sys
h=json.load(open(sys.argv[1])); c=h.get('collector') or {}
assert c.get('pid') == int(sys.argv[2]) and c.get('updatedAt')
PY
  then
    complete=1
    pm2 save >/dev/null
    printf 'Quote dedup live; code rollback: %s\n' "$backup"
    exit 0
  fi
  sleep 5
done
false
REMOTE
