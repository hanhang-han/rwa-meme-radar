#!/usr/bin/env bash
# Publish the health/collector repair without copying or replacing research.sqlite.
# Keep an exact code rollback without duplicating the live database.
set -Eeuo pipefail

SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="$HOME/.ssh/id_tencent"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
PACKAGE=$(mktemp /tmp/cliperx-health-repair.XXXXXX.tar.gz)
trap 'rm -f "$PACKAGE"' EXIT

# This release never installs dependencies or updates the data schema.
REQUIREMENTS_SHA=$(shasum -a 256 server-py/requirements.txt | awk '{print $1}')
FILES=(
  app/main.py
  app/briefing.py
  app/state.py
  app/stock_identity.py
  app/api/token.py
  app/worker.py
  app/db.py
  app/okx_client.py
  app/comparison_service.py
  app/realtime_projection.py
  app/realtime_schema.py
  app/collectors/live_quotes.py
  app/collectors/assets.py
  app/collectors/chain_stream.py
  app/collectors/market_streams.py
  app/collectors/main_round.py
  app/collectors/scheduler.py
)
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$PACKAGE" -C server-py "${FILES[@]}"
scp "${SSH_OPTS[@]}" "$PACKAGE" "$SERVER:/tmp/cliperx-health-repair.tar.gz"

ssh "${SSH_OPTS[@]}" "$SERVER" "REQUIREMENTS_SHA=$REQUIREMENTS_SHA bash -s" <<'REMOTE'
set -Eeuo pipefail
export PATH="/www/server/nodejs/v22.22.0/bin:$PATH"
cd /opt/memedashboard
test "$(sha256sum server-py/requirements.txt | awk '{print $1}')" = "$REQUIREMENTS_SHA"

STAGING=$(mktemp -d /opt/memedashboard/.releases/health-staging.XXXXXX)
BACKUP="/opt/memedashboard/.releases/health-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP" "$STAGING/server-py"
OLD_MOVED=0
NEW_MOVED=0
SERVICES_STOPPED=0
SERVICES=(pyradar-worker pyradar-projection pyradar)
restore() {
  result=$?
  trap - ERR
  set +e
  if [ "$SERVICES_STOPPED" -eq 1 ]; then
    for service in "${SERVICES[@]}"; do pm2 stop "$service" >/dev/null 2>&1; done
  fi
  if [ "$OLD_MOVED" -eq 1 ]; then
    if [ "$NEW_MOVED" -eq 1 ]; then rm -rf server-py/app; fi
    mv "$BACKUP/app" server-py/app
  fi
  if [ "$SERVICES_STOPPED" -eq 1 ]; then
    for service in "${SERVICES[@]}"; do pm2 restart "$service" >/dev/null 2>&1; done
    pm2 save >/dev/null 2>&1
  fi
  rm -rf "$STAGING"
  exit "$result"
}
trap restore ERR

# Start with the exact running app; local unrelated changes are never shipped.
cp -a server-py/app "$STAGING/server-py/app"
tar xzf /tmp/cliperx-health-repair.tar.gz -C "$STAGING/server-py"
ln -s /opt/memedashboard/contracts "$STAGING/contracts"
PYTHONDONTWRITEBYTECODE=1 server-py/.venv/bin/python -m compileall -q "$STAGING/server-py/app"
(
  cd "$STAGING"
  PYTHONDONTWRITEBYTECODE=1 NODE_ENV=test \
    PYTHONPATH="$STAGING/server-py" \
    RESEARCH_DB="$STAGING/research.sqlite" \
    DEVELOPER_DB_PATH="$STAGING/developer-access.sqlite" \
    /opt/memedashboard/server-py/.venv/bin/python - <<'PY'
from app.main import app
from app.worker import run as collector_run
from app.projection_worker import run as projection_run
from app.collectors.scheduler import apply_result
assert '/api/health/data' in app.openapi()['paths']
assert collector_run and projection_run and apply_result
PY
)

SERVICES_STOPPED=1
for service in "${SERVICES[@]}"; do pm2 stop "$service"; done
mv server-py/app "$BACKUP/app"
OLD_MOVED=1
mv "$STAGING/server-py/app" server-py/app
NEW_MOVED=1
for service in "${SERVICES[@]}"; do pm2 restart "$service"; done

for attempt in {1..24}; do
  if curl -fsS --max-time 10 http://127.0.0.1:8010/api/health/ready >/dev/null &&
     curl -fsS --max-time 20 http://127.0.0.1:8010/api/health/data -o "$STAGING/health.json" &&
     server-py/.venv/bin/python - "$STAGING/health.json" <<'PY'
import json, sys
health = json.load(open(sys.argv[1]))
assert health['collector']['ok'] and health['projection']['ok']
assert isinstance(health['coverage']['candidates']['total'], int)
assert isinstance(health['sources'], list)
PY
  then
    trap - ERR
    pm2 save
    rm -rf "$STAGING"
    printf 'Health repair live; code rollback: %s\n' "$BACKUP"
    exit 0
  fi
  sleep 5
done
false
REMOTE

curl --compressed -fsS --max-time 20 https://cliperx.com/dashboard/api/health/ready -o /dev/null
printf 'Deployed health repair: https://cliperx.com/dashboard/\n'
