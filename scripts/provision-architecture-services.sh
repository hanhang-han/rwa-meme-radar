#!/usr/bin/env bash
# Run on the deployment host. Creates isolated, local-only project services.
set -Eeuo pipefail
cd /opt/memedashboard
exec 9>.releases/architecture-services.lock
flock -n 9
python3 - <<'PY'
import json,os,pathlib,secrets,socket
root=pathlib.Path('data/architecture');root.mkdir(parents=True,exist_ok=True)
settings=pathlib.Path('data/architecture-runtime.json')
if settings.exists():
 d=json.loads(settings.read_text())
 assert d.get('managedBy')=='cliperx-architecture-v1','Unknown settings owner'
else:
 for port in (5434,6388):
  try:
   with socket.create_connection(('127.0.0.1',port),timeout=.3):raise RuntimeError('Dedicated service port already occupied')
  except ConnectionRefusedError:pass
 pg_password=secrets.token_urlsafe(36);redis_password=secrets.token_urlsafe(36)
 d={'managedBy':'cliperx-architecture-v1','enabled':False,'dsn':f'postgresql://cliperx:{pg_password}@127.0.0.1:5434/cliperx',
    'redisUrl':f'redis://:{redis_password}@127.0.0.1:6388/0','schema':'cliperx_read_model',
    'prefix':'cliperx:read-model:v1','maxVerifiedAgeMs':6000,'pollSeconds':2}
 pg_env=root/'postgres.env'
 pg_env.write_text(f'POSTGRES_USER=cliperx\nPOSTGRES_DB=cliperx\nPOSTGRES_PASSWORD={pg_password}\nPOSTGRES_INITDB_ARGS=--data-checksums\n')
 pg_env.chmod(0o600)
 redis_config=root/'redis.conf'
 redis_config.write_text(f'bind 0.0.0.0\nprotected-mode yes\nport 6379\nrequirepass {redis_password}\nmaxmemory 128mb\nmaxmemory-policy allkeys-lru\nappendonly no\nsave ""\ntcp-keepalive 60\n')
 redis_config.chmod(0o600)
 settings.write_text(json.dumps(d,indent=2));settings.chmod(0o600)
print('Dedicated project settings prepared; secrets remain on host.')
PY
sudo -n chown 999:999 data/architecture/redis.conf
if ! sudo -n docker container inspect cliperx-postgres >/dev/null 2>&1; then
 sudo -n docker run --detach --pull never --name cliperx-postgres --restart unless-stopped \
  --label com.cliperx.owner=architecture-v1 --memory 512m --cpus 1 \
  -p 127.0.0.1:5434:5432 --env-file data/architecture/postgres.env \
  -v /opt/memedashboard/data/architecture/postgres:/var/lib/postgresql/data \
  postgres:16-alpine postgres -c shared_buffers=64MB -c max_connections=20 \
  -c work_mem=4MB -c max_wal_size=256MB -c wal_compression=on >/dev/null
fi
if ! sudo -n docker container inspect cliperx-redis >/dev/null 2>&1; then
 sudo -n docker run --detach --pull never --name cliperx-redis --restart unless-stopped \
  --label com.cliperx.owner=architecture-v1 --memory 192m --cpus 0.5 \
  -p 127.0.0.1:6388:6379 \
  -v /opt/memedashboard/data/architecture/redis.conf:/usr/local/etc/redis/redis.conf:ro \
  redis:7-alpine redis-server /usr/local/etc/redis/redis.conf >/dev/null
fi
for attempt in {1..30}; do
 if sudo -n docker exec cliperx-postgres pg_isready -U cliperx -d cliperx >/dev/null 2>&1; then break; fi
 sleep 1
done
sudo -n docker exec cliperx-postgres pg_isready -U cliperx -d cliperx
printf 'Dedicated Redis/PostgreSQL containers prepared; public read switch stays disabled.\n'
