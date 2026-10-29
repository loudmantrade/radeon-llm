"""Windows Vulkan launcher, indexed KV supervisor and per-user logon task."""
import argparse
import base64
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import traceback

from platform_support import FileLock, WINDOWS
from slot_cache import identity
from warm_opencode import API, private_json

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNTIME = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'radeon-llm'
TASK = 'RadeonLLM'


def configure(runtime, model=None, binary=None):
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / 'cache/kv').mkdir(parents=True, exist_ok=True)
    (runtime / 'logs').mkdir(exist_ok=True)
    path = runtime / 'config.json'
    config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else json.loads(
        (ROOT / 'server/config.windows.example.json').read_text(encoding='utf-8'))
    if model is not None: config['model'] = str(model.resolve())
    if binary is not None: config['native_binary'] = str(binary.resolve())
    if not path.exists() or model is not None or binary is not None:
        private_json(path, config)
    key = runtime / 'api-key'
    if not key.exists():
        fd = os.open(str(key), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream: stream.write(secrets.token_urlsafe(32))
    return config


def binary_path(config, root=ROOT):
    if config.get('native_binary'):
        return root / config['native_binary']
    candidates = [root / ('llama.cpp/build-vulkan/bin/' + suffix + 'llama-server.exe')
                  for suffix in ('Release/', '')]
    return next((p for p in candidates if p.is_file()), candidates[0])


def environment(binary):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(('GGML_VK_', 'LLAMA_ARG_')) or key in (
                'LLAMA_KV_SHIFT_ACTIVE_ONLY', 'MVK_CONFIG_FAST_MATH_ENABLED',
                'VK_DRIVER_FILES', 'VK_ICD_FILENAMES', 'DYLD_LIBRARY_PATH'):
            env.pop(key)
    # Native Windows Vulkan loader discovers the display driver's ICD itself.
    env['PATH'] = str(binary.parent) + os.pathsep + env.get('PATH', '')
    return env


def command(config, runtime, cpu=False, root=ROOT):
    binary = binary_path(config, root)
    model = root / config['model']
    if not binary.is_file(): raise RuntimeError('Build Vulkan first with .\\windows.ps1 build, or configure --binary PATH')
    if not model.is_file(): raise RuntimeError('Download the GGUF with .\\windows.ps1 download-model, or configure --model PATH')
    if config.get('cache_reuse_tokens', 0):
        raise RuntimeError('Windows profile uses exact prefix reuse; experimental chunk shifting is not validated here')
    env = environment(binary)
    device = 'none'
    if not cpu:
        listing = subprocess.run([str(binary), '--list-devices'], env=env, capture_output=True,
                                 text=True, encoding='utf-8', errors='replace', timeout=90)
        devices = re.findall(r'^\s*(Vulkan\d+):', listing.stdout + listing.stderr, re.M)
        device = config.get('gpu_device', 'auto')
        if device == 'auto': device = devices[0] if devices else None
        if listing.returncode or device not in devices:
            raise RuntimeError('Requested Vulkan GPU unavailable. --list-devices output:\n' + listing.stdout + listing.stderr)
    port = config['port']
    backend = config.get('kv_backend_port', port + 1)
    if backend == port: raise ValueError('Public API and native backend ports must differ')
    args = [str(binary), '-m', str(model), '--alias', config['alias'],
            '--host', '127.0.0.1', '--port', str(backend), '-c', str(config['context']),
            '--parallel', '1', '--device', device, '-ngl', '0' if cpu else '99',
            '--fit', config.get('fit', 'on'), '-b', '128', '-ub', '128', '-t', '8',
            '-fa', config.get('flash_attention', 'on'),
            '-ctk', config.get('cache_type_k', 'q8_0'), '-ctv', config.get('cache_type_v', 'q8_0'),
            '--jinja', '--no-context-shift', '--no-webui', '--api-key-file', str(runtime / 'api-key'),
            '--temp', '0.2', '-n', str(config['max_output']), '--slots',
            '--slot-save-path', str((runtime / 'cache/kv').resolve()) + '/',
            '--cache-ram', '0', '--no-warmup']
    return args, env


def api(config, runtime):
    return API(config['port'], (runtime / 'api-key').read_text().strip())


def running(runtime):
    try:
        lock = FileLock(runtime / 'cache/kv/owner.lock')
    except BlockingIOError:
        return True
    else:
        lock.close()
        return False


def ready(config, runtime):
    if not running(runtime): return False
    try:
        marker = json.loads((runtime / 'cache/kv/ready.json').read_text())
        return bool(marker.get('supervisor_pid')) and api(config, runtime)('/health', timeout=2).get('status') == 'ok'
    except (OSError, ValueError, KeyError): return False


def run(config, runtime, cpu=False):
    from cache_proxy import supervise_proxy
    from tiered_cache import TieredCache
    args, env = command(config, runtime, cpu)
    directory = runtime / 'cache/kv'
    cache = TieredCache(directory, config.get('kv_backend_port', config['port'] + 1),
                       (runtime / 'api-key').read_text().strip(), identity(args), config)
    # Clear only after acquiring ownership; a duplicate launch cannot erase a stop request.
    (directory / 'stop.request').unlink(missing_ok=True)
    return supervise_proxy(args, env, cache, directory / 'ready.json', config['port'],
                           config.get('kv_backend_port', config['port'] + 1), config.get('kv_idle_seconds', 5))


def start(config, runtime):
    if ready(config, runtime):
        print('Already ready: http://127.0.0.1:%d/v1' % config['port']); return
    if running(runtime): raise RuntimeError('Service is already starting; inspect logs')
    # Fail before spawning a detached process if another program owns either port.
    for port in (config['port'], config.get('kv_backend_port', config['port'] + 1)):
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                raise RuntimeError('Port %d is already in use' % port)
    args = [sys.executable, str(Path(__file__).resolve()), 'run', '--runtime', str(runtime), '--background']
    process = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None: raise RuntimeError('Service exited during startup; inspect .\\windows.ps1 logs')
        if ready(config, runtime):
            print('Ready: http://127.0.0.1:%d/v1' % config['port']); return
        time.sleep(1)
    raise RuntimeError('Startup still in progress after 180s; inspect logs or stop the service')


def stop(config, runtime):
    if not running(runtime): print('Stopped'); return
    (runtime / 'cache/kv/stop.request').write_text('stop\n')
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if not running(runtime): print('Stopped'); return
        time.sleep(.25)
    raise RuntimeError('Service did not stop within 40s; inspect logs')


def ps_string(value): return "'" + str(value).replace("'", "''") + "'"


def task_script(action, runtime):
    name = ps_string(TASK)
    if action == 'uninstall':
        return '$ErrorActionPreference="Stop"; Unregister-ScheduledTask -TaskName %s -Confirm:$false -ErrorAction SilentlyContinue' % name
    arguments = subprocess.list2cmdline([str(Path(__file__).resolve()), 'run', '--runtime', str(runtime), '--background'])
    return '''$ErrorActionPreference='Stop'
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute %s -Argument %s -WorkingDirectory %s
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName %s -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
''' % (ps_string(sys.executable), ps_string(arguments), ps_string(ROOT), name)


def install(action, runtime):
    script = base64.b64encode(task_script(action, runtime).encode('utf-16-le')).decode('ascii')
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', script], check=True)


def opencode_config(config, runtime):
    template = ROOT / 'opencode.example.json'
    if not template.exists(): template = ROOT / 'opencode.json'
    body = json.loads(template.read_text(encoding='utf-8'))
    settings = body['providers']['radeon']['settings']
    settings.update(baseURL='http://127.0.0.1:%d/v1' % config['port'],
                    apiKey='{file:' + (runtime / 'api-key').as_posix() + '}')
    context = config['context']
    try:
        slots = api(config, runtime)('/slots', timeout=3)
        actual = [s['n_ctx'] for s in slots if s.get('n_ctx', 0) > 0]
        if actual: context = min(context, min(actual))
    except (OSError, ValueError, KeyError):
        pass  # Configure can run before the model is loaded.
    body['providers']['radeon']['models']['qwen3-4b-local']['limit'].update(
        context=context, output=config['max_output'])
    private_json(runtime / 'opencode.json', body)
    return runtime / 'opencode.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['configure', 'run', 'start', 'stop', 'restart', 'status',
                                          'install', 'uninstall', 'logs', 'opencode', 'check-api', 'warm'])
    parser.add_argument('--runtime', type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument('--model', type=Path)
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--cpu', action='store_true')
    parser.add_argument('--background', action='store_true')
    parser.add_argument('--project', type=Path, default=Path.cwd())
    opts, extra = parser.parse_known_args()
    if (opts.model or opts.binary) and opts.action != 'configure':
        parser.error('--model and --binary belong to configure')
    if extra and opts.action not in ('opencode', 'warm', 'check-api'):
        parser.error('Unrecognized arguments: ' + ' '.join(extra))
    if not WINDOWS: raise RuntimeError('This entry point requires Windows; on macOS use daemon.command')
    runtime = opts.runtime.resolve()
    config = configure(runtime, opts.model, opts.binary)
    if opts.action == 'run':
        if opts.background:
            with (runtime / 'logs/server.log').open('a', encoding='utf-8', buffering=1) as log, \
                    contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                try:
                    return run(config, runtime, opts.cpu)
                except BaseException:
                    # Scheduled tasks and CREATE_NO_WINDOW have no usable stderr
                    # after leaving this context. Preserve startup failures here.
                    traceback.print_exc()
                    raise
        return run(config, runtime, opts.cpu)
    control = opts.action not in ('opencode', 'check-api', 'warm', 'status', 'logs')
    with FileLock(runtime / 'manager.lock') if control else contextlib.nullcontext():
        if opts.action == 'configure':
            opencode_config(config, runtime)
            print('Configuration: ' + str(runtime / 'config.json'))
        elif opts.action == 'start': start(config, runtime)
        elif opts.action == 'stop': stop(config, runtime)
        elif opts.action == 'restart': stop(config, runtime); start(config, runtime)
        elif opts.action == 'status':
            print('ready' if ready(config, runtime) else 'starting/busy' if running(runtime) else 'stopped')
        elif opts.action == 'install':
            install('install', runtime)
            start(config, runtime)
        elif opts.action == 'uninstall':
            install('uninstall', runtime)
            stop(config, runtime)
        elif opts.action == 'logs':
            path = runtime / 'logs/server.log'
            print('\n'.join(path.read_text(encoding='utf-8', errors='replace').splitlines()[-100:]) if path.exists() else 'No log yet')
        elif opts.action == 'opencode':
            if not ready(config, runtime):
                with FileLock(runtime / 'manager.lock'):
                    start(config, runtime)
            binary = shutil.which('opencode')
            if not binary: raise RuntimeError('Install OpenCode and reopen PowerShell so opencode is on PATH')
            env = dict(os.environ, OPENCODE_CONFIG=str(opencode_config(config, runtime)), PWD=str(opts.project.resolve()))
            # Batch launchers need cmd.exe, while native OpenCode executables do not.
            command_line = ([binary, 'run', '--standalone'] + extra[1:]
                            if extra[:1] == ['run'] else [binary, '--standalone'] + extra)
            if Path(binary).suffix.lower() in ('.cmd', '.bat'):
                raise RuntimeError('Use the native opencode.exe distribution on Windows')
            return subprocess.call(command_line, cwd=opts.project, env=env)
        elif opts.action in ('check-api', 'warm'):
            common = ['--port', str(config['port']), '--key-file', str(runtime / 'api-key')]
            if opts.action == 'warm':
                script = 'warm_opencode.py'
                common += [str(opts.project.resolve()), '--config', str(opencode_config(config, runtime)),
                           '--cache-dir', str(runtime / 'cache/kv')]
            else: script = 'check_api.py'
            return subprocess.call([sys.executable, str(ROOT / 'server' / script)] + common + extra)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
