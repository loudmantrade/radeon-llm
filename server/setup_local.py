"""Build and install the project for this host; no cross-platform binaries copied."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import urllib.request

import bootstrap
from platform_support import FileLock
from warm_opencode import private_json

ROOT = Path(__file__).resolve().parent.parent
SDK_URL = 'https://sdk.lunarg.com/sdk/download/1.3.296.0/mac/vulkansdk-macos-1.3.296.0.zip'
SDK_SHA = '393fd11f65a4001f12fd34fdd009c38045220ca3f735bc686d97822152b0f33c'
MVK_URL = 'https://github.com/KhronosGroup/MoltenVK/releases/download/v1.4.2/MoltenVK-macos.tar'
MVK_SHA = 'f95765a6229cb7b915990a2890ce12ebe36a730b021545d3d52ae69ce4c4024e'


def backend_for(system, machine, requested='auto'):
    if system not in ('Darwin', 'Windows'):
        raise RuntimeError('Setup supports macOS and Windows; this host is ' + system)
    if system == 'Windows' and machine.lower() not in ('amd64', 'x86_64'):
        raise RuntimeError('Windows setup currently requires x64 Python/Windows')
    backend = requested if requested != 'auto' else (
        'metal' if system == 'Darwin' and machine.lower() in ('arm64', 'aarch64') else 'vulkan')
    if system == 'Windows' and backend != 'vulkan':
        raise RuntimeError('Windows requires the Vulkan backend')
    return backend


def invoke(args, **kwargs):
    print('Running: ' + subprocess.list2cmdline([str(a) for a in args]), flush=True)
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def download_verified(url, sha, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and bootstrap.sha256(destination) == sha: return destination
    temporary = destination.with_suffix(destination.suffix + '.part')
    print('Downloading verified dependency: ' + url, flush=True)
    # LunarG rejects Python's default User-Agent. Identify this unattended client.
    request = urllib.request.Request(url, headers={'User-Agent': 'radeon-llm-setup/1.0',
                                                  'Accept': 'application/octet-stream'})
    with urllib.request.urlopen(request, timeout=120) as response, temporary.open('wb') as stream:
        shutil.copyfileobj(response, stream, length=4 * 2**20)
    if bootstrap.sha256(temporary) != sha:
        raise RuntimeError('Dependency SHA-256 mismatch: ' + str(temporary))
    os.replace(temporary, destination)
    return destination


def windows_sdk():
    candidates = [Path(os.environ['VULKAN_SDK'])] if os.environ.get('VULKAN_SDK') else []
    base = Path('C:/VulkanSDK')
    if base.exists(): candidates += sorted(base.iterdir(), key=lambda p: p.name, reverse=True)
    return next((p for p in candidates if (p / 'Bin/glslc.exe').is_file()
                 and (p / 'Include/vulkan/vulkan.h').is_file()), None)


def windows_compiler():
    if shutil.which('cl'): return True
    where = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft Visual Studio/Installer/vswhere.exe'
    if not where.is_file(): return False
    result = subprocess.run([str(where), '-latest', '-products', '*', '-requires',
                             'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath'],
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
    return result.returncode == 0 and bool(result.stdout.strip())


def refresh_windows_path():
    import winreg
    values = []
    for hive, key in [(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'),
                      (winreg.HKEY_CURRENT_USER, 'Environment')]:
        try:
            with winreg.OpenKey(hive, key) as handle: values.append(winreg.QueryValueEx(handle, 'Path')[0])
        except OSError: pass
    os.environ['PATH'] = os.path.expandvars(';'.join(values + [os.environ.get('PATH', '')]))


def winget(package, extra=()):
    if not shutil.which('winget'):
        raise RuntimeError('Install the missing dependency manually or install Microsoft App Installer (winget), then rerun setup')
    args = ['winget', 'install', '--id', package, '--exact', '--source', 'winget',
            '--accept-source-agreements', '--accept-package-agreements'] + list(extra)
    result = subprocess.run(args)
    if result.returncode not in (0, 3010, 1641):
        raise RuntimeError('winget failed for %s (exit %d)' % (package, result.returncode))
    if result.returncode in (3010, 1641):
        raise RuntimeError('Windows requires a restart to finish %s; reboot and rerun setup' % package)
    refresh_windows_path()


def dependencies(system, backend, no_deps=False, check=False):
    missing = [name for name in ('git', 'cmake') if not shutil.which(name)]
    if system == 'Windows':
        if not windows_compiler(): missing.append('msvc')
        if not windows_sdk(): missing.append('vulkan-sdk')
    else:
        if subprocess.run(['xcode-select', '-p'], capture_output=True).returncode:
            missing.append('xcode-clt')
    if check:
        if system == 'Darwin' and backend == 'vulkan':
            if not (ROOT / 'vulkan-sdk/macOS/bin/glslc').is_file(): missing.append('local Vulkan SDK')
            if not (ROOT / 'moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS/libMoltenVK.dylib').is_file():
                missing.append('local MoltenVK')
        print(json.dumps({'missing_dependencies': missing}, indent=2))
        return not missing
    if missing and no_deps:
        raise RuntimeError('Missing dependencies: ' + ', '.join(missing) + '; install them and rerun, or omit --no-deps')
    for name in missing:
        if system == 'Windows':
            package, extra = {
                'git': ('Git.Git', ()), 'cmake': ('Kitware.CMake', ()),
                'vulkan-sdk': ('KhronosGroup.VulkanSDK', ()),
                'msvc': ('Microsoft.VisualStudio.2022.BuildTools', ('--override',
                    '--wait --passive --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended'))
            }[name]
            winget(package, extra)
        elif name == 'xcode-clt':
            subprocess.run(['xcode-select', '--install'], check=False)
            raise RuntimeError('Finish installing Xcode Command Line Tools in the macOS dialog, then rerun setup')
        elif name == 'git':
            raise RuntimeError('Install Xcode Command Line Tools with xcode-select --install, then rerun setup')
        else:
            brew = shutil.which('brew')
            if not brew: raise RuntimeError('Install CMake from cmake.org or Homebrew from brew.sh, then rerun setup')
            invoke([brew, 'install', 'cmake'])
            prefix = subprocess.check_output([brew, '--prefix'], text=True).strip()
            os.environ['PATH'] = str(Path(prefix) / 'bin') + os.pathsep + os.environ.get('PATH', '')
    if system == 'Windows':
        if not windows_sdk() or not windows_compiler():
            raise RuntimeError('MSVC C++ tools or Vulkan SDK are still missing; finish the installers and rerun setup')
        os.environ['VULKAN_SDK'] = str(windows_sdk())
        os.environ['PATH'] = str(windows_sdk() / 'Bin') + os.pathsep + os.environ.get('PATH', '')
    for tool in ('git', 'cmake'):
        if not shutil.which(tool): raise RuntimeError(tool + ' is not on PATH; reopen the terminal and rerun setup')
    return True


def mac_vulkan(no_deps=False):
    sdk = ROOT / 'vulkan-sdk'
    driver = ROOT / 'moltenvk-1.4.2'
    cache = ROOT / 'work/setup-downloads'
    sdk_ready = (sdk / 'macOS/bin/glslc').is_file() and (sdk / 'macOS/include/vulkan/vulkan.h').is_file()
    driver_ready = (driver / 'MoltenVK/MoltenVK/dynamic/dylib/macOS/libMoltenVK.dylib').is_file()
    if no_deps and not (sdk_ready and driver_ready):
        raise RuntimeError('Missing project-local Vulkan SDK / MoltenVK; omit --no-deps to fetch verified packages')
    if not sdk_ready:
        if sdk.exists(): raise RuntimeError('Incomplete vulkan-sdk directory; inspect it before rerunning the SDK installer')
        archive = download_verified(SDK_URL, SDK_SHA, cache / 'vulkansdk-1.3.296.0.zip')
        installer = cache / 'sdk-installer'
        invoke(['/usr/bin/ditto', '-x', '-k', archive, installer])
        executable = installer / 'InstallVulkan.app/Contents/MacOS/InstallVulkan'
        invoke([executable, '--root', sdk, '--accept-licenses', '--accept-messages', '--confirm-command',
                'install', 'com.lunarg.vulkan.core'])
    if not driver_ready:
        archive = download_verified(MVK_URL, MVK_SHA, cache / 'MoltenVK-macos-1.4.2.tar')
        driver.mkdir(exist_ok=True)
        invoke(['/usr/bin/tar', '-xf', archive, '-C', driver])
    if not (sdk / 'macOS/bin/glslc').is_file() or not (driver / 'MoltenVK/MoltenVK/dynamic/dylib/macOS/MoltenVK_icd.json').is_file():
        raise RuntimeError('Vulkan dependencies did not install successfully; inspect the setup log')
    os.environ['VULKAN_SDK'] = str(sdk / 'macOS')
    os.environ['PATH'] = str(sdk / 'macOS/bin') + os.pathsep + os.environ.get('PATH', '')


def configure_mac(backend, context=None):
    runtime = Path.home() / 'Library/Application Support/radeon-llm'
    path = ROOT / 'server/config.json'
    previous = path if path.exists() else runtime / 'server/config.json'
    if previous.exists():
        config = json.loads(previous.read_text(encoding='utf-8'))
    else:
        config = json.loads((ROOT / 'server/config.example.json').read_text(encoding='utf-8'))
    if previous.exists(): shutil.copy2(previous, path.with_name('config.before-setup-%d.json' % time.time_ns()))
    if context is not None: config['context'] = context
    config.update(backend=backend, native_binary='llama.cpp/build-%s/bin/llama-server' % backend,
                  python=getattr(sys, '_base_executable', sys.executable), amd_shared_fa=backend == 'vulkan')
    if backend == 'metal': config.update(cache_reuse_tokens=0, kv_shift_active_only=False, kv_reuse_mode='exact-prefix')
    key = ROOT / 'server/api-key'
    if not key.exists() and (runtime / 'server/api-key').exists():
        shutil.copy2(runtime / 'server/api-key', key)
        key.chmod(0o600)
    private_json(path, config)
    return config


def copy_windows_runtime(build_dir, runtime):
    source = next((p for p in (build_dir / 'bin/Release', build_dir / 'bin')
                   if (p / 'llama-server.exe').is_file()), None)
    if source is None: raise RuntimeError('Built llama-server.exe not found in ' + str(build_dir))
    target = runtime / 'native'
    target.mkdir(exist_ok=True)
    for path in source.iterdir():
        if path.is_file() and (path.name == 'llama-server.exe' or path.suffix.lower() == '.dll'):
            shutil.copy2(path, target / path.name)
    return target / 'llama-server.exe'


def install(system, backend, context=None, build_dir=None):
    if system == 'Darwin':
        config = configure_mac(backend, context)
        invoke([sys.executable, ROOT / 'server/daemon.py', 'install'])
        key = Path.home() / 'Library/Application Support/radeon-llm/server/api-key'
    else:
        import windows
        runtime = windows.DEFAULT_RUNTIME
        config = windows.configure(runtime)
        shutil.copy2(runtime / 'config.json', runtime / ('config.before-setup-%d.json' % time.time_ns()))
        # A first install needs an absolute model path independent of launch cwd.
        model = Path(config['model'])
        if not model.is_absolute(): model = ROOT / model
        default_model = json.loads((ROOT / 'models/qwen3-source.json').read_text())['file']
        if not model.exists() and model.name == default_model: model = ROOT / 'models' / default_model
        if not model.is_file(): raise RuntimeError('Configured model is missing: ' + str(model))
        config['model'] = str(model)
        if context is not None: config['context'] = context
        python = getattr(sys, '_base_executable', None) or sys.executable
        # Windows cannot relink/replace loaded EXE/DLL files. Build separately,
        # then stop and copy to a private runtime directory before restarting.
        invoke([python, ROOT / 'server/windows.py', 'stop', '--runtime', runtime])
        config['native_binary'] = str(copy_windows_runtime(build_dir or ROOT / 'llama.cpp/build-setup-vulkan', runtime))
        private_json(runtime / 'config.json', config)
        invoke([python, ROOT / 'server/windows.py', 'install', '--runtime', runtime])
        key = runtime / 'api-key'
        windows.opencode_config(config, runtime)
    invoke([sys.executable, ROOT / 'server/check_api.py', '--port', str(config['port']),
            '--key-file', key, '--model', config['alias']])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=['auto', 'vulkan', 'metal'], default='auto')
    parser.add_argument('--no-deps', action='store_true', help='Require dependencies to be installed already')
    parser.add_argument('--build-only', action='store_true', help='Build without downloading a model or changing services/configuration')
    parser.add_argument('--check', action='store_true', help='Check tools without installing or building')
    parser.add_argument('--plan', action='store_true', help='Print the host installation plan without changing services')
    parser.add_argument('--context', type=int)
    parser.add_argument('--jobs', type=int, default=min(6, os.cpu_count() or 2))
    opts = parser.parse_args()
    if sys.version_info < (3, 11): parser.error('Python 3.11+ is required')
    if opts.jobs < 1 or (opts.context is not None and opts.context < 512):
        parser.error('--jobs must be positive; --context must be at least 512')
    system = platform.system()
    backend = backend_for(system, platform.machine(), opts.backend)
    print(json.dumps({'os': system, 'architecture': platform.machine(), 'backend': backend,
                      'native_revision': bootstrap.COMMIT, 'build_only': opts.build_only,
                      'autostart': 'LaunchAgent' if system == 'Darwin' else 'Task Scheduler'}, indent=2), flush=True)
    if opts.plan: return 0
    if opts.check: return 0 if dependencies(system, backend, check=True) else 1
    lock_dir = ROOT / 'work'
    lock_dir.mkdir(exist_ok=True)
    with FileLock(lock_dir / 'setup.lock'):
        dependencies(system, backend, opts.no_deps)
        if system == 'Darwin' and backend == 'vulkan': mac_vulkan(opts.no_deps)
        directory = ROOT / 'llama.cpp' / ('build-setup-vulkan' if system == 'Windows' else 'build-' + backend)
        bootstrap.build(backend, opts.jobs, env=dict(os.environ), build_dir=directory)
        if opts.build_only:
            print('Build complete; model, configuration and services were not changed.'); return 0
        bootstrap.download()
        install(system, backend, opts.context, directory)
    print('Setup complete: source build, model verification, autostart and API/tool checks passed.', flush=True)
    return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print('Setup failed: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
