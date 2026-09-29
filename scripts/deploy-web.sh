#!/usr/bin/env bash
# Publish a frontend-only release without restarting collectors or copying the database.
set -euo pipefail

SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="$HOME/.ssh/id_tencent"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
PACKAGE=$(mktemp /tmp/cliperx-web.XXXXXX.tar.gz)
trap 'rm -f "$PACKAGE"' EXIT

node --test web/tests/*.test.js
(cd web && VITE_BASE=/dashboard/ npm run build)
test -f web/dist/index.html
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$PACKAGE" -C web dist
EXPECTED_INDEX_SHA=$(shasum -a 256 web/dist/index.html | awk '{print $1}')
scp "${SSH_OPTS[@]}" "$PACKAGE" "$SERVER:/tmp/cliperx-web-release.tar.gz"

ssh "${SSH_OPTS[@]}" "$SERVER" "EXPECTED_INDEX_SHA=$EXPECTED_INDEX_SHA bash -s" <<'REMOTE'
set -Eeuo pipefail
cd /opt/memedashboard
mkdir -p .releases
STAGING=$(mktemp -d .releases/web-staging.XXXXXX)
BACKUP=".releases/web-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
OLD_MOVED=0
NEW_MOVED=0
restore() {
  result=$?
  trap - ERR
  if [ "$OLD_MOVED" -eq 1 ]; then
    if [ "$NEW_MOVED" -eq 1 ]; then rm -rf web/dist; fi
    mv "$BACKUP" web/dist
  fi
  rm -rf "$STAGING"
  exit "$result"
}
trap restore ERR
tar xzf /tmp/cliperx-web-release.tar.gz -C "$STAGING"
test -f "$STAGING/dist/index.html"
test "$(sha256sum "$STAGING/dist/index.html" | cut -d' ' -f1)" = "$EXPECTED_INDEX_SHA"
# Keep hashed files needed by browser tabs that loaded the previous index.
if [ -d web/dist/assets ]; then cp -an web/dist/assets/. "$STAGING/dist/assets/"; fi
mv web/dist "$BACKUP"
OLD_MOVED=1
mv "$STAGING/dist" web/dist
NEW_MOVED=1
test "$(sha256sum web/dist/index.html | cut -d' ' -f1)" = "$EXPECTED_INDEX_SHA"
for attempt in 1 2 3 4 5; do
  rm -f "$STAGING/public-index.html"
  curl --compressed -fsS --max-time 15 https://cliperx.com/dashboard/ -o "$STAGING/public-index.html" || true
  if [ -f "$STAGING/public-index.html" ] &&
    [ "$(sha256sum "$STAGING/public-index.html" | cut -d' ' -f1)" = "$EXPECTED_INDEX_SHA" ] &&
    curl --compressed -fsS --max-time 15 https://cliperx.com/dashboard/api/health/ready -o /dev/null; then
    trap - ERR
    rm -rf "$STAGING"
    printf 'Web release live; rollback directory: %s\n' "$BACKUP"
    exit 0
  fi
  sleep 2
done
false
REMOTE
printf 'Deployed frontend: https://cliperx.com/dashboard/\n'
