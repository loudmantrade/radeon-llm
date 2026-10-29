#!/usr/bin/python3
"""Opt-in experimental reuse profile with clean cache namespace and full rollback."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
from apply_indexed_cache import MODULES,copy_snapshot

ROOT=Path(__file__).resolve().parent.parent
RUNTIME=Path.home()/'Library/Application Support/radeon-llm'


def manager(action):
    # Use the current manager; it can check readiness without kill(pid, 0).
    subprocess.run(['/usr/bin/python3',str(ROOT/'server/daemon.py'),action],check=True)


def prepare_runtime():
    subprocess.run(['/usr/bin/python3',str(RUNTIME/'server/daemon.py'),'prepare'],check=True)


def rollback(backup):
    manager('stop')
    cache=RUNTIME/'cache/kv'
    if cache.exists():shutil.move(str(cache),str(backup/('experimental-cache-'+str(time.time_ns()))))
    if (backup/'kv').exists():shutil.copytree(backup/'kv',cache,copy_function=copy_snapshot)
    target=RUNTIME/'experimental/shift-active/bin'
    if (backup/'previous-shift-active-bin').exists():
        if target.exists():shutil.move(str(target),str(backup/('new-build-'+str(time.time_ns()))))
        shutil.copytree(backup/'previous-shift-active-bin',target,symlinks=True)
    for name in MODULES+['config.json']:
        if (backup/name).exists():shutil.copy2(backup/name,RUNTIME/'server'/name)
    if (backup/'source-config.json').exists():shutil.copy2(backup/'source-config.json',ROOT/'server/config.json')
    prepare_runtime();manager('start')
    print('Previous daemon profile and clean cache restored.',flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--rollback',action='store_true');opts=parser.parse_args()
    old=json.loads((RUNTIME/'server/config.json').read_text())
    backups=RUNTIME/'backups';pointer=backups/'experimental-reuse-last-backup.json'
    if opts.rollback:
        backup=Path(json.loads(pointer.read_text())['directory'])
        assert backup.parent==backups and backup.is_dir()
        rollback(backup);return
    assert old.get('indexed_kv') and old.get('persistent_kv'),'Enable indexed persistent cache first'
    assert old['cache_type_k']==old['cache_type_v']=='q8_0'
    source=ROOT/'experimental/shift-active/bin'
    manifest=json.loads((source.parent/'manifest.json').read_text())
    assert manifest['opt_in']=='LLAMA_KV_SHIFT_ACTIVE_ONLY=1'
    assert (source/'llama-server').exists()
    config=dict(old)
    config.update(native_binary='experimental/shift-active/bin/llama-server',
                  cache_reuse_tokens=64,kv_shift_active_only=True,kv_reuse_mode='experimental-shift')
    if opts.dry_run:
        print(json.dumps({'context':config['context'],'K':config['cache_type_k'],'V':config['cache_type_v'],
            'mode':'experimental-shift','min_chunk_tokens':64,'active_only_shift':True,
            'new_cache_namespace':True,'backup_and_rollback':True,
            'automatic_checks':['API/tools','global chunk reuse and decode speed','indexed cache restart']},indent=2))
        return
    # Stage all native files before interrupting the current daemon.
    staging=RUNTIME/'experimental'/('reuse-stage-'+str(time.time_ns()))
    shutil.copytree(source,staging,symlinks=True)
    for path in staging.iterdir():
        if path.is_symlink() or (path.name!='llama-server' and path.suffix!='.dylib'):continue
        listing=subprocess.check_output(['/usr/bin/otool','-l',str(path)],text=True)
        rpaths=re.findall(r'cmd LC_RPATH\n\s*cmdsize \d+\n\s*path (.*?) \(offset',listing)
        for rpath in rpaths:
            if '/Documents/' in rpath:
                subprocess.run(['/usr/bin/install_name_tool','-delete_rpath',rpath,str(path)],check=True)
        if '@loader_path' not in rpaths:
            subprocess.run(['/usr/bin/install_name_tool','-add_rpath','@loader_path',str(path)],check=True)
    backup=backups/('before-experimental-reuse-'+str(time.time_ns()))
    backup.mkdir(parents=True,mode=0o700)
    for name in MODULES+['config.json']:
        if (RUNTIME/'server'/name).exists():shutil.copy2(RUNTIME/'server'/name,backup/name)
    shutil.copy2(ROOT/'server/config.json',backup/'source-config.json')
    try:
        manager('stop')
    except BaseException:
        shutil.rmtree(staging,ignore_errors=True)
        raise
    cache=RUNTIME/'cache/kv'
    backed_up=False
    try:
        if cache.exists():shutil.move(str(cache),str(backup/'kv'))
        backed_up=True
        pointer.write_text(json.dumps({'directory':str(backup)},indent=2)+'\n')
        print('Previous profile/cache: '+str(backup),flush=True)
        target=RUNTIME/'experimental/shift-active/bin'
        if target.exists():shutil.move(str(target),str(backup/'previous-shift-active-bin'))
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.move(str(staging),str(target))
        for name in MODULES:shutil.copy2(ROOT/'server'/name,RUNTIME/'server'/name)
        (RUNTIME/'server/config.json').write_text(json.dumps(config,indent=2)+'\n')
        log_path=RUNTIME/'logs/server.stderr.log'
        offset=log_path.stat().st_size if log_path.exists() else 0
        prepare_runtime();manager('start')
        subprocess.run([str(ROOT/'check-api.command')],check=True)
        subprocess.run(['/usr/bin/python3',str(ROOT/'server/check_experimental_reuse.py')],check=True)
        with log_path.open('rb') as stream:
            stream.seek(offset);startup=stream.read().decode(errors='replace')
        assert 'active-only K-shift, cells =' in startup,'Native active-only shift marker missing'
        subprocess.run([str(ROOT/'check-indexed-cache.command')],check=True)
        (ROOT/'server/config.json').write_text(json.dumps(config,indent=2)+'\n')
        (ROOT/'logs/experimental-reuse-install.json').write_text(json.dumps({
            'enabled':True,'mode':'experimental-shift','context':config['context'],'backup':str(backup),
            'min_chunk_tokens':64,'active_only_shift':True,'api_checked':True,
            'active_only_shift_confirmed':True,
            'restart_cache_checked':True,'quality_guaranteed':False},indent=2)+'\n')
        print('EXPERIMENTAL chunk reuse enabled. API, decode speed and cache restart checks passed.',flush=True)
    except BaseException:
        print('Activation/check failed; restoring previous daemon...',flush=True)
        if backed_up:rollback(backup)
        else:manager('start')
        raise


if __name__=='__main__':main()
