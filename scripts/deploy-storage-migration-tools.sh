#!/usr/bin/env bash
# Publish only the opt-in migration CLI. No process/config/database cutover.
set -Eeuo pipefail
cd /opt/memedashboard
export STORAGE_MIGRATION_CANDIDATE
STORAGE_MIGRATION_CANDIDATE=$(realpath "${1:?Pass checksum-reviewed candidate directory}")
exec 9>.releases/pool-pipeline-repair.lock
exec 8>.releases/dashboard-web-only-deploy.lock
exec 7>.releases/architecture-deploy.lock
flock -n 9
flock -n 8
flock -n 7
server-py/.venv/bin/python - <<'PY'
import hashlib,json,os,shutil,time
from pathlib import Path
candidate=Path(os.environ['STORAGE_MIGRATION_CANDIDATE'])
manifest=json.loads((candidate/'release.json').read_text())
allowed={'server-py/app/storage_migration.py','server-py/migration-requirements.txt'}
assert set(manifest['files'])==allowed
for name,row in manifest['files'].items():
 path=Path(name)
 assert not path.is_symlink()
 assert (hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None)==row['before'],name+' source changed'
 assert hashlib.sha256((candidate/name).read_bytes()).hexdigest()==row['after'],name+' candidate mismatch'
 assert not path.with_name(path.name+'.migration-release').exists()
backup=Path('.releases')/('storage-migration-tools-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+str(os.getpid()))
backup.mkdir(mode=0o700)
shutil.copy2(candidate/'release.json',backup/'release.json')
for name,row in manifest['files'].items():
 path=Path(name)
 if path.exists():
  saved=backup/path;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,saved)
try:
 for name in manifest['files']:
  path=Path(name);temporary=path.with_name(path.name+'.migration-release')
  shutil.copy2(candidate/name,temporary);os.replace(temporary,path)
except BaseException:
 for name,row in manifest['files'].items():
  path=Path(name)
  if row['before'] is None:path.unlink(missing_ok=True)
  else:shutil.copy2(backup/path,path)
 raise
receipt={'backup':str(backup),'files':manifest['files'],'runtimeCutover':False,'processesRestarted':False}
(backup/'receipt.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps(receipt,indent=2))
PY
