#!/bin/bash
set -euo pipefail
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="$HOME/.ssh/id_tencent"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
PKG=$(mktemp /tmp/memedashboard-release.XXXXXX)
trap 'rm -f "$PKG"' EXIT
npm run check
npm test
(cd web && VITE_BASE=/dashboard/ npm run build)
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$PKG" src public scripts tests server-py web/dist contracts package.json package-lock.json tsconfig.json ecosystem.config.cjs DATA.md IMPLEMENTATION.md PRODUCT_DESIGN.md OPTIMIZATION_PLAN_V2.md .env.example
scp "${SSH_OPTS[@]}" "$PKG" "$SERVER:/tmp/memedashboard-release.tar.gz"
ssh "${SSH_OPTS[@]}" "$SERVER" 'bash -s' <<'REMOTE'
set -euo pipefail
export PATH="/www/server/nodejs/v22.22.0/bin:$PATH"
cd /opt/memedashboard
mkdir -p .releases data
BACKUP=".releases/before-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
tar czf "$BACKUP" src public scripts package.json package-lock.json tsconfig.json ecosystem.config.cjs
printf 'Rollback archive: %s\n' "$BACKUP"
node --input-type=module -e 'import {existsSync} from "node:fs"; import {DatabaseSync} from "node:sqlite"; if(existsSync("data/research.sqlite")){const db=new DatabaseSync("data/research.sqlite"); db.prepare("VACUUM INTO ?").run(process.argv[1]+".sqlite");db.close()}' "$BACKUP"
# Save a real sample before restarting. Its original timestamps are retained.
# /api/state does not include leadStore/verificationStore/radarArchive; a bare
# mv wiped those process-restore stores on every deploy. Merge instead.
curl --compressed -fsS --max-time 15 http://127.0.0.1:3456/api/state -o data/state.json.next
node -e '
const fs = require("fs");
const next = JSON.parse(fs.readFileSync("data/state.json.next", "utf8"));
if (!Array.isArray(next.assets) || !next.assets.length) process.exit(1);
let prev = {};
try { prev = JSON.parse(fs.readFileSync("data/state.json", "utf8")); } catch {}
if (Array.isArray(prev.leadStore) && prev.leadStore.length) next.leadStore = prev.leadStore;
if (Array.isArray(prev.verificationStore) && prev.verificationStore.length) next.verificationStore = prev.verificationStore;
if (Array.isArray(prev.radarArchive)) next.radarArchive = prev.radarArchive;
fs.writeFileSync("data/state.json", JSON.stringify(next));
'
rm -f data/state.json.next
RESTARTED=0
rollback() {
  echo 'Deploy failed; restoring previous code.'
  pm2 stop memedashboard || true
  tar xzf "$BACKUP"
  if [ -f "$BACKUP.sqlite" ]; then rm -f data/research.sqlite-wal data/research.sqlite-shm; cp "$BACKUP.sqlite" data/research.sqlite; fi
  pm2 start ecosystem.config.cjs --only memedashboard
  pm2 save
}
trap rollback ERR
tar xzf /tmp/memedashboard-release.tar.gz
npm install --include=dev --no-audit --no-fund
npm run check
npm test
RESTARTED=1
# PM2 reload retains an existing shell script path even when the interpreter
# changes. Recreate this one process from the explicit app configuration.
pm2 delete memedashboard
pm2 start ecosystem.config.cjs --only memedashboard
cd /opt/memedashboard/server-py
/usr/bin/python3.11 -m venv .venv 2>/dev/null || true
.venv/bin/pip install -q -r requirements.txt 2>&1 | tail -1
cd /opt/memedashboard
pm2 restart pyradar 2>/dev/null || pm2 start "/opt/memedashboard/server-py/.venv/bin/python -m uvicorn app.main:app --app-dir server-py --host 127.0.0.1 --port 8010" --name pyradar --cwd /opt/memedashboard
pm2 save
for attempt in {1..12}; do
  if curl --compressed -fsS --max-time 5 http://127.0.0.1:3456/api/state -o /tmp/memedashboard-deployed-state.json; then
    if node -e 'const s=JSON.parse(require("fs").readFileSync("/tmp/memedashboard-deployed-state.json","utf8")); if(!s.okx||!s.xlayer?.coverage||s.relationTypes?.length!==6||!s.assets?.length)process.exit(1); console.log(JSON.stringify({assets:s.assets.length,radar:s.radar?.tokens?.length,okx:s.okx.status,xlayer:s.xlayer.status,relations:s.xlayer.relations.length}))'; then
      trap - ERR
      exit 0
    fi
  fi
  sleep 2
done
false
REMOTE
curl --compressed -fsS --max-time 15 http://129.226.135.20/workspace.js -o /dev/null
curl --compressed -fsS --max-time 15 http://129.226.135.20/xlayer-workspace.js -o /dev/null
printf 'Deployed: http://129.226.135.20/\n'
