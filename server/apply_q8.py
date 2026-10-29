#!/usr/bin/python3
"""Install the tested FA-only runtime and Q8 profile, with automatic rollback."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = Path.home() / 'Library/Application Support/radeon-llm'
FILES = ['daemon.py', 'slot_cache.py', 'cache_index.py', 'tiered_cache.py', 'cache_proxy.py', 'platform_support.py', 'config.json']


def run(*args):
    subprocess.run(list(map(str, args)), check=True)


def installed(action):
    run('/usr/bin/python3', RUNTIME / 'server/daemon.py', action)


def restore(backup):
    installed('stop')
    old_binary = backup / 'previous-experimental-bin'
    if old_binary.exists():
        target = RUNTIME / 'experimental/bin'
        if target.exists():
            shutil.move(str(target), str(backup / ('q8-bin-' + str(time.time_ns()))))
        shutil.move(str(old_binary), str(target))
    for name in FILES:
        if (backup / name).exists():
            shutil.copy2(backup / name, RUNTIME / 'server' / name)
    cache = RUNTIME / 'cache/kv'
    if (backup / 'kv').exists():
        if cache.exists():
            shutil.move(str(cache), str(backup / ('q8-cache-' + str(time.time_ns()))))
        shutil.move(str(backup / 'kv'), str(cache))
    installed('prepare')
    installed('start')
    if (backup / 'opencode.json').exists():
        shutil.copy2(backup / 'opencode.json', ROOT / 'opencode.json')
    print('Previous daemon profile restored.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--rollback', action='store_true')
    options = parser.parse_args()
    manifest = json.loads((ROOT / 'experimental/manifest.json').read_text())
    assert manifest['opt_in'] == 'GGML_VK_AMD_MOLTENVK_FA_SHARED=1'
    config = json.loads((ROOT / 'server/config.json').read_text())
    context = config['context']
    assert context in (8192, 16384) and config['cache_type_k'] == config['cache_type_v'] == 'q8_0'
    assert config['flash_attention'] == 'on' and config['amd_shared_fa']
    assert config.get('fit') == 'off', 'Use fixed settings so GPU initialization diagnostics remain visible'
    assert config['native_binary'] == 'experimental/bin/llama-server'
    source = ROOT / 'experimental/bin'
    assert (source / 'llama-server').exists()
    assert (RUNTIME / 'server/daemon.py').exists(), 'Install the stable daemon first'
    backups = RUNTIME / 'backups'
    pointer = backups / 'q8-last-backup.json'
    if options.dry_run:
        print(json.dumps({'context': context, 'K': 'q8_0', 'V': 'q8_0',
                          'native_binary': config['native_binary'], 'FA_shared': True,
                          'fit': 'off',
                          'expected_kv_mib': 612 * context // 8192, 'backup_before_change': True,
                          'api_and_restart_checks': True, 'automatic_rollback_on_failure': True}, indent=2))
        return
    if options.rollback:
        backup = Path(json.loads(pointer.read_text())['directory'])
        assert backup.parent == backups and backup.is_dir()
        restore(backup)
        return

    # Complete the staging copy before stopping the live daemon.
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = RUNTIME / ('q8-stage-' + str(time.time_ns()))
    shutil.copytree(source, staging, symlinks=True)
    for path in staging.iterdir():
        if path.is_symlink() or (path.name != 'llama-server' and path.suffix != '.dylib'):
            continue
        listing = subprocess.check_output(['/usr/bin/otool', '-l', str(path)], text=True)
        rpaths = re.findall(r'cmd LC_RPATH\n\s*cmdsize \d+\n\s*path (.*?) \(offset', listing)
        for rpath in rpaths:
            if '/Documents/' in rpath:
                run('/usr/bin/install_name_tool', '-delete_rpath', rpath, path)
        if '@loader_path' not in rpaths:
            run('/usr/bin/install_name_tool', '-add_rpath', '@loader_path', path)
    backup = backups / ('before-q8-' + str(time.time_ns()))
    backup.mkdir(mode=0o700)
    for name in FILES:
        if (RUNTIME / 'server' / name).exists():
            shutil.copy2(RUNTIME / 'server' / name, backup / name)
    shutil.copy2(ROOT / 'opencode.json', backup / 'opencode.json')
    installed('stop')
    cache = RUNTIME / 'cache/kv'
    if cache.exists():
        shutil.move(str(cache), str(backup / 'kv'))
    pointer.write_text(json.dumps({'directory': str(backup)}, indent=2) + '\n')
    print('Previous profile and KV snapshot: ' + str(backup), flush=True)
    try:
        target = RUNTIME / 'experimental/bin'
        if target.exists():
            shutil.move(str(target), str(backup / 'previous-experimental-bin'))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(target))
        for name in FILES:
            shutil.copy2(ROOT / 'server' / name, RUNTIME / 'server' / name)
        log_path = RUNTIME / 'logs/server.stderr.log'
        offset = log_path.stat().st_size if log_path.exists() else 0
        installed('prepare')
        installed('start')
        startup = log_path.read_bytes()[offset:].decode(errors='replace')
        (ROOT / 'logs/q8-daemon-startup.log').write_text(startup)
        assert 'experimental AMD MoltenVK shared FA only = 1; other shuffle disabled' in startup, \
            'FA-only initialization marker missing; inspect logs/q8-daemon-startup.log'
        assert re.search(r'flash_attn\s*=\s*enabled', startup), 'Flash Attention not enabled'
        assert 'K (q8_0)' in startup and 'V (q8_0)' in startup, 'Actual cache types differ'
        assert 'offloaded 37/37' in startup, 'Weights not fully offloaded'
        assert re.search(r'n_ctx\s*=\s*%d\b' % context, startup), 'Actual context differs'
        groups = re.findall(r'graph[^\n]*splits = (\d+)(?: / (\d+))?', startup)
        splits = [int(x) for group in groups for x in group if x]
        assert splits and max(splits) <= 8, 'Substantial CPU fallback detected'
        for line in startup.splitlines():
            if any(term in line for term in ('KV buffer size', 'K (q8_0)', 'flash_attn ', 'n_ctx ')):
                print(line, flush=True)
        run(ROOT / 'check-api.command')
        run(ROOT / 'check-kv-cache.command')
        # Advertise the larger limit only after the server and restart checks pass.
        opencode_path = ROOT / 'opencode.json'
        opencode = json.loads(opencode_path.read_text())
        provider = opencode['providers']['radeon']
        provider['models'][config['alias']]['limit']['context'] = context
        if context > 8192:
            provider['settings']['timeout'] = max(provider['settings'].get('timeout', 0), 2400000)
        temporary = opencode_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(opencode, indent=2) + '\n')
        temporary.replace(opencode_path)
        report = {'passed': True, 'context': context, 'cache_type_k': 'q8_0', 'cache_type_v': 'q8_0',
                  'gpu_graph_splits': splits, 'backup': str(backup),
                  'api_passed': True, 'persistent_cache_restart_passed': True}
        (ROOT / 'logs/q8-daemon-install.json').write_text(json.dumps(report, indent=2) + '\n')
        print('Q8 daemon enabled at %d tokens; OpenCode limit updated; API and persistent KV restart checks passed.' % context, flush=True)
    except BaseException:
        print('Q8 installation/check failed; restoring previous daemon...', flush=True)
        restore(backup)
        raise


if __name__ == '__main__':
    main()
