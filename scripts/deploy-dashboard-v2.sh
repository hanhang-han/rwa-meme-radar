#!/usr/bin/env bash
# First, narrow release of the independent /dashboardv2/ frontend and read model.
# No research database, credentials, dependencies, or collector code are shipped.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
BUILD_DIR=$(mktemp -d /tmp/cliperx-dashboard-v2-build.XXXXXX)
PACKAGE=$(mktemp /tmp/cliperx-dashboard-v2.XXXXXX)
KEEP_PACKAGE=0
cleanup_local() {
  rm -rf "$BUILD_DIR"
  if [ "$KEEP_PACKAGE" -eq 0 ]; then rm -f "$PACKAGE"; fi
}
trap cleanup_local EXIT

if [ "${1:-}" != "" ] && [ "${1:-}" != "--prepare-only" ]; then
  printf 'Usage: %s [--prepare-only]\n' "$0" >&2
  exit 2
fi

# The exact pre-v2 state is a guard against silently replacing a newer
# production state.py with an older local copy.
BASE_STATE_SHA=$(git show dashboard-before-v2-20260929:server-py/app/state.py | shasum -a 256 | awk '{print $1}')
REQUIREMENTS_SHA=$(shasum -a 256 server-py/requirements.txt | awk '{print $1}')

node --test web/tests/*.test.js
PYTHONPATH=server-py server-py/.venv/bin/python -m unittest discover \
  -s server-py/tests -p test_theme_map.py
(cd web && VITE_BASE=/dashboardv2/ npm run build -- --outDir "$BUILD_DIR/dist" --emptyOutDir)
test -f "$BUILD_DIR/dist/index.html"
grep -Fq '/dashboardv2/assets/' "$BUILD_DIR/dist/index.html"
INDEX_SHA=$(shasum -a 256 "$BUILD_DIR/dist/index.html" | awk '{print $1}')

mkdir -p "$BUILD_DIR/server-py/app" "$BUILD_DIR/scripts"
cp server-py/app/state.py server-py/app/theme_map.py "$BUILD_DIR/server-py/app/"
cp scripts/dashboardv2-nginx.conf "$BUILD_DIR/scripts/"
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$PACKAGE" -C "$BUILD_DIR" dist server-py scripts
PACKAGE_SHA=$(shasum -a 256 "$PACKAGE" | awk '{print $1}')
if [ "${1:-}" = "--prepare-only" ]; then
  KEEP_PACKAGE=1
  printf 'Prepared V2 package: %s\nSHA-256: %s\n' "$PACKAGE" "$PACKAGE_SHA"
  exit 0
fi

REMOTE_PACKAGE="/tmp/cliperx-dashboard-v2-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
scp "${SSH_OPTS[@]}" "$PACKAGE" "$SERVER:$REMOTE_PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$REMOTE_PACKAGE" "$PACKAGE_SHA" \
  "$INDEX_SHA" "$BASE_STATE_SHA" "$REQUIREMENTS_SHA" <<'REMOTE'
set -Eeuo pipefail
export PATH="/www/server/nodejs/v22.22.0/bin:$PATH"
PACKAGE="$1"
PACKAGE_SHA="$2"
INDEX_SHA="$3"
BASE_STATE_SHA="$4"
REQUIREMENTS_SHA="$5"
ROOT=/opt/memedashboard
SITE_CONF=/www/server/panel/vhost/nginx/cliperx.com.conf
NGINX_SNIPPET=/opt/memedashboard/nginx/dashboardv2.conf
NGINX_BIN=/www/server/nginx/sbin/nginx
cd "$ROOT"
test -x "$NGINX_BIN"
sudo -n test -f "$SITE_CONF"
test ! -e "$NGINX_SNIPPET"
test ! -L "$NGINX_SNIPPET"
test -f server-py/.venv/bin/python
test "$(sha256sum "$PACKAGE" | awk '{print $1}')" = "$PACKAGE_SHA"
test "$(sha256sum server-py/requirements.txt | awk '{print $1}')" = "$REQUIREMENTS_SHA"
test "$(sha256sum server-py/app/state.py | awk '{print $1}')" = "$BASE_STATE_SHA"
sudo -n "$NGINX_BIN" -t >/dev/null
# Confirm this file participates in the running Nginx configuration, and
# prevent an automatic edit if another V2 route already exists anywhere.
sudo -n "$NGINX_BIN" -T 2>&1 | awk -v path="$SITE_CONF" \
  '$0 == "# configuration file " path ":" { found=1 } END { exit !found }'
if sudo -n "$NGINX_BIN" -T 2>&1 | awk 'index($0,"/dashboardv2") { found=1 } END { exit !found }'; then
  printf 'An existing /dashboardv2 route needs manual review before replacing it.\n' >&2
  exit 1
fi
pm2 describe pyradar >/dev/null
pm2 describe pyradar-projection >/dev/null
pm2 describe pyradar-worker >/dev/null
WORKER_PID=$(pm2 pid pyradar-worker)
test "${WORKER_PID:-0}" -gt 0
curl -fsS --max-time 10 http://127.0.0.1:8010/api/health/ready -o /dev/null
test -f "$ROOT/web/dist/index.html"
OLD_WEB_SHA=$(sha256sum "$ROOT/web/dist/index.html" | awk '{print $1}')

mkdir -p .releases web-v2
STAGING=$(mktemp -d "$ROOT/.releases/dashboard-v2-staging.XXXXXX")
BACKUP="$ROOT/.releases/dashboard-v2-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
BACKEND_TOUCHED=0
BACKEND_UPDATED=0
THEME_EXISTED=0
WEB_TOUCHED=0
WEB_HAD_OLD=0
NGINX_CHANGED=0
NGINX_SNIPPET_CREATED=0
SUCCEEDED=0
rollback_on_exit() {
  result=$?
  trap - EXIT
  if [ "$SUCCEEDED" -eq 1 ]; then
    rm -rf "$STAGING"
    rm -f "$PACKAGE"
    return 0
  fi
  set +e
  printf 'V2 release failed; restoring changed files from %s\n' "$BACKUP" >&2
  rollback_failed=0
  nginx_restored=1
  if [ "$NGINX_CHANGED" -eq 1 ]; then
    if ! sudo -n cp -p "$BACKUP/cliperx.com.conf" "$SITE_CONF" ||
       ! sudo -n "$NGINX_BIN" -t ||
       ! sudo -n "$NGINX_BIN" -s reload; then
      nginx_restored=0
      rollback_failed=1
      printf 'Nginx rollback failed; original site file is %s\n' "$BACKUP/cliperx.com.conf" >&2
    fi
  fi
  if [ "$NGINX_SNIPPET_CREATED" -eq 1 ] && [ "$nginx_restored" -eq 1 ]; then
    if ! rm -f "$NGINX_SNIPPET"; then rollback_failed=1; fi
  fi
  if ! sudo -n rm -f "$SITE_CONF.dashboardv2-tmp-$$"; then rollback_failed=1; fi
  if [ "$WEB_TOUCHED" -eq 1 ]; then
    if [ -d "$BACKUP/dist" ]; then
      if ! rm -rf "$ROOT/web-v2/dist" ||
         ! mv "$BACKUP/dist" "$ROOT/web-v2/dist"; then rollback_failed=1; fi
    elif [ "$WEB_HAD_OLD" -eq 0 ]; then
      if ! rm -rf "$ROOT/web-v2/dist"; then rollback_failed=1; fi
    fi
  fi
  if [ "$BACKEND_TOUCHED" -eq 1 ]; then
    if ! pm2 stop pyradar-projection >/dev/null 2>&1; then rollback_failed=1; fi
    if ! pm2 stop pyradar >/dev/null 2>&1; then rollback_failed=1; fi
    if [ "$BACKEND_UPDATED" -eq 1 ]; then
      if ! cp -p "$BACKUP/state.py" server-py/app/state.py; then rollback_failed=1; fi
      if [ "$THEME_EXISTED" -eq 1 ]; then
        if ! cp -p "$BACKUP/theme_map.py" server-py/app/theme_map.py; then rollback_failed=1; fi
      else
        if ! rm -f server-py/app/theme_map.py; then rollback_failed=1; fi
      fi
    fi
    if ! pm2 restart pyradar-projection >/dev/null 2>&1; then rollback_failed=1; fi
    if ! pm2 restart pyradar >/dev/null 2>&1; then rollback_failed=1; fi
  fi
  if ! rm -rf "$STAGING"; then rollback_failed=1; fi
  if ! rm -f "$PACKAGE"; then rollback_failed=1; fi
  if [ "$rollback_failed" -eq 1 ]; then
    printf 'Automatic rollback INCOMPLETE; inspect %s and live services.\n' "$BACKUP" >&2
    exit 2
  fi
  printf 'Automatic rollback completed; existing /dashboard/ retained.\n' >&2
  exit "$result"
}
trap rollback_on_exit EXIT

tar xzf "$PACKAGE" -C "$STAGING"
test "$(sha256sum "$STAGING/dist/index.html" | awk '{print $1}')" = "$INDEX_SHA"
test -f "$STAGING/server-py/app/state.py"
test -f "$STAGING/server-py/app/theme_map.py"
test -f "$STAGING/scripts/dashboardv2-nginx.conf"
cp -p server-py/app/state.py "$BACKUP/state.py"
if [ -f server-py/app/theme_map.py ]; then
  THEME_EXISTED=1
  cp -p server-py/app/theme_map.py "$BACKUP/theme_map.py"
fi
sudo -n cp -p "$SITE_CONF" "$BACKUP/cliperx.com.conf"

# Test the new pair against the actual production package and dependencies,
# with read-only imports and an isolated, nonexistent database path.
mkdir -p "$STAGING/validation/server-py"
cp -a server-py/app "$STAGING/validation/server-py/app"
cp "$STAGING/server-py/app/state.py" "$STAGING/validation/server-py/app/state.py"
cp "$STAGING/server-py/app/theme_map.py" "$STAGING/validation/server-py/app/theme_map.py"
PYTHONDONTWRITEBYTECODE=1 server-py/.venv/bin/python -m compileall -q "$STAGING/validation/server-py/app"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$STAGING/validation/server-py" \
  RESEARCH_DB="$STAGING/nonexistent.sqlite" \
  server-py/.venv/bin/python - <<'PY'
from app.theme_map import build_theme_map, build_important_changes
from app.state import DashboardData
assert callable(build_theme_map) and callable(build_important_changes)
assert DashboardData
PY

# Add an include to the one unambiguous HTTPS server block. The Python parser
# masks comments and quoted text before counting braces; a surprising layout
# aborts instead of editing an unrelated virtual host.
sudo -n cat "$SITE_CONF" > "$STAGING/site.conf"
python3 - "$STAGING/site.conf" "$STAGING/site.conf.next" "$NGINX_SNIPPET" <<'PY'
import pathlib, re, sys
source_path, target_path, snippet_path = map(pathlib.Path, sys.argv[1:])
text = source_path.read_text()
masked = list(text)
quote = None
comment = False
escaped = False
for i, char in enumerate(text):
    if comment:
        if char == '\n':
            comment = False
        else:
            masked[i] = ' '
        continue
    if quote:
        masked[i] = ' '
        if escaped:
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == quote:
            quote = None
        continue
    if char == '#':
        comment = True
        masked[i] = ' '
    elif char in ('"', "'"):
        quote = char
        masked[i] = ' '
masked = ''.join(masked)
blocks = []
for match in re.finditer(r'\bserver\s*\{', masked):
    opening = masked.find('{', match.start(), match.end())
    depth = 1
    closing = opening + 1
    while closing < len(masked) and depth:
        depth += (masked[closing] == '{') - (masked[closing] == '}')
        closing += 1
    if depth:
        raise SystemExit('Unbalanced Nginx server block')
    body = masked[opening + 1:closing - 1]
    names = [name for value in re.findall(r'\bserver_name\s+([^;]+);', body)
             for name in value.split()]
    listens = re.findall(r'\blisten\s+([^;]+);', body)
    if 'cliperx.com' in names and any(re.search(r'(?<!\d)443(?!\d)', listen) for listen in listens):
        blocks.append((opening, closing - 1, body))
if len(blocks) != 1:
    raise SystemExit(f'Expected one cliperx.com HTTPS block, found {len(blocks)}')
opening, closing, body = blocks[0]
if '/dashboard/' not in body:
    raise SystemExit('The target HTTPS block does not contain the known /dashboard/ route')
if '/dashboardv2' in masked:
    raise SystemExit('Existing V2 route requires manual review')
include = f'\n    include {snippet_path};\n'
target_path.write_text(text[:closing] + include + text[closing:])
PY
cp "$STAGING/scripts/dashboardv2-nginx.conf" "$BACKUP/dashboardv2-nginx.conf"

# Stop only read-model and API processes. The collector process remains live.
BACKEND_TOUCHED=1
pm2 stop pyradar-projection
pm2 stop pyradar
BACKEND_UPDATED=1
mv "$STAGING/server-py/app/theme_map.py" server-py/app/theme_map.py
mv "$STAGING/server-py/app/state.py" server-py/app/state.py
pm2 restart pyradar-projection
pm2 restart pyradar

# The worker republishes the existing facts at least every 30 seconds, even
# when no new event arrives. Wait for the new read model before exposing V2.
for attempt in {1..20}; do
  if curl -fsS --max-time 10 http://127.0.0.1:8010/api/health/ready -o /dev/null &&
     curl -fsS --max-time 25 'http://127.0.0.1:8010/api/dashboard?view=full' -o "$STAGING/dashboard.json" &&
     server-py/.venv/bin/python - "$STAGING/dashboard.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
unified = data['unified']
theme = unified['themeMap']
changes = unified['importantChanges']
assert theme['version'] == 'official-a-theme-map-v1'
assert isinstance(theme['themes'], list) and isinstance(theme['bubbles'], list)
assert isinstance(changes['items'], list)
assert isinstance(unified['assets'], list)
PY
  then break; fi
  if [ "$attempt" -eq 20 ]; then false; fi
  sleep 3
done

WEB_TOUCHED=1
if [ -d "$ROOT/web-v2/dist" ]; then
  WEB_HAD_OLD=1
  mv "$ROOT/web-v2/dist" "$BACKUP/dist"
fi
mv "$STAGING/dist" "$ROOT/web-v2/dist"

# Preserve existing owner/mode by copying into a sibling of the site file,
# then atomically rename it. Test before a graceful reload.
mkdir -p "$ROOT/nginx"
ln "$STAGING/scripts/dashboardv2-nginx.conf" "$NGINX_SNIPPET"
NGINX_SNIPPET_CREATED=1
sudo -n cp -p "$SITE_CONF" "$SITE_CONF.dashboardv2-tmp-$$"
sudo -n cp "$STAGING/site.conf.next" "$SITE_CONF.dashboardv2-tmp-$$"
NGINX_CHANGED=1
sudo -n mv "$SITE_CONF.dashboardv2-tmp-$$" "$SITE_CONF"
sudo -n "$NGINX_BIN" -t
sudo -n "$NGINX_BIN" -s reload

test "$(sha256sum "$ROOT/web-v2/dist/index.html" | awk '{print $1}')" = "$INDEX_SHA"
test "$(pm2 pid pyradar-worker)" = "$WORKER_PID"
curl -fsS --max-time 20 https://cliperx.com/dashboardv2/ -o "$STAGING/public-index.html"
test "$(sha256sum "$STAGING/public-index.html" | awk '{print $1}')" = "$INDEX_SHA"
ASSET_PATH=$(python3 - "$STAGING/public-index.html" <<'PY'
import re, sys
html = open(sys.argv[1]).read()
match = re.search(r'/dashboardv2/assets/[^"\s<>]+\.js', html)
if not match:
    raise SystemExit('V2 index did not reference a versioned script')
print(match.group(0))
PY
)
curl -fsS --max-time 20 "https://cliperx.com$ASSET_PATH" -o /dev/null
curl -fsS --max-time 15 https://cliperx.com/dashboardv2/api/health/ready -o /dev/null
curl -fsS --max-time 30 'https://cliperx.com/dashboardv2/api/dashboard?view=overview' \
  -o "$STAGING/public-dashboard.json"
server-py/.venv/bin/python - "$STAGING/public-dashboard.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
assert data['unified']['themeMap']['version'] == 'official-a-theme-map-v1'
assert isinstance(data['unified']['importantChanges']['items'], list)
PY
curl -fsS --max-time 15 https://cliperx.com/dashboard/api/health/ready -o /dev/null
test "$(sha256sum "$ROOT/web/dist/index.html" | awk '{print $1}')" = "$OLD_WEB_SHA"
SUCCEEDED=1
printf 'V2 live: https://cliperx.com/dashboardv2/\nRollback directory: %s\n' "$BACKUP"
REMOTE
printf 'Deployed V2: https://cliperx.com/dashboardv2/\n'
