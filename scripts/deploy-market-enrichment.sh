#!/usr/bin/env bash
# Publish the Node enrichment scheduler without touching the research database.
set -Eeuo pipefail

SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="$HOME/.ssh/id_tencent"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
PACKAGE=$(mktemp /tmp/cliperx-market-enrichment.XXXXXX.tar.gz)
trap 'rm -f "$PACKAGE"' EXIT

npm run check
npm test
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$PACKAGE" src/server.ts src/lib/market-enrichment.ts src/lib/stock-identity.ts src/lib/okx.ts src/lib/research-store.ts
scp "${SSH_OPTS[@]}" "$PACKAGE" "$SERVER:/tmp/cliperx-market-enrichment.tar.gz"

ssh "${SSH_OPTS[@]}" "$SERVER" 'bash -s' <<'REMOTE'
set -Eeuo pipefail
export PATH="/www/server/nodejs/v22.22.0/bin:$PATH"
cd /opt/memedashboard
STAGING=$(mktemp -d .releases/market-staging.XXXXXX)
BACKUP=".releases/market-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP/src/lib" "$STAGING/src"
OLD_MOVED=0
STOPPED=0
restore() {
  result=$?
  trap - ERR
  set +e
  if [ "$STOPPED" -eq 1 ]; then pm2 stop memedashboard >/dev/null 2>&1; fi
  if [ "$OLD_MOVED" -eq 1 ]; then
    cp -a "$BACKUP/src/server.ts" src/server.ts
    cp -a "$BACKUP/src/lib/market-enrichment.ts" src/lib/market-enrichment.ts
    cp -a "$BACKUP/src/lib/stock-identity.ts" src/lib/stock-identity.ts
    cp -a "$BACKUP/src/lib/okx.ts" src/lib/okx.ts
    cp -a "$BACKUP/src/lib/research-store.ts" src/lib/research-store.ts
  fi
  if [ "$STOPPED" -eq 1 ]; then
    pm2 restart memedashboard >/dev/null 2>&1
    pm2 save >/dev/null 2>&1
  fi
  rm -rf "$STAGING"
  exit "$result"
}
trap restore ERR

# Type-check the exact candidate files against the deployed dependency tree.
cp -a src/. "$STAGING/src/"
tar xzf /tmp/cliperx-market-enrichment.tar.gz -C "$STAGING"
cp package.json package-lock.json tsconfig.json "$STAGING/"
ln -s /opt/memedashboard/node_modules "$STAGING/node_modules"
(cd "$STAGING" && node node_modules/typescript/bin/tsc --noEmit)

# Flush the latest in-memory dashboard view before the brief Node restart.
# The API omits persistent lead/verification/archive stores, so retain them.
curl --compressed -fsS --max-time 30 http://127.0.0.1:3456/api/state -o "$STAGING/state.json.next"
node - "$STAGING/state.json.next" <<'STATE'
const fs = require('fs');
const filename = process.argv[2];
const next = JSON.parse(fs.readFileSync(filename, 'utf8'));
if (!Array.isArray(next.assets) || !next.assets.length) process.exit(1);
let previous = {};
try { previous = JSON.parse(fs.readFileSync('data/state.json', 'utf8')); } catch {}
if (Array.isArray(previous.leadStore)) next.leadStore = previous.leadStore;
if (Array.isArray(previous.verificationStore)) next.verificationStore = previous.verificationStore;
if (Array.isArray(previous.radarArchive)) next.radarArchive = previous.radarArchive;
fs.writeFileSync(filename, JSON.stringify(next));
STATE
cp -a src/server.ts "$BACKUP/src/server.ts"
cp -a src/lib/market-enrichment.ts "$BACKUP/src/lib/market-enrichment.ts"
cp -a src/lib/stock-identity.ts "$BACKUP/src/lib/stock-identity.ts"
cp -a src/lib/okx.ts "$BACKUP/src/lib/okx.ts"
cp -a src/lib/research-store.ts "$BACKUP/src/lib/research-store.ts"
cp -a data/state.json "$BACKUP/state.json"
pm2 stop memedashboard >/dev/null
STOPPED=1
mv "$STAGING/state.json.next" data/state.json
OLD_MOVED=1
cp -a "$STAGING/src/server.ts" src/server.ts
cp -a "$STAGING/src/lib/market-enrichment.ts" src/lib/market-enrichment.ts
cp -a "$STAGING/src/lib/stock-identity.ts" src/lib/stock-identity.ts
cp -a "$STAGING/src/lib/okx.ts" src/lib/okx.ts
cp -a "$STAGING/src/lib/research-store.ts" src/lib/research-store.ts
pm2 restart memedashboard >/dev/null

for attempt in {1..20}; do
  if curl --compressed -fsS --max-time 10 http://127.0.0.1:3456/api/state -o "$STAGING/checked.json" &&
     node - "$STAGING/checked.json" <<'CHECK' &&
const state = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
if (!Array.isArray(state.assets) || !state.assets.length || !state.xlayer?.coverage) process.exit(1);
CHECK
     curl -fsS --max-time 10 http://127.0.0.1:8010/api/health/ready -o /dev/null; then
    trap - ERR
    pm2 save >/dev/null
    rm -rf "$STAGING"
    printf 'Market enrichment live; code rollback: %s\n' "$BACKUP"
    exit 0
  fi
  sleep 3
done
false
REMOTE

curl --compressed -fsS --max-time 20 https://cliperx.com/dashboard/api/health/ready -o /dev/null
printf 'Deployed market enrichment: https://cliperx.com/dashboard/\n'
