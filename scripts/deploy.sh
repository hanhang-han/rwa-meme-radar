#!/bin/bash
set -euo pipefail
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="$HOME/.ssh/id_tencent"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
PKG=$(mktemp /tmp/memedashboard-release.XXXXXX)
trap 'rm -f "$PKG"' EXIT
npm run check
npm test
node --test web/tests/*.test.js
PYTHONPATH=server-py server-py/.venv/bin/python -m unittest discover -s server-py/tests
(cd web && VITE_BASE=/dashboard/ npm run build)
# Never ship a local .venv: the server rebuilds its own with python3.11.
# A macOS venv inside the tar silently replaced the server's Linux venv once
# and took pyradar down.
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$PKG" --exclude='server-py/.venv' --exclude='*/__pycache__' --exclude='*.pyc' src public scripts tests server-py web/dist contracts package.json package-lock.json tsconfig.json ecosystem.config.cjs DATA.md IMPLEMENTATION.md PRODUCT_DESIGN.md OPTIMIZATION_PLAN_V2.md .env.example
scp "${SSH_OPTS[@]}" "$PKG" "$SERVER:/tmp/memedashboard-release.tar.gz"
scp "${SSH_OPTS[@]}" scripts/release_maintenance.py "$SERVER:/tmp/cliperx-release-maintenance.py"
ssh "${SSH_OPTS[@]}" "$SERVER" 'bash -s' <<'REMOTE'
set -euo pipefail
export PATH="/www/server/nodejs/v22.22.0/bin:$PATH"
cd /opt/memedashboard
mkdir -p .releases data
python3 /tmp/cliperx-release-maintenance.py --root /opt/memedashboard --apply
python3 - <<'PY'
import shutil,os
usage=shutil.disk_usage('.')
required=max(2*os.path.getsize('data/research.sqlite')+512*1024**2, int(usage.total*.1))
if usage.free<required:raise SystemExit('Insufficient disk for a verified rollback and deployment')
PY
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STAGING=$(mktemp -d /opt/memedashboard/.releases/staging.XXXXXX)
NEW_VENV=""
VENV_PROMOTED=0
cleanup_staging() {
  rm -rf "$STAGING"
  if [ -n "$NEW_VENV" ] && [ "$VENV_PROMOTED" -eq 0 ]; then rm -rf "$NEW_VENV"; fi
}
trap cleanup_staging EXIT
cp /tmp/memedashboard-release.tar.gz "$STAGING/release.tar.gz"
tar xzf "$STAGING/release.tar.gz" -C "$STAGING"
mkdir -p "$STAGING/.test-data/tmp"
NODE_CHANGED=1
if cmp -s package.json "$STAGING/package.json" && cmp -s package-lock.json "$STAGING/package-lock.json" && [ -d node_modules ]; then
  NODE_CHANGED=0
  ln -s /opt/memedashboard/node_modules "$STAGING/node_modules"
  echo 'Node dependency manifests unchanged; using installed dependencies for staging checks.'
else
  (cd "$STAGING" && npm ci --include=dev --no-audit --no-fund)
fi
PYTHON_CHANGED=1
if cmp -s server-py/requirements.txt "$STAGING/server-py/requirements.txt" && [ -x server-py/.venv/bin/python ]; then
  PYTHON_CHANGED=0
  TEST_PYTHON=/opt/memedashboard/server-py/.venv/bin/python
  echo 'Python requirements unchanged; using installed venv for staging checks.'
else
  # Keep this final path stable: moving a venv breaks its absolute shebangs.
  NEW_VENV="/opt/memedashboard/.releases/venv-$RELEASE_ID"
  /usr/bin/python3.11 -m venv "$NEW_VENV"
  "$NEW_VENV/bin/pip" install -q -r "$STAGING/server-py/requirements.txt"
  "$NEW_VENV/bin/pip" check
  "$NEW_VENV/bin/pip" freeze > "$NEW_VENV/validated-requirements.txt"
  TEST_PYTHON="$NEW_VENV/bin/python"
fi
# Validate the exact uploaded source while every production process is still
# running. No production .env or data is copied into staging; all test stores
# and temporary directories resolve inside this disposable release directory.
(
  cd "$STAGING"
  TEST_ENV=(env -i PATH="$PATH" HOME="$HOME" LANG="${LANG:-C.UTF-8}"
    NODE_ENV=test PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$STAGING/server-py"
    TMPDIR="$STAGING/.test-data/tmp" RESEARCH_DB="$STAGING/.test-data/research.sqlite"
    STREAM_LEDGER_PATH="$STAGING/.test-data/stream.sqlite"
    OKX_LEDGER_PATH="$STAGING/.test-data/okx-budget.sqlite")
  "${TEST_ENV[@]}" npm run check
  "${TEST_ENV[@]}" npm test
  "${TEST_ENV[@]}" "$TEST_PYTHON" -m unittest discover -s server-py/tests
)
echo 'Staging dependency installation and all server checks passed; preparing rollback.'
BACKUP=".releases/before-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
tar --exclude=server-py/.venv --exclude='*/__pycache__' -czf "$BACKUP" src public scripts server-py web/dist package.json package-lock.json tsconfig.json ecosystem.config.cjs
printf 'Rollback archive: %s\n' "$BACKUP"
# VACUUM INTO gives a transactionally consistent backup even while writers are
# alive. Verify it before entering the short stop/swap/start window.
node --input-type=module -e 'import {existsSync} from "node:fs"; import {DatabaseSync} from "node:sqlite"; if(existsSync("data/research.sqlite")){const db=new DatabaseSync("data/research.sqlite"); db.prepare("VACUUM INTO ?").run(process.argv[1]+".sqlite");db.close();const copy=new DatabaseSync(process.argv[1]+".sqlite",{readOnly:true});if(Object.values(copy.prepare("PRAGMA quick_check").get())[0]!=="ok")throw new Error("Rollback database failed integrity check");copy.close()}' "$BACKUP"
# Save a real sample before restarting. Its original timestamps are retained.
# /api/state does not include leadStore/verificationStore/radarArchive; merge
# the persistent stores rather than losing them on deployment.
curl --compressed -fsS --max-time 15 http://127.0.0.1:3456/api/state -o "$STAGING/state.json.next"
node - "$STAGING/state.json.next" <<'STATE'
const fs = require("fs");
const next = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
if (!Array.isArray(next.assets) || !next.assets.length) process.exit(1);
let prev = {};
try { prev = JSON.parse(fs.readFileSync("data/state.json", "utf8")); } catch {}
if (Array.isArray(prev.leadStore) && prev.leadStore.length) next.leadStore = prev.leadStore;
if (Array.isArray(prev.verificationStore) && prev.verificationStore.length) next.verificationStore = prev.verificationStore;
if (Array.isArray(prev.radarArchive)) next.radarArchive = prev.radarArchive;
fs.writeFileSync(process.argv[2], JSON.stringify(next));
STATE
# Stop/delete only this release's known processes. The projection process does
# not exist on pre-isolation releases, so first deployment must tolerate that.
release_processes=(memedashboard pyradar pyradar-worker pyradar-projection)
stop_release_processes() {
  for app_name in "${release_processes[@]}"; do
    if pm2 describe "$app_name" >/dev/null 2>&1; then pm2 stop "$app_name"; fi
  done
}
delete_release_processes() {
  for app_name in "${release_processes[@]}"; do
    if pm2 describe "$app_name" >/dev/null 2>&1; then pm2 delete "$app_name"; fi
  done
}
start_release_processes() {
  local app_names
  app_names=$(node -e 'const allowed=new Set(["memedashboard","pyradar","pyradar-worker","pyradar-projection"]);process.stdout.write(require("./ecosystem.config.cjs").apps.filter(app=>allowed.has(app.name)).map(app=>app.name).join(","))')
  test -n "$app_names"
  pm2 start ecosystem.config.cjs --only "$app_names"
}
NODE_SWAPPED=0
VENV_SWAPPED=0
rollback() {
  result=$?
  trap - ERR
  set +e
  echo 'Deploy failed; restoring previous code and dependencies.'
  stop_release_processes
  tar xzf "$BACKUP"
  if [ "$NODE_SWAPPED" -eq 1 ]; then
    rm -rf node_modules
    if [ -e "$BACKUP.node_modules" ] || [ -L "$BACKUP.node_modules" ]; then mv "$BACKUP.node_modules" node_modules; fi
  fi
  if [ "$VENV_SWAPPED" -eq 1 ]; then
    rm -rf server-py/.venv
    if [ -e "$BACKUP.venv" ] || [ -L "$BACKUP.venv" ]; then mv "$BACKUP.venv" server-py/.venv; fi
    VENV_PROMOTED=0
  fi
  # Recreate processes so interpreter changes are rolled back too. Keep the
  # live database: restoring an older backup would discard post-restart data.
  delete_release_processes
  rm -f data/worker-health.json data/projection-health.json
  start_release_processes
  pm2 save
  exit "$result"
}
trap rollback ERR
stop_release_processes
mv "$STAGING/state.json.next" data/state.json
cp data/state.json "$BACKUP.state.json"
tar xzf "$STAGING/release.tar.gz"
if [ "$NODE_CHANGED" -eq 1 ]; then
  if [ -e node_modules ] || [ -L node_modules ]; then mv node_modules "$BACKUP.node_modules"; fi
  NODE_SWAPPED=1
  mv "$STAGING/node_modules" node_modules
fi
if [ "$PYTHON_CHANGED" -eq 1 ]; then
  if [ -e server-py/.venv ] || [ -L server-py/.venv ]; then mv server-py/.venv "$BACKUP.venv"; fi
  VENV_SWAPPED=1
  ln -s "$NEW_VENV" server-py/.venv
  VENV_PROMOTED=1
fi
# PM2 reload retains an existing shell script path even when the interpreter
# changes. Recreate the processes from the explicit app configuration.
delete_release_processes
rm -f data/worker-health.json data/projection-health.json
start_release_processes
pm2 save
check_release_health() {
  curl --compressed -fsS --max-time 5 http://127.0.0.1:3456/api/state -o /tmp/memedashboard-deployed-state.json || return 1
  node -e 'const s=JSON.parse(require("fs").readFileSync("/tmp/memedashboard-deployed-state.json","utf8")); if(!s.okx||!s.xlayer?.coverage||s.relationTypes?.length!==6||!s.assets?.length)process.exit(1); console.log(JSON.stringify({assets:s.assets.length,radar:s.radar?.tokens?.length,okx:s.okx.status,xlayer:s.xlayer.status,relations:s.xlayer.relations.length}))' || return 1
  curl -fsS --max-time 10 http://127.0.0.1:8010/api/health >/dev/null || return 1
  curl -fsS --max-time 10 http://127.0.0.1:8010/api/health/ready >/dev/null || return 1
  curl -fsS --max-time 10 http://127.0.0.1:8010/api/health/data -o /tmp/cliperx-data-health.json || return 1
  node -e 'const h=JSON.parse(require("fs").readFileSync("/tmp/cliperx-data-health.json"));if(!h.worker?.ok||!h.projection?.ok||!h.coverage||!Object.keys(h.worker.tasks||{}).length)process.exit(1);console.log(JSON.stringify({dataStatus:h.status,issues:h.issues,coverage:h.coverage}))' || return 1
  curl -fsS --max-time 20 http://127.0.0.1:8010/api/dashboard -o /tmp/cliperx-python-check.json || return 1
  node -e 'const d=JSON.parse(require("fs").readFileSync("/tmp/cliperx-python-check.json"));if(!d.unified?.assets?.length||!d.unified?.sources?.some(s=>s.provider==="DexScreener"))process.exit(1);const stocks=d.unified.stockTokens||[];if(stocks.some(s=>["SLV.WAR","QQQ.BA"].includes(s.referenceSymbol)))throw new Error("Unverified equity identity escaped");if(!stocks.some(s=>s.provider==="Binance"))throw new Error("Binance shared quotes unavailable")' || return 1
  curl --compressed -fsS --max-time 15 http://129.226.135.20/workspace.js -o /dev/null || return 1
  curl --compressed -fsS --max-time 15 http://129.226.135.20/xlayer-workspace.js -o /dev/null || return 1
  curl --compressed -fsS --max-time 20 https://cliperx.com/dashboard/api/health/ready -o /dev/null || return 1
}
for attempt in {1..12}; do
  if check_release_health; then
    trap - ERR
    exit 0
  fi
  sleep 2
done
false
REMOTE
curl --compressed -fsS --max-time 15 http://129.226.135.20/workspace.js -o /dev/null
curl --compressed -fsS --max-time 15 http://129.226.135.20/xlayer-workspace.js -o /dev/null
curl --compressed -fsS --max-time 20 https://cliperx.com/dashboard/api/health/ready -o /dev/null
printf 'Deployed: https://cliperx.com/dashboard/\n'
