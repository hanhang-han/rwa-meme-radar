#!/usr/bin/env bash
# Release the reviewed dashboard build and its two server-side copy templates.
# No collector, projection, database, config, or unrelated source file is shipped.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

BASELINE="${1:?Usage: $0 BASELINE.json [--prepare-only]}"
if [ "${2:-}" != "" ] && [ "${2:-}" != "--prepare-only" ]; then
  printf 'Usage: %s BASELINE.json [--prepare-only]\n' "$0" >&2
  exit 2
fi
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=10
  -o ServerAliveCountMax=3 -o IdentitiesOnly=yes -i "${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}")
PACKAGE=$(mktemp /tmp/cliperx-ui-copy.XXXXXX.tar.gz)
MANIFEST=$(mktemp /tmp/cliperx-ui-copy.XXXXXX.json)
trap 'rm -f "$PACKAGE" "$MANIFEST"' EXIT

# The baseline must be captured from production immediately before this run:
# {"server-py/app/asset_facts.py":"<sha256>",
#  "server-py/app/briefing.py":"<sha256>",
#  "web/dist/index.html":"<sha256>"}
python3 - "$BASELINE" "$MANIFEST" "$PACKAGE" <<'PY'
import hashlib
import json
import pathlib
import re
import sys
import tarfile

baseline = json.loads(pathlib.Path(sys.argv[1]).read_text())
source = ('server-py/app/asset_facts.py', 'server-py/app/briefing.py')
required = set(source) | {'web/dist/index.html'}
assert isinstance(baseline, dict) and set(baseline) == required, 'Baseline needs exactly the three listed files'
assert all(isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v) for v in baseline.values())
dist = pathlib.Path('web/dist')
assert (dist / 'index.html').is_file(), 'Build with VITE_BASE=/dashboard/ first'
assert '/dashboard/assets/' in (dist / 'index.html').read_text(), 'Wrong dashboard base path'
paths = [pathlib.Path(p) for p in source]
paths += sorted(p for p in dist.rglob('*') if p.is_file())
assert len(paths) > len(source) + 1, 'Dashboard build has no assets'
assert all(p.is_file() and not p.is_symlink() for p in paths), 'Release contains a symlink'
hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
manifest = {'before': baseline, 'after': hashes}
pathlib.Path(sys.argv[2]).write_text(json.dumps(manifest, sort_keys=True))
with tarfile.open(sys.argv[3], 'w:gz') as archive:
    for path in paths:
        archive.add(path, arcname=str(path), recursive=False)
    archive.add(sys.argv[2], arcname='release.json', recursive=False)
print(f'Prepared {len(paths)} files: 2 server copy files and {len(paths)-2} dashboard build files')
print(f'New index SHA-256: {hashes["web/dist/index.html"]}')
PY

if [ "${2:-}" = "--prepare-only" ]; then
  printf 'Whitelist package validated locally; no server connection was made.\n'
  exit 0
fi

PACKAGE_SHA=$(shasum -a 256 "$PACKAGE" | awk '{print $1}')
REMOTE_PACKAGE="/tmp/cliperx-ui-copy-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
scp "${SSH_OPTS[@]}" "$PACKAGE" "$SERVER:$REMOTE_PACKAGE"
ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$REMOTE_PACKAGE" "$PACKAGE_SHA" <<'REMOTE'
set -Eeuo pipefail
export PATH="/www/server/nodejs/v22.22.0/bin:$PATH"
PACKAGE="$1"
PACKAGE_SHA="$2"
cd /opt/memedashboard
test "$(sha256sum "$PACKAGE" | awk '{print $1}')" = "$PACKAGE_SHA"
test -x server-py/.venv/bin/python
pm2 describe pyradar >/dev/null
pm2 describe pyradar-worker >/dev/null
pm2 describe pyradar-projection >/dev/null
test "$(pm2 pid pyradar)" -gt 0
test "$(pm2 pid pyradar-worker)" -gt 0
PROJECTION_PID=$(pm2 pid pyradar-projection)
test "$PROJECTION_PID" -gt 0
curl -fsS --max-time 10 http://127.0.0.1:8010/api/health/ready -o /dev/null

mkdir -p .releases
STAGING=$(mktemp -d .releases/ui-copy-staging.XXXXXX)
BACKUP=".releases/ui-copy-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir "$BACKUP"
SERVICES_TOUCHED=0
CODE_TOUCHED=0
WEB_MOVED=0
SUCCEEDED=0
finish() {
  result=$?
  trap - EXIT
  set +e
  rollback_failed=0
  if [ "$SUCCEEDED" -eq 0 ] && [ "$SERVICES_TOUCHED" -eq 1 ]; then
    pm2 stop pyradar-worker pyradar >/dev/null 2>&1 || true
    if [ "$CODE_TOUCHED" -eq 1 ]; then
      cp -p "$BACKUP/asset_facts.py" server-py/app/asset_facts.py || rollback_failed=1
      cp -p "$BACKUP/briefing.py" server-py/app/briefing.py || rollback_failed=1
    fi
    if [ "$WEB_MOVED" -eq 1 ]; then
      if [ -d web/dist ]; then mv web/dist "$BACKUP/failed-web-dist" || rollback_failed=1; fi
      mv "$BACKUP/web-dist" web/dist || rollback_failed=1
    fi
    pm2 restart pyradar >/dev/null 2>&1 || rollback_failed=1
    pm2 restart pyradar-worker >/dev/null 2>&1 || rollback_failed=1
    ready=0
    for attempt in {1..30}; do
      if curl -fsS --max-time 5 http://127.0.0.1:8010/api/health/ready -o /dev/null 2>/dev/null; then
        ready=1
        break
      fi
      sleep 2
    done
    if [ "$ready" -ne 1 ]; then rollback_failed=1; fi
    printf 'Release rolled back from %s\n' "$BACKUP" >&2
  fi
  rm -rf "$STAGING" || rollback_failed=1
  rm -f "$PACKAGE" || rollback_failed=1
  if [ "$rollback_failed" -ne 0 ]; then
    printf 'Automatic rollback incomplete; inspect %s and services.\n' "$BACKUP" >&2
    exit 2
  fi
  exit "$result"
}
trap finish EXIT

tar xzf "$PACKAGE" -C "$STAGING"
server-py/.venv/bin/python - "$STAGING" "$BACKUP" <<'PY'
import hashlib
import json
import pathlib
import py_compile
import shutil
import sys

stage, backup = map(pathlib.Path, sys.argv[1:])
manifest = json.loads((stage / 'release.json').read_text())
source = ('server-py/app/asset_facts.py', 'server-py/app/briefing.py')
required = set(source) | {'web/dist/index.html'}
assert set(manifest['before']) == required
assert set(manifest['after']) == {str(p.relative_to(stage)) for p in stage.rglob('*') if p.is_file()} - {'release.json'}
assert set(manifest['after']) >= required
for name, expected in manifest['after'].items():
    path = stage / name
    assert path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
for name, expected in manifest['before'].items():
    path = pathlib.Path(name)
    assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected, f'Production baseline changed: {name}'
for name in source:
    py_compile.compile(str(stage / name), doraise=True)
    shutil.copy2(name, backup / pathlib.Path(name).name)
shutil.copy2(stage / 'release.json', backup / 'release.json')
print('Production baseline and staged payload verified; source backups saved')
PY

# The two templates are loaded by both API and worker. Projection is untouched.
SERVICES_TOUCHED=1
pm2 stop pyradar-worker pyradar >/dev/null
CODE_TOUCHED=1
cp -p "$STAGING/server-py/app/asset_facts.py" server-py/app/asset_facts.py
cp -p "$STAGING/server-py/app/briefing.py" server-py/app/briefing.py
pm2 restart pyradar >/dev/null
for attempt in {1..120}; do
  if curl -fsS --max-time 4 http://127.0.0.1:8010/api/health/ready -o /dev/null 2>/dev/null; then break; fi
  if [ "$attempt" -eq 120 ]; then false; fi
  sleep 3
done
pm2 restart pyradar-worker >/dev/null
for attempt in {1..24}; do
  if curl --compressed -fsS --max-time 10 'http://127.0.0.1:8010/api/dashboard?view=overview' -o "$STAGING/overview.json" &&
     curl --compressed -fsS --max-time 10 'http://127.0.0.1:8010/api/ai/briefing?lang=zh' -o "$STAGING/briefing.json" &&
     server-py/.venv/bin/python - "$STAGING/overview.json" "$STAGING/briefing.json" <<'PY'
import json, sys
overview, briefing = (json.load(open(p)) for p in sys.argv[1:])
assert overview['unified']['snapshotScope'] == 'overview'
assert 'metrics' in overview['unified']
assert isinstance(briefing, dict) and 'status' in briefing and 'text' in briefing
PY
  then break; fi
  if [ "$attempt" -eq 24 ]; then false; fi
  sleep 3
done
test "$(pm2 pid pyradar-worker)" -gt 0
test "$(pm2 pid pyradar-projection)" = "$PROJECTION_PID"

# Preserve old versioned assets for browser tabs opened before this switch.
if [ -d web/dist/assets ]; then cp -an web/dist/assets/. "$STAGING/web/dist/assets/"; fi
mv web/dist "$BACKUP/web-dist"
WEB_MOVED=1
mv "$STAGING/web/dist" web/dist
EXPECTED_INDEX_SHA=$(server-py/.venv/bin/python - "$BACKUP/release.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))['after']['web/dist/index.html'])
PY
)
test "$(sha256sum web/dist/index.html | awk '{print $1}')" = "$EXPECTED_INDEX_SHA"
for attempt in {1..8}; do
  rm -f "$STAGING/public-index.html"
  curl --compressed -fsS --max-time 20 https://cliperx.com/dashboard/ -o "$STAGING/public-index.html" || true
  if [ -f "$STAGING/public-index.html" ] &&
     [ "$(sha256sum "$STAGING/public-index.html" | awk '{print $1}')" = "$EXPECTED_INDEX_SHA" ]; then break; fi
  if [ "$attempt" -eq 8 ]; then false; fi
  sleep 3
done
ASSET_PATH=$(server-py/.venv/bin/python - "$STAGING/public-index.html" <<'PY'
import pathlib, re, sys
html = pathlib.Path(sys.argv[1]).read_text()
match = re.search(r'/dashboard/assets/[^"\s<>]+\.js', html)
assert match, 'Public index has no dashboard script'
print(match.group(0))
PY
)
curl -fsS --max-time 20 "https://cliperx.com$ASSET_PATH" -o /dev/null
curl -fsS --max-time 15 https://cliperx.com/dashboard/api/health/ready -o /dev/null
curl --compressed -fsS --max-time 15 'https://cliperx.com/dashboard/api/ai/briefing?lang=zh' -o /dev/null
test "$(pm2 pid pyradar-projection)" = "$PROJECTION_PID"
pm2 save >/dev/null
SUCCEEDED=1
printf 'UI and copy live: https://cliperx.com/dashboard/\nRollback: %s\n' "$BACKUP"
REMOTE
