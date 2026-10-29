from platform_support import shutdown_signals, child_options, stop_child
"""Local OpenAI-compatible forwarding with serialized KV selection and SSE passthrough."""
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import sys
import socket
import sqlite3
import subprocess
import threading
import time
from urllib.parse import urlsplit
from slot_cache import log

INFERENCE = {'/v1/chat/completions', '/chat/completions', '/v1/completions', '/completions', '/completion'}
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade', 'host'}


class CacheHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Proxy:
    def __init__(self, cache, public_port, backend_port):
        self.cache = cache
        self.backend_port = backend_port
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.connections = set()
        self.connections_lock = threading.Lock()
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, fmt, *args):
                pass  # Native logs already contain timings; never log request bodies.

            def json_reply(self, status, data):
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(raw)
                self.close_connection = True

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def do_OPTIONS(self):
                self.handle_request()

            def handle_request(self):
                if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + cache.key):
                    self.json_reply(401, {'error': {'message': 'Unauthorized'}})
                    return
                if not app.ready.is_set():
                    self.json_reply(503, {'error': {'message': 'Model/cache loading'}})
                    return
                path = urlsplit(self.path).path.rstrip('/') or '/'
                if path == '/cache/status' and self.command == 'GET':
                    # Do not block status polling behind a 25-minute inference request.
                    if cache.lock.acquire(blocking=False):
                        try:
                            self.json_reply(200, cache.status())
                        finally:
                            cache.lock.release()
                    else:
                        self.json_reply(200, {'busy': True, 'message': 'Inference/cache operation in progress'})
                    return
                if self.headers.get('Transfer-Encoding'):
                    self.json_reply(411, {'error': {'message': 'Content-Length required'}})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    self.json_reply(400, {'error': {'message': 'Invalid Content-Length'}})
                    return
                if length < 0 or length > 16 * 2**20:
                    self.json_reply(413, {'error': {'message': 'Request body too large'}})
                    return
                self.connection.settimeout(30)
                try:
                    raw = self.rfile.read(length)
                except OSError:
                    return
                if len(raw) != length:
                    self.json_reply(400, {'error': {'message': 'Incomplete request body'}})
                    return
                inference = self.command == 'POST' and path in INFERENCE
                state_action = self.command == 'POST' and not inference and path not in {
                    '/tokenize', '/apply-template', '/detokenize'}
                if inference or state_action:
                    # One GPU slot: selection, inference, and snapshotting must not interleave.
                    with cache.lock:
                        if app.stopping.is_set():
                            self.json_reply(503, {'error': {'message': 'Daemon stopping'}})
                            return
                        self.forward(path, raw, inference, state_action)
                else:
                    self.forward(path, raw, False, False)

            def forward(self, path, raw, inference, state_action):
                tokens = used_entry = None
                response_started = False
                connection = http.client.HTTPConnection('127.0.0.1', backend_port, timeout=2400)
                with app.connections_lock:
                    app.connections.add(connection)
                try:
                    if inference:
                        try:
                            body = json.loads(raw)
                            if isinstance(body, dict):
                                tokens = cache.tokenize(path, body)
                        except (OSError, ValueError, KeyError, TypeError) as exc:
                            # Unsupported/malformed requests still reach the native API unchanged.
                            log('prefix lookup unavailable: ' + type(exc).__name__)
                        try:
                            used_entry = cache.prepare(tokens)
                        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
                            log('cache preparation failed; continuing cold: ' + type(exc).__name__)
                            cache.invalidate()
                    headers = {key: value for key, value in self.headers.items()
                               if key.lower() not in HOP_HEADERS and key.lower() != 'content-length'}
                    headers['Content-Length'] = str(len(raw))
                    headers['Host'] = '127.0.0.1:%d' % backend_port
                    connection.request(self.command, self.path, body=raw or None, headers=headers)
                    response = connection.getresponse()
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in HOP_HEADERS:
                            self.send_header(key, value)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    response_started = True
                    while True:
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    self.close_connection = True
                    if inference:
                        if 200 <= response.status < 300:
                            try:
                                cache.completed(tokens, used_entry)
                            except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
                                cache.invalidate()
                                log('cache accounting failed: ' + type(exc).__name__)
                        else:
                            cache.invalidate()
                    if state_action:
                        cache.invalidate()
                except (OSError, http.client.HTTPException, ValueError) as exc:
                    if inference or state_action:
                        cache.invalidate()
                    log('forwarding ended: ' + type(exc).__name__)
                    if not response_started:
                        try:
                            self.json_reply(502, {'error': {'message': 'Local backend unavailable'}})
                        except OSError:
                            pass
                    self.close_connection = True
                finally:
                    connection.close()
                    with app.connections_lock:
                        app.connections.discard(connection)

        self.http = CacheHTTPServer(('127.0.0.1', public_port), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .2}, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.ready.clear()
        with self.connections_lock:
            for connection in self.connections:
                if connection.sock is not None:
                    try:
                        connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                connection.close()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=3)


def supervise_proxy(args, env, cache, ready_path, public_port, backend_port, idle_seconds=5):
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    old_handlers = {s: signal.signal(s, stop) for s in shutdown_signals()}
    ready_path = Path(ready_path)
    ready_path.unlink(missing_ok=True)
    child = None
    proxy = None
    try:
        proxy = Proxy(cache, public_port, backend_port)
        proxy.start()
        child = subprocess.Popen(args, env=env, stdout=sys.stdout, stderr=sys.stderr, **child_options())
        deadline = time.monotonic() + 180
        while not stopping and child.poll() is None and not ready_path.with_name('stop.request').exists():
            try:
                if cache.api('/health', timeout=2).get('status') == 'ok':
                    break
            except (OSError, ValueError):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError('Native backend startup timed out')
            time.sleep(.5)
        if stopping or child.poll() is not None or ready_path.with_name('stop.request').exists():
            return child.returncode or 0
        with cache.lock:
            try:
                cache.restore()
            except (OSError, ValueError, KeyError) as exc:
                log('startup restore unavailable; cold start: ' + type(exc).__name__)
        ready_path.write_text(json.dumps({'supervisor_pid': __import__('os').getpid(), 'server_pid': child.pid,
                                         'proxy': True, 'backend_port': backend_port}))
        proxy.ready.set()
        log('indexed cache ready on port %d; native backend %d' % (public_port, backend_port))
        idle_since = None
        retry_after = 0
        maintenance_after = 0
        while not stopping and child.poll() is None and not ready_path.with_name('stop.request').exists():
            if cache.lock.acquire(blocking=False):
                try:
                    slots = cache.api('/slots', timeout=3)
                    if any(s['is_processing'] for s in slots):
                        idle_since = None
                    else:
                        idle_since = idle_since or time.monotonic()
                        now = time.monotonic()
                        if now - idle_since >= idle_seconds and now >= retry_after:
                            cache.save_if_idle()
                            if now >= maintenance_after:
                                cache.store.prune(protected=cache.active_id)
                                maintenance_after = now + 60
                except (OSError, ValueError, KeyError, StopIteration, sqlite3.Error) as exc:
                    log('index maintenance failed: ' + type(exc).__name__)
                    retry_after = time.monotonic() + 30
                finally:
                    cache.lock.release()
            else:
                idle_since = None
            time.sleep(.5)
        return child.returncode or 0
    finally:
        ready_path.unlink(missing_ok=True)
        if proxy is not None:
            proxy.stop()
        stop_child(child)
        with cache.lock:
            cache.close()
        for s, handler in old_handlers.items():
            signal.signal(s, handler)
