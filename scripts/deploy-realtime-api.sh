#!/usr/bin/env bash
# Release detail-trade stream filtering and per-pool candle freshness only.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_SERVER:-ubuntu@129.226.135.20}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_tencent}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -i "$SSH_KEY")
PACKAGE=$(mktemp /tmp/cliperx-realtime-api.XXXXXX.tar.gz)
trap 'rm -f "$PACKAGE"' EXIT
FILES=(server-py/app/api/candles.py server-py/app/api/stream.py)
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$PACKAGE" "${FILES[@]}"
NEW_CANDLES=$(shasum -a 256 "${FILES[0]}" | cut -d' ' -f1)
NEW_STREAM=$(shasum -a 256 "${FILES[1]}" | cut -d' ' -f1)
scp "${SSH_OPTS[@]}" "$PACKAGE" "$SERVER:/tmp/cliperx-realtime-api.tar.gz"

ssh "${SSH_OPTS[@]}" "$SERVER" bash -s -- "$NEW_CANDLES" "$NEW_STREAM" <<'REMOTE'
set -Eeuo pipefail
export PATH=/www/server/nodejs/v22.22.0/bin:$PATH
cd /opt/memedashboard
OLD_CANDLES=ad9d0b29627bf9b5ad51155592adad8640952bfaf17b3f7a1ff0f213e412007a
OLD_STREAM=9f64388db0866defcc23eee65f6e44b2211d3d598c80d611cbbdf9a5515b7225
CANDLES=server-py/app/api/candles.py
STREAM=server-py/app/api/stream.py
test "$(sha256sum "$CANDLES" | cut -d' ' -f1)" = "$OLD_CANDLES"
test "$(sha256sum "$STREAM" | cut -d' ' -f1)" = "$OLD_STREAM"
test "$(pm2 pid pyradar-worker)" -gt 0
test "$(pm2 pid pyradar-projection)" -gt 0
worker_pid=$(pm2 pid pyradar-worker)
projection_pid=$(pm2 pid pyradar-projection)
staging=$(mktemp -d .releases/realtime-api-staging.XXXXXX)
backup=".releases/realtime-api-before-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$backup/server-py/app/api"
tar xzf /tmp/cliperx-realtime-api.tar.gz -C "$staging"
test "$(sha256sum "$staging/$CANDLES" | cut -d' ' -f1)" = "$1"
test "$(sha256sum "$staging/$STREAM" | cut -d' ' -f1)" = "$2"
server-py/.venv/bin/python -m py_compile "$staging/$CANDLES" "$staging/$STREAM"
cp -p "$CANDLES" "$STREAM" "$backup/server-py/app/api/"
changed=0
complete=0
finish() {
  result=$?
  trap - EXIT
  if [ "$changed" -eq 1 ] && [ "$complete" -ne 1 ]; then
    pm2 stop pyradar >/dev/null 2>&1 || true
    cp -p "$backup/$CANDLES" "$CANDLES"
    cp -p "$backup/$STREAM" "$STREAM"
    pm2 restart pyradar >/dev/null 2>&1 || true
    printf 'Realtime API rolled back from %s\n' "$backup" >&2
  fi
  rm -rf "$staging"
  exit "$result"
}
trap finish EXIT
pm2 stop pyradar >/dev/null
changed=1
cp -p "$staging/$CANDLES" "$CANDLES"
cp -p "$staging/$STREAM" "$STREAM"
pm2 restart pyradar >/dev/null
for attempt in {1..36}; do
  if curl -fsS --max-time 8 http://127.0.0.1:8010/api/health/ready -o /dev/null &&
     test "$(sha256sum "$CANDLES" | cut -d' ' -f1)" = "$1" &&
     test "$(sha256sum "$STREAM" | cut -d' ' -f1)" = "$2" &&
     test "$(pm2 pid pyradar-worker)" = "$worker_pid" &&
     test "$(pm2 pid pyradar-projection)" = "$projection_pid" &&
     server-py/.venv/bin/python - <<'PY'
import urllib.request
url='http://127.0.0.1:8010/api/stream?trades=196%3A0xabc&snapshot=false&protocol=1'
with urllib.request.urlopen(url,timeout=8) as response:
    assert response.status==200
    assert response.headers.get_content_type()=='text/event-stream'
    assert b'event: hello' in response.readline()+response.readline()
PY
  then
    complete=1
    pm2 save >/dev/null
    printf 'Realtime API live; code rollback: %s\n' "$backup"
    exit 0
  fi
  sleep 5
done
false
REMOTE

curl --compressed -fsS --max-time 20 https://cliperx.com/dashboard/api/health/ready -o /dev/null
