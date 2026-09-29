#!/usr/bin/env bash
# Replace full-dashboard detail reads with bounded per-instrument fact queries.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
PACKAGE=$(mktemp /tmp/cliperx-scoped-detail.XXXXXX.tar.gz)
trap 'rm -f "$PACKAGE"' EXIT
PYTHONPATH=server-py server-py/.venv/bin/python -m unittest server-py/tests/test_scoped_detail_reads.py -q
FILES=(server-py/app/api/token.py server-py/app/api/misc.py
  server-py/app/comparison_service.py server-py/app/registry.py server-py/app/scoped_reads.py)
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$PACKAGE" "${FILES[@]}"
scp "${SSH_OPTS[@]}" "$PACKAGE" "$SERVER:/tmp/cliperx-scoped-detail.tar.gz"

ssh "${SSH_OPTS[@]}" "$SERVER" bash -s <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
test "$(sha256sum server-py/app/api/token.py | cut -d' ' -f1)" = bbf7e1e3ee65f37fb1d4247d812b0335d08bb70e858723e54f63c9de0b57e3ea
test "$(sha256sum server-py/app/api/misc.py | cut -d' ' -f1)" = ab365644cdee3f4cb29570c81005739d1de6b42f7c102a195178c7397b90bb5f
test "$(sha256sum server-py/app/comparison_service.py | cut -d' ' -f1)" = 17bf8101b6ab96e2338327c3073b5d5e958af78ef3047b1f8ab0f44193f526ea
test "$(sha256sum server-py/app/registry.py | cut -d' ' -f1)" = dd8068cf187eb5d18fc1b24467c629dec62b5cfb4d18620724daef4a1eb40199
test ! -e server-py/app/scoped_reads.py
staging=$(mktemp -d .releases/scoped-staging.XXXXXX)
backup=".releases/scoped-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$backup/server-py/app/api"
tar xzf /tmp/cliperx-scoped-detail.tar.gz -C "$staging"
for file in server-py/app/api/token.py server-py/app/api/misc.py server-py/app/comparison_service.py server-py/app/registry.py server-py/app/scoped_reads.py; do
  server-py/.venv/bin/python -m py_compile "$staging/$file"
done
for file in server-py/app/api/token.py server-py/app/api/misc.py server-py/app/comparison_service.py server-py/app/registry.py; do
  cp -p --parents "$file" "$backup"
done
asset=$(server-py/.venv/bin/python - <<'PY'
import sqlite3
db=sqlite3.connect('file:data/research.sqlite?mode=ro', uri=True, timeout=3)
row=db.execute("SELECT json_extract(body,'$.token') FROM facts WHERE kind='196:relation' AND json_extract(body,'$.status')='verified' LIMIT 1").fetchone()
assert row and row[0] and len(row[0])==42
print(row[0].lower())
PY
)
stock=$(server-py/.venv/bin/python - <<'PY'
import sqlite3
db=sqlite3.connect('file:data/research.sqlite?mode=ro', uri=True, timeout=3)
row=db.execute("SELECT id FROM facts WHERE kind='196:stock' LIMIT 1").fetchone()
assert row and row[0] and len(row[0])==42
print(row[0].lower())
PY
)
worker_pid=$(pm2 pid pyradar-worker)
projection_pid=$(pm2 pid pyradar-projection)
changed=0
complete=0
finish() {
  result=$?
  trap - EXIT
  if [ "$changed" -eq 1 ] && [ "$complete" -ne 1 ]; then
    pm2 stop pyradar >/dev/null 2>&1 || true
    for file in server-py/app/api/token.py server-py/app/api/misc.py server-py/app/comparison_service.py server-py/app/registry.py; do
      cp -p "$backup/$file" "$file"
    done
    rm -f server-py/app/scoped_reads.py
    pm2 restart pyradar >/dev/null 2>&1 || true
    printf 'Scoped detail API rolled back from %s\n' "$backup" >&2
  fi
  rm -rf "$staging"
  exit "$result"
}
trap finish EXIT
pm2 stop pyradar >/dev/null
changed=1
for file in server-py/app/api/token.py server-py/app/api/misc.py server-py/app/comparison_service.py server-py/app/registry.py server-py/app/scoped_reads.py; do
  cp -p "$staging/$file" "$file"
done
pm2 restart pyradar >/dev/null
for attempt in {1..36}; do
  if curl -fsS --max-time 8 http://127.0.0.1:8010/api/health/ready -o /dev/null &&
     curl --compressed -fsS --max-time 20 "http://127.0.0.1:8010/api/token/196/$asset" -o /tmp/cliperx-scoped-token.json &&
     curl --compressed -fsS --max-time 20 "http://127.0.0.1:8010/api/pair/196/$asset" -o /tmp/cliperx-scoped-pair.json &&
     curl --compressed -fsS --max-time 20 "http://127.0.0.1:8010/api/comparisons/196/$stock" -o /tmp/cliperx-scoped-comparison.json &&
     server-py/.venv/bin/python - "$asset" <<'PY'
import json,sys
token=json.load(open('/tmp/cliperx-scoped-token.json'))
pair=json.load(open('/tmp/cliperx-scoped-pair.json'))
comparison=json.load(open('/tmp/cliperx-scoped-comparison.json'))
assert token['asset']['token'].lower()==sys.argv[1]
assert pair['asset']['token'].lower()==sys.argv[1]
assert isinstance(token['relations'],list) and isinstance(token['pools'],list)
assert isinstance(pair['relations'],list) and 'current' in comparison
print('detail parity',len(token['relations']),len(token['pools']),len(pair['relations']))
PY
  then
    test "$(pm2 pid pyradar-worker)" = "$worker_pid"
    test "$(pm2 pid pyradar-projection)" = "$projection_pid"
    complete=1
    pm2 save >/dev/null
    printf 'Scoped detail API live; code rollback: %s\n' "$backup"
    exit 0
  fi
  sleep 5
done
false
REMOTE

curl --compressed -fsS --max-time 20 https://cliperx.com/dashboard/api/health/ready -o /dev/null
