#!/usr/bin/env bash
# Publish only the invite API's Python code. The 13 GB research database is
# unchanged; the separate, small developer-access database is backed up.
set -Eeuo pipefail

SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="$HOME/.ssh/id_tencent"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
PACKAGE=$(mktemp /tmp/cliperx-api-beta.XXXXXX.tar.gz)
trap 'rm -f "$PACKAGE"' EXIT

# This narrow release is valid only while Python dependencies are unchanged.
REQUIREMENTS_SHA=$(shasum -a 256 server-py/requirements.txt | awk '{print $1}')
# Ship only the new beta surface. Other local collector changes are outside
# this release, even when the working tree contains them.
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$PACKAGE" -C server-py \
  app/main.py app/developer_access.py app/developer_admin.py \
  app/api/developer.py app/api/public_v1.py
scp "${SSH_OPTS[@]}" "$PACKAGE" "$SERVER:/tmp/cliperx-api-beta-release.tar.gz"

ssh "${SSH_OPTS[@]}" "$SERVER" "REQUIREMENTS_SHA=$REQUIREMENTS_SHA bash -s" <<'REMOTE'
set -Eeuo pipefail
export PATH="/www/server/nodejs/v22.22.0/bin:$PATH"
cd /opt/memedashboard
test "$(sha256sum server-py/requirements.txt | awk '{print $1}')" = "$REQUIREMENTS_SHA"

mkdir -p .releases
STAGING=$(mktemp -d /opt/memedashboard/.releases/api-beta-staging.XXXXXX)
BACKUP="/opt/memedashboard/.releases/api-beta-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
OLD_MOVED=0
NEW_MOVED=0
restore() {
  result=$?
  trap - ERR
  set +e
  if [ "$OLD_MOVED" -eq 1 ]; then
    pm2 stop pyradar >/dev/null 2>&1
    if [ "$NEW_MOVED" -eq 1 ]; then rm -rf server-py/app; fi
    mv "$BACKUP/app" server-py/app
    pm2 restart pyradar >/dev/null 2>&1
  fi
  rm -rf "$STAGING"
  exit "$result"
}
trap restore ERR

# Start from the exact production application, then overlay the small beta
# change set. Collector and research-schema files remain byte-for-byte the
# version already running on the host.
mkdir -p "$STAGING/server-py"
cp -a server-py/app "$STAGING/server-py/app"
tar xzf /tmp/cliperx-api-beta-release.tar.gz -C "$STAGING/server-py"
# registry.py resolves contracts relative to the package's project root.
ln -s /opt/memedashboard/contracts "$STAGING/contracts"
test -f "$STAGING/server-py/app/main.py"
test -f "$STAGING/server-py/app/developer_access.py"
test -f "$STAGING/server-py/app/developer_admin.py"
test -f "$STAGING/server-py/app/api/developer.py"
test -f "$STAGING/server-py/app/api/public_v1.py"

# Test the staged module and access schema without opening the live research
# database or creating live invitations. The runtime venv is reused unchanged.
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$STAGING/server-py" \
  RESEARCH_DB="$STAGING/research.sqlite" \
  DEVELOPER_DB_PATH="$STAGING/developer-access.sqlite" \
  server-py/.venv/bin/python - <<'PY'
from app.main import app
from app import developer_access
developer_access.init()
# Registration fails closed unless the shared trial code is configured in a
# protected server-only file (or the service environment).
assert developer_access._shared_invitation_code()
paths = set(app.openapi()['paths'])
assert '/api/developer/register' in paths
assert '/api/v1/relations' in paths
assert '/api/v1/snapshot/latest' in paths
PY

# Only credentials/usage live in this small independent DB. Back it up with
# SQLite's online backup API; rollback never restores an old copy over new
# registrations or usage.
if [ -f data/developer-access.sqlite ]; then
  server-py/.venv/bin/python - "$BACKUP/developer-access.sqlite" <<'PY'
import sqlite3, sys
source = sqlite3.connect('file:data/developer-access.sqlite?mode=ro', uri=True)
target = sqlite3.connect(sys.argv[1])
source.backup(target)
assert target.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
target.close()
source.close()
PY
fi

pm2 stop pyradar
mv server-py/app "$BACKUP/app"
OLD_MOVED=1
mv "$STAGING/server-py/app" server-py/app
NEW_MOVED=1
pm2 restart pyradar

for attempt in 1 2 3 4 5 6 7 8 9 10; do
  ready=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8010/api/health/ready || true)
  unauthorized=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
    'http://127.0.0.1:8010/api/v1/relations?stock=NVDA' || true)
  if [ "$ready" = 200 ] && [ "$unauthorized" = 401 ]; then
    public_unauthorized=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 \
      'https://cliperx.com/dashboard/api/v1/relations?stock=NVDA' || true)
    if [ "$public_unauthorized" = 401 ]; then
      trap - ERR
      rm -rf "$STAGING"
      printf 'Invite API live; code rollback directory: %s\n' "$BACKUP"
      exit 0
    fi
  fi
  sleep 2
done
false
REMOTE

printf 'Deployed invite API: https://cliperx.com/dashboard/api/v1/\n'
