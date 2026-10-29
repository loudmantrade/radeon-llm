from platform_support import shutdown_signals, child_options, stop_child
"""Persistent cache for a single llama-server slot; no API proxy is needed."""
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import subprocess
import time
import urllib.request


def log(message):
    print('KV cache: ' + message, flush=True)


def identity(args):
    # Include native libraries as well as the launcher: their builds can differ.
    paths = [Path(args[0]), Path(args[args.index('-m') + 1])]
    paths += sorted(p for p in Path(args[0]).parent.iterdir()
                    if p.suffix.lower() in ('.dylib', '.dll', '.so') and not p.is_symlink())
    files = [(str(p.resolve()), p.stat().st_size, p.stat().st_mtime_ns) for p in paths]
    data = {'format': 1, 'args': args, 'files': files}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class SlotCache:
    def __init__(self, directory, port, key, fingerprint):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.base = 'http://127.0.0.1:%d' % port
        self.key = key
        self.fingerprint = fingerprint
        self.index = self.directory / 'latest.json'
        self.last_saved_task = None

    def api(self, path, body=None, timeout=120):
        request = urllib.request.Request(self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def restore(self):
        if not self.index.exists():
            log('no saved snapshot; cold start')
            return None
        metadata = json.loads(self.index.read_text())
        if not isinstance(metadata, dict):
            raise ValueError('Invalid snapshot index')
        if metadata.get('fingerprint') != self.fingerprint:
            log('snapshot incompatible with model/build/settings; cold start')
            return None
        filename = metadata['filename']
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.startswith('slot-'):
            raise ValueError('Invalid snapshot filename')
        if (self.directory / filename).stat().st_size != metadata['bytes']:
            raise ValueError('Incomplete snapshot')
        result = self.api('/slots/0?action=restore', {'filename': filename})
        if result.get('n_restored', 0) <= 0:
            raise ValueError('Snapshot restored no tokens')
        log('restored %d tokens in %.2fs' % (result['n_restored'], result['timings']['restore_ms'] / 1000))
        return result

    def save_if_idle(self, minimum_tokens=512):
        slots = self.api('/slots', timeout=3)
        slot = next(s for s in slots if s['id'] == 0)
        task = slot.get('id_task')
        if (slot['is_processing'] or task is None or task == self.last_saved_task
                or slot.get('n_prompt_tokens', 0) < minimum_tokens):
            return False
        # A fresh name preserves the previous snapshot if writing is interrupted.
        filename = 'slot-%d.bin' % time.time_ns()
        path = self.directory / filename
        try:
            result = self.api('/slots/0?action=save', {'filename': filename})
            if result.get('n_saved', 0) < minimum_tokens or result.get('n_written', 0) <= 0:
                raise ValueError('Empty or undersized snapshot')
            path.chmod(0o600)
            with path.open('r+b') as f:
                os.fsync(f.fileno())
            metadata = {'fingerprint': self.fingerprint, 'filename': filename,
                        'bytes': path.stat().st_size, 'tokens': result['n_saved'],
                        'saved_at': time.time(), 'save_ms': result['timings']['save_ms']}
            temporary = self.index.with_suffix('.tmp')
            temporary.write_text(json.dumps(metadata, indent=2) + '\n')
            temporary.chmod(0o600)
            with temporary.open('r+b') as f:
                os.fsync(f.fileno())
            temporary.replace(self.index)
            self.last_saved_task = task
            for previous in self.directory.glob('slot-*.bin'):
                if previous != path:
                    previous.unlink()
            log('saved %d tokens, %.1f MiB in %.2fs' % (
                result['n_saved'], metadata['bytes'] / 2**20, result['timings']['save_ms'] / 1000))
            return True
        except BaseException:
            # Never remove a snapshot already referenced by the committed index.
            if not self.index.exists() or json.loads(self.index.read_text()).get('filename') != filename:
                path.unlink(missing_ok=True)
            raise


def supervise(args, env, cache, ready_path, idle_seconds=5):
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    old_handlers = {s: signal.signal(s, stop) for s in shutdown_signals()}
    ready_path = Path(ready_path)
    ready_path.unlink(missing_ok=True)
    child = None
    try:
        child = subprocess.Popen(args, env=env, stdout=sys.stdout, stderr=sys.stderr, **child_options())
        deadline = time.monotonic() + 180
        while not stopping and child.poll() is None and not ready_path.with_name('stop.request').exists():
            try:
                if cache.api('/health', timeout=2).get('status') == 'ok':
                    break
            except (OSError, ValueError):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError('Server startup timed out')
            time.sleep(.5)
        if stopping or child.poll() is not None or ready_path.with_name('stop.request').exists():
            return child.returncode or 0
        try:
            cache.restore()
        except (OSError, ValueError, KeyError) as exc:
            log('restore failed; continuing cold: ' + str(exc))
        ready_path.write_text(json.dumps({'supervisor_pid': os.getpid(), 'server_pid': child.pid}))
        log('ready; automatic idle snapshots enabled')
        idle_since = None
        retry_after = 0
        while not stopping and child.poll() is None and not ready_path.with_name('stop.request').exists():
            try:
                slots = cache.api('/slots', timeout=3)
                if any(s['is_processing'] for s in slots):
                    idle_since = None
                else:
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since >= idle_seconds and time.monotonic() >= retry_after:
                        cache.save_if_idle()
            except (OSError, ValueError, KeyError, StopIteration) as exc:
                log('snapshot check failed; retrying later: ' + str(exc))
                retry_after = time.monotonic() + 30
            time.sleep(1)
        return child.returncode or 0
    finally:
        ready_path.unlink(missing_ok=True)
        stop_child(child)
        for s, handler in old_handlers.items():
            signal.signal(s, handler)
