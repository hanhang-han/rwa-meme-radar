#!/usr/bin/env bash
# Update only the existing /dashboardv2/ static frontend; never restart services.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
BUILD_DIR=$(mktemp -d /tmp/cliperx-v2-web-build.XXXXXX)
PACKAGE=$(mktemp /tmp/cliperx-v2-web.XXXXXX.tar.gz)
trap 'rm -rf "$BUILD_DIR"; rm -f "$PACKAGE"' EXIT

if [ "${1:-}" != "" ] && [ "${1:-}" != "--prepare-only" ]; then
  printf 'Usage: %s [--prepare-only]\n' "$0" >&2
  exit 2
fi

node --test web/tests/*.test.js
(cd web && VITE_BASE=/dashboardv2/ npm run build -- --outDir "$BUILD_DIR/dist" --emptyOutDir)
test -f "$BUILD_DIR/dist/index.html"
grep -Fq '/dashboardv2/assets/' "$BUILD_DIR/dist/index.html"
INDEX_SHA=$(shasum -a 256 "$BUILD_DIR/dist/index.html" | awk '{print $1}')
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$PACKAGE" -C "$BUILD_DIR" dist
PACKAGE_SHA=$(shasum -a 256 "$PACKAGE" | awk '{print $1}')

if [ "${1:-}" = "--prepare-only" ]; then
  printf 'V2 frontend package prepared and tested (index SHA-256: %s).\n' "$INDEX_SHA"
  exit 0
fi

REMOTE_PACKAGE="/tmp/cliperx-v2-web-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
scp "${SSH_OPTS[@]}" "$PACKAGE" "$SERVER:$REMOTE_PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$REMOTE_PACKAGE" "$PACKAGE_SHA" "$INDEX_SHA" <<'REMOTE'
set -Eeuo pipefail
PACKAGE="$1"
PACKAGE_SHA="$2"
INDEX_SHA="$3"
ROOT=/opt/memedashboard
cd "$ROOT"

STAGING=
BACKUP=
OLD_MOVED=0
NEW_MOVED=0
SUCCEEDED=0
finish() {
  result=$?
  trap - EXIT
  set +e
  rollback_failed=0
  if [ "$SUCCEEDED" -eq 0 ] && [ "$OLD_MOVED" -eq 1 ]; then
    if [ "$NEW_MOVED" -eq 1 ]; then
      rm -rf "$ROOT/web-v2/dist" || rollback_failed=1
    fi
    mv "$BACKUP/dist" "$ROOT/web-v2/dist" || rollback_failed=1
    if [ "$rollback_failed" -eq 0 ]; then
      printf 'V2 frontend restored from %s\n' "$BACKUP" >&2
    fi
  fi
  if [ -n "$STAGING" ]; then rm -rf "$STAGING" || rollback_failed=1; fi
  rm -f "$PACKAGE" || rollback_failed=1
  if [ "$rollback_failed" -ne 0 ]; then
    printf 'V2 frontend rollback/cleanup incomplete; inspect %s\n' "$BACKUP" >&2
    exit 2
  fi
  exit "$result"
}
trap finish EXIT

test -f "$PACKAGE"
test "$(sha256sum "$PACKAGE" | awk '{print $1}')" = "$PACKAGE_SHA"
test -f "$ROOT/web-v2/dist/index.html"
test -f "$ROOT/web/dist/index.html"
test -f "$ROOT/nginx/dashboardv2.conf"
grep -Fq '/opt/memedashboard/web-v2/dist/' "$ROOT/nginx/dashboardv2.conf"
OLD_WEB_SHA=$(sha256sum "$ROOT/web/dist/index.html" | awk '{print $1}')
curl --compressed -fsS --max-time 15 https://cliperx.com/dashboardv2/ -o /dev/null
curl --compressed -fsS --max-time 15 https://cliperx.com/dashboard/ -o /dev/null
curl -fsS --max-time 15 https://cliperx.com/dashboardv2/api/health/ready -o /dev/null

mkdir -p "$ROOT/.releases"
STAGING=$(mktemp -d "$ROOT/.releases/dashboard-v2-web-staging.XXXXXX")
BACKUP="$ROOT/.releases/dashboard-v2-web-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir "$BACKUP"
tar xzf "$PACKAGE" -C "$STAGING"
test -f "$STAGING/dist/index.html"
test "$(sha256sum "$STAGING/dist/index.html" | awk '{print $1}')" = "$INDEX_SHA"

# Keep old hashed chunks available to browser tabs opened before the switch.
if [ -d "$ROOT/web-v2/dist/assets" ]; then
  cp -an "$ROOT/web-v2/dist/assets/." "$STAGING/dist/assets/"
fi
mv "$ROOT/web-v2/dist" "$BACKUP/dist"
OLD_MOVED=1
mv "$STAGING/dist" "$ROOT/web-v2/dist"
NEW_MOVED=1
test "$(sha256sum "$ROOT/web-v2/dist/index.html" | awk '{print $1}')" = "$INDEX_SHA"

# A successful response alone can be an old CDN copy; require the new index.
for attempt in 1 2 3 4 5; do
  rm -f "$STAGING/public-v2-index.html"
  curl --compressed -fsS --max-time 20 https://cliperx.com/dashboardv2/ \
    -o "$STAGING/public-v2-index.html" || true
  if [ -f "$STAGING/public-v2-index.html" ] &&
     [ "$(sha256sum "$STAGING/public-v2-index.html" | awk '{print $1}')" = "$INDEX_SHA" ]; then
    break
  fi
  if [ "$attempt" -eq 5 ]; then
    printf 'Public V2 index did not match the new release.\n' >&2
    false
  fi
  sleep 2
done
ASSET_PATH=$(python3 - "$STAGING/public-v2-index.html" <<'PY'
import re
import sys
html = open(sys.argv[1]).read()
match = re.search(r'/dashboardv2/assets/[^"\s<>]+\.js', html)
if not match:
    raise SystemExit('V2 index did not reference a versioned script')
print(match.group(0))
PY
)
curl -fsS --max-time 20 "https://cliperx.com$ASSET_PATH" -o /dev/null
curl -fsS --max-time 15 https://cliperx.com/dashboardv2/api/health/ready -o /dev/null
curl --compressed -fsS --max-time 15 https://cliperx.com/dashboard/ \
  -o "$STAGING/public-old-index.html"
test "$(sha256sum "$STAGING/public-old-index.html" | awk '{print $1}')" = "$OLD_WEB_SHA"
test "$(sha256sum "$ROOT/web/dist/index.html" | awk '{print $1}')" = "$OLD_WEB_SHA"

SUCCEEDED=1
printf 'V2 frontend live: https://cliperx.com/dashboardv2/\nRollback directory: %s\n' "$BACKUP"
REMOTE
printf 'Deployed V2 frontend: https://cliperx.com/dashboardv2/\n'
