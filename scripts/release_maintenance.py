#!/usr/bin/env python3
"""Bound release backups without touching live data; dry-run is the default."""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile

MIN_ROLLBACK_POINTS = 2
MAX_BACKUP_BYTES = 8 * 1024**3


def retention(directory: Path, now: dt.datetime):
    files = sorted((p for p in directory.glob('before-*.tar.gz.sqlite')
                    if re.fullmatch(r'before-\d{8}T\d{6}Z\.tar\.gz\.sqlite',p.name) and not p.is_symlink()),
                   key=lambda p:p.name,reverse=True)
    keep = set(files[:MIN_ROLLBACK_POINTS])
    days, months = set(),set()
    for p in files:
        stamp = dt.datetime.strptime(p.name[7:23],'%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc)
        # One daily backup for the last week; one older monthly checkpoint.
        if (now-stamp).days<=7 and stamp.date() not in days:
            keep.add(p);days.add(stamp.date())
        elif (now-stamp).days>7 and stamp.strftime('%Y-%m') not in months:
            keep.add(p);months.add(stamp.strftime('%Y-%m'))
    # Count/age rules do not override a capacity ceiling. Always preserve the
    # two newest verified rollback points, even if they alone exceed it.
    for p in reversed(files):
        if sum(x.stat().st_size for x in keep)<=MAX_BACKUP_BYTES:break
        if p in keep and p not in files[:MIN_ROLLBACK_POINTS]:keep.remove(p)
    return files,keep


def maintain(root: Path, apply=False):
    directory = root.resolve()/'.releases'
    directory.mkdir(exist_ok=True)
    files,keep = retention(directory,dt.datetime.now(dt.timezone.utc))
    verified=[]
    cache_path=directory/'verified-backups.json'
    try:cache=json.loads(cache_path.read_text())
    except (OSError,ValueError):cache={}
    if len(files)>=MIN_ROLLBACK_POINTS:
        for p in files[:MIN_ROLLBACK_POINTS]:
            code=Path(str(p)[:-7])
            with tarfile.open(code,'r:gz') as archive:
                if not any(m.name.endswith('package.json') for m in archive.getmembers()):
                    raise RuntimeError('Rollback archive missing package.json')
            fingerprint={'size':p.stat().st_size,'mtimeNs':p.stat().st_mtime_ns}
            if cache.get(p.name)!=fingerprint:
                db=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
                try:
                    if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                        raise RuntimeError('Rollback database failed integrity check')
                finally:db.close()
                cache[p.name]=fingerprint
            verified.append(p.name)
    remove=[p for p in files if p not in keep] if len(verified)==MIN_ROLLBACK_POINTS else []
    result={'apply':apply,'verified':verified,'keep':[p.name for p in files if p in keep],
            'remove':[p.name for p in remove],'reclaimBytes':sum(p.stat().st_size for p in remove)}
    if apply:
        # Manifest is durable before deletion. Tiny code archives are kept.
        manifest=directory/('retention-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
        manifest.write_text(json.dumps(result,indent=2)+'\n')
        cache_path.write_text(json.dumps(cache,indent=2)+'\n')
        for p in remove:p.unlink()
    usage=shutil.disk_usage(root)
    result['freeBytes']=usage.free
    result['freePercent']=round(usage.free/usage.total*100,2)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=Path('/opt/memedashboard'))
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    print(json.dumps(maintain(args.root,args.apply),indent=2))
