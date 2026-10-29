#!/usr/bin/python3
"""Script-only indexed-cache upgrade, preserving native runtime and rollback data."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = Path.home() / 'Library/Application Support/radeon-llm'
MODULES = ['daemon.py', 'slot_cache.py', 'cache_index.py', 'tiered_cache.py', 'cache_proxy.py', 'platform_support.py']


def installed(action):
    subprocess.run(['/usr/bin/python3', str(RUNTIME/'server/daemon.py'), action], check=True)


def copy_snapshot(source, target):
    # Snapshot files are immutable: hardlinks preserve the backup without doubling GB of data.
    if str(source).endswith('.bin'):
        try:
            os.link(source, target)
            return str(target)
        except OSError:
            pass
    return shutil.copy2(source, target)


def rollback(backup):
    installed('stop')
    cache = RUNTIME/'cache/kv'
    if cache.exists():
        shutil.move(str(cache), str(backup/('indexed-cache-'+str(time.time_ns()))))
    if (backup/'kv').exists():
        shutil.copytree(backup/'kv', cache, copy_function=copy_snapshot)
    for name in MODULES+['config.json']:
        if (backup/name).exists():
            shutil.copy2(backup/name, RUNTIME/'server'/name)
    installed('prepare')
    installed('start')
    print('Previous daemon and cache restored.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--rollback', action='store_true')
    options = parser.parse_args()
    old_config = json.loads((RUNTIME/'server/config.json').read_text())
    new_config = dict(old_config)
    source_config = json.loads((ROOT/'server/config.json').read_text())
    for key in ['indexed_kv','kv_backend_port','kv_disk_mib','kv_hot_mib',
                'kv_max_entries','kv_ttl_days','kv_min_tokens']:
        new_config[key] = source_config[key]
    new_config['persistent_kv'] = True
    backups = RUNTIME/'backups'
    pointer = backups/'indexed-last-backup.json'
    if options.dry_run:
        print(json.dumps({'context_preserved': old_config['context'],
            'public_port': old_config['port'], 'backend_port': new_config['kv_backend_port'],
            'disk_mib': new_config['kv_disk_mib'], 'hot_mapped_mib': new_config['kv_hot_mib'],
            'entry_limit': new_config['kv_max_entries'], 'ttl_days':new_config['kv_ttl_days'],
            'native_runtime_changed':False, 'automatic_rollback':True}, indent=2))
        return
    if options.rollback:
        backup = Path(json.loads(pointer.read_text())['directory'])
        assert backup.parent == backups and backup.is_dir()
        rollback(backup)
        return
    for name in MODULES:
        assert (ROOT/'server'/name).exists(), name
    # Reject invalid limits before touching the running service.
    assert new_config['kv_disk_mib'] > 0 and new_config['kv_hot_mib'] >= 0
    assert new_config['kv_max_entries'] >= 1 and new_config['kv_ttl_days'] > 0
    assert new_config['kv_min_tokens'] >= 64
    assert new_config['kv_backend_port'] != old_config['port']
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backups/('before-indexed-'+str(time.time_ns()))
    backup.mkdir(mode=0o700)
    for name in MODULES+['config.json']:
        if (RUNTIME/'server'/name).exists():
            shutil.copy2(RUNTIME/'server'/name, backup/name)
    installed('stop')
    cache = RUNTIME/'cache/kv'
    backed_up = False
    try:
        if cache.exists():
            shutil.copytree(cache, backup/'kv', copy_function=copy_snapshot)
        backed_up = True
        pointer.write_text(json.dumps({'directory':str(backup)},indent=2)+'\n')
        print('Previous cache/profile: '+str(backup), flush=True)
        for name in MODULES:
            shutil.copy2(ROOT/'server'/name, RUNTIME/'server'/name)
        (RUNTIME/'server/config.json').write_text(json.dumps(new_config,indent=2)+'\n')
        installed('prepare')
        installed('start')
        subprocess.run([str(ROOT/'check-api.command')],check=True)
        subprocess.run([str(ROOT/'check-indexed-cache.command')],check=True)
        print('Indexed disk cache and bounded hot mmap tier enabled; API/restart checks passed.',flush=True)
    except BaseException:
        print('Upgrade/check failed; restoring previous daemon...',flush=True)
        if backed_up:
            rollback(backup)
        else:
            installed('start')
        raise


if __name__ == '__main__':
    main()
