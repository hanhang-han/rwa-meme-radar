#!/usr/bin/env bash
# Narrow release for /api/dashboard?view=market. It never ships a database,
# frontend, collector, worker, projection process, dependencies, or Nginx file.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
if [ "${1:-}" != "" ] && [ "${1:-}" != "--prepare-only" ]; then
  printf 'Usage: %s [--prepare-only]\n' "$0" >&2
  exit 2
fi

SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
PACKAGE=$(mktemp /tmp/cliperx-v2-market-api.XXXXXX.tar.gz)
trap 'rm -f "$PACKAGE"' EXIT

# This release is based on the deployed V2 revision. A mismatch on the host
# stops the release rather than overwriting a newer server-side edit.
DEPLOYED_REVISION=4e4c34b
BASE_API_SHA=$(git show "$DEPLOYED_REVISION:server-py/app/api/dashboard.py" | shasum -a 256 | awk '{print $1}')
BASE_PROJECTION_SHA=$(git show "$DEPLOYED_REVISION:server-py/app/dashboard_projection.py" | shasum -a 256 | awk '{print $1}')
REQUIREMENTS_SHA=$(shasum -a 256 server-py/requirements.txt | awk '{print $1}')

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=server-py server-py/.venv/bin/python -m unittest discover \
  -s server-py/tests -p test_dashboard_projection.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=server-py server-py/.venv/bin/python - \
  server-py/app/api/dashboard.py server-py/app/dashboard_projection.py <<'PY'
import ast, pathlib, sys
for filename in sys.argv[1:]:
    ast.parse(pathlib.Path(filename).read_text(), filename=filename)
from app.dashboard_projection import market_dashboard
from app.api.dashboard import _snapshot_json
sample = {'unified': {'assets': [], 'stockTokens': [], 'relations': []}}
assert market_dashboard(sample)['unified']['snapshotScope'] == 'market'
assert callable(_snapshot_json)
PY

COPYFILE_DISABLE=1 tar --no-xattrs -czf "$PACKAGE" -C server-py \
  app/api/dashboard.py app/dashboard_projection.py
PACKAGE_SHA=$(shasum -a 256 "$PACKAGE" | awk '{print $1}')
API_SHA=$(shasum -a 256 server-py/app/api/dashboard.py | awk '{print $1}')
PROJECTION_SHA=$(shasum -a 256 server-py/app/dashboard_projection.py | awk '{print $1}')
if [ "${1:-}" = "--prepare-only" ]; then
  printf 'V2 market API package prepared and tested (SHA-256: %s). No server changes.\n' "$PACKAGE_SHA"
  exit 0
fi

REMOTE_PACKAGE="/tmp/cliperx-v2-market-api-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
scp "${SSH_OPTS[@]}" "$PACKAGE" "$SERVER:$REMOTE_PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$REMOTE_PACKAGE" "$PACKAGE_SHA" \
  "$API_SHA" "$PROJECTION_SHA" "$BASE_API_SHA" "$BASE_PROJECTION_SHA" \
  "$REQUIREMENTS_SHA" <<'REMOTE'
set -Eeuo pipefail
export PATH="/www/server/nodejs/v22.22.0/bin:$PATH"
PACKAGE="$1"
PACKAGE_SHA="$2"
API_SHA="$3"
PROJECTION_SHA="$4"
BASE_API_SHA="$5"
BASE_PROJECTION_SHA="$6"
REQUIREMENTS_SHA="$7"
ROOT=/opt/memedashboard
cd "$ROOT"

STAGING=
BACKUP=
FILES_TOUCHED=0
SUCCEEDED=0
finish() {
  result=$?
  trap - EXIT
  set +e
  rollback_failed=0
  if [ "$SUCCEEDED" -eq 0 ] && [ "$FILES_TOUCHED" -eq 1 ]; then
    printf 'V2 market API release failed; restoring %s\n' "$BACKUP" >&2
    cp -p "$BACKUP/dashboard.py" server-py/app/api/dashboard.py || rollback_failed=1
    cp -p "$BACKUP/dashboard_projection.py" server-py/app/dashboard_projection.py || rollback_failed=1
    pm2 restart pyradar >/dev/null 2>&1 || rollback_failed=1
    if [ "$rollback_failed" -eq 0 ]; then
      for attempt in 1 2 3 4 5 6 7 8 9 10; do
        if curl -fsS --max-time 8 http://127.0.0.1:8010/api/health/ready -o /dev/null &&
           curl --compressed -fsS --max-time 35 \
             'http://127.0.0.1:8010/api/dashboard?view=overview' -o /dev/null; then
          break
        fi
        if [ "$attempt" -eq 10 ]; then rollback_failed=1; fi
        sleep 2
      done
    fi
    if [ "$rollback_failed" -eq 0 ]; then
      printf 'Automatic API rollback completed; old dashboard views are available.\n' >&2
    fi
  fi
  if [ -n "$STAGING" ]; then rm -rf "$STAGING" || rollback_failed=1; fi
  rm -f "$PACKAGE" || rollback_failed=1
  if [ "$rollback_failed" -ne 0 ]; then
    printf 'Automatic rollback/cleanup INCOMPLETE; inspect %s and pyradar.\n' "$BACKUP" >&2
    exit 2
  fi
  exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

test -f "$PACKAGE"
test "$(sha256sum "$PACKAGE" | awk '{print $1}')" = "$PACKAGE_SHA"
test -x server-py/.venv/bin/python
test -f server-py/app/api/dashboard.py
test -f server-py/app/dashboard_projection.py
test ! -L server-py/app/api/dashboard.py
test ! -L server-py/app/dashboard_projection.py
test "$(sha256sum server-py/requirements.txt | awk '{print $1}')" = "$REQUIREMENTS_SHA"
test "$(sha256sum server-py/app/api/dashboard.py | awk '{print $1}')" = "$BASE_API_SHA"
test "$(sha256sum server-py/app/dashboard_projection.py | awk '{print $1}')" = "$BASE_PROJECTION_SHA"
pm2 describe pyradar >/dev/null
pm2 describe pyradar-worker >/dev/null
pm2 describe pyradar-projection >/dev/null
WORKER_PID=$(pm2 pid pyradar-worker)
PROJECTION_PID=$(pm2 pid pyradar-projection)
test "${WORKER_PID:-0}" -gt 0
test "${PROJECTION_PID:-0}" -gt 0
curl -fsS --max-time 10 http://127.0.0.1:8010/api/health/ready -o /dev/null
curl --compressed -fsS --max-time 40 \
  'http://127.0.0.1:8010/api/dashboard?view=overview' -o /dev/null
curl -fsS --max-time 15 https://cliperx.com/dashboardv2/api/health/ready -o /dev/null

mkdir -p "$ROOT/.releases"
STAGING=$(mktemp -d "$ROOT/.releases/dashboard-v2-market-api-staging.XXXXXX")
BACKUP="$ROOT/.releases/dashboard-v2-market-api-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir "$BACKUP"
tar xzf "$PACKAGE" -C "$STAGING"
test "$(sha256sum "$STAGING/app/api/dashboard.py" | awk '{print $1}')" = "$API_SHA"
test "$(sha256sum "$STAGING/app/dashboard_projection.py" | awk '{print $1}')" = "$PROJECTION_SHA"
cp -p server-py/app/api/dashboard.py "$BACKUP/dashboard.py"
cp -p server-py/app/dashboard_projection.py "$BACKUP/dashboard_projection.py"

# Validate the exact candidate against the host's unchanged modules and venv
# before touching the running application or its database.
mkdir -p "$STAGING/candidate/server-py"
cp -a server-py/app "$STAGING/candidate/server-py/app"
cp "$STAGING/app/api/dashboard.py" "$STAGING/candidate/server-py/app/api/dashboard.py"
cp "$STAGING/app/dashboard_projection.py" "$STAGING/candidate/server-py/app/dashboard_projection.py"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$STAGING/candidate/server-py" \
  RESEARCH_DB="$STAGING/nonexistent.sqlite" server-py/.venv/bin/python - <<'PY'
import ast, os, pathlib
from app.dashboard_projection import market_dashboard, overview_dashboard
from app.api.dashboard import _snapshot_json
for name in ('api/dashboard.py', 'dashboard_projection.py'):
    path = pathlib.Path(os.environ['PYTHONPATH']) / 'app' / name
    ast.parse(path.read_text(), filename=str(path))
sample = {'unified': {'assets': [], 'stockTokens': [], 'relations': []}}
assert market_dashboard(sample)['unified']['snapshotScope'] == 'market'
assert overview_dashboard(sample)['unified']['snapshotScope'] == 'overview'
assert callable(_snapshot_json)
PY

# Existing API workers continue serving their loaded code until this one
# restart. No collector or projection process is stopped or restarted.
FILES_TOUCHED=1
mv -f "$STAGING/app/api/dashboard.py" server-py/app/api/dashboard.py
mv -f "$STAGING/app/dashboard_projection.py" server-py/app/dashboard_projection.py
pm2 restart pyradar

for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS --max-time 8 http://127.0.0.1:8010/api/health/ready -o /dev/null; then
    break
  fi
  if [ "$attempt" -eq 10 ]; then false; fi
  sleep 2
done

curl --compressed -fsS --max-time 45 \
  'http://127.0.0.1:8010/api/dashboard?view=full' -o "$STAGING/full.json"
curl --compressed -fsS --max-time 45 \
  'http://127.0.0.1:8010/api/dashboard?view=overview' -o "$STAGING/overview.json"
curl --compressed -fsS --max-time 45 \
  'http://127.0.0.1:8010/api/dashboard?view=market' -o "$STAGING/market.json"
server-py/.venv/bin/python - "$STAGING/full.json" "$STAGING/overview.json" \
  "$STAGING/market.json" <<'PY'
import json, sys
full, overview, market = (json.load(open(path)) for path in sys.argv[1:])
names = ('assets', 'stockTokens', 'relations')
for document, scope in ((full, 'full'), (overview, 'overview'), (market, 'market')):
    assert isinstance(document, dict)
    unified = document['unified']
    assert unified['snapshotScope'] == scope, (scope, unified.get('snapshotScope'))
    assert all(isinstance(unified.get(name), list) for name in names)
assert full['unified']['assets'], 'the existing full dashboard unexpectedly has no assets'
assert market['unified']['assets'], 'the market dashboard unexpectedly has no assets'
if full.get('realtime') is not None and full.get('realtime') == market.get('realtime'):
    for name in names:
        assert len(market['unified'][name]) == len(full['unified'][name]), name
if full.get('realtime') is not None and full.get('realtime') == overview.get('realtime'):
    assert len(overview['unified']['assets']) <= len(full['unified']['assets'])
PY

curl --compressed -fsS --max-time 45 \
  'https://cliperx.com/dashboardv2/api/dashboard?view=market' -o "$STAGING/public-market.json"
server-py/.venv/bin/python - "$STAGING/public-market.json" <<'PY'
import json, sys
unified = json.load(open(sys.argv[1]))['unified']
assert unified['snapshotScope'] == 'market'
assert all(isinstance(unified.get(name), list)
           for name in ('assets', 'stockTokens', 'relations'))
PY
curl -fsS --max-time 15 https://cliperx.com/dashboard/api/health/ready -o /dev/null
test "$(pm2 pid pyradar-worker)" = "$WORKER_PID"
test "$(pm2 pid pyradar-projection)" = "$PROJECTION_PID"
test "$(sha256sum server-py/app/api/dashboard.py | awk '{print $1}')" = "$API_SHA"
test "$(sha256sum server-py/app/dashboard_projection.py | awk '{print $1}')" = "$PROJECTION_SHA"
SUCCEEDED=1
printf 'V2 market API live; code rollback directory: %s\n' "$BACKUP"
REMOTE
printf 'Deployed V2 market API: https://cliperx.com/dashboardv2/api/dashboard?view=market\n'
