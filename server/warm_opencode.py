"""Capture OpenCode's real static prefixes, then seed the existing indexed KV cache.

The capture server returns text only: no tools are requested or executed. No model
weights, production configuration, or running services are changed.
"""
import argparse
import copy
from platform_support import FileLock
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid

from cache_index import common_prefix, packed

ROOT = Path(__file__).resolve().parent.parent
WORK = (ROOT.parent.parent if ROOT.parent.name == 'outputs' else ROOT) / 'work' / 'opencode-prefixes'


def private_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write('\n')
    temporary.replace(path)


class API:
    def __init__(self, port, key):
        self.base = 'http://127.0.0.1:%d' % port
        self.key = key

    def __call__(self, path, body=None, timeout=30):
        request = urllib.request.Request(self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def tokens(self, text):
        return self('/tokenize', {'content': text, 'add_special': True, 'parse_special': True})['tokens']


def matches_project(system, project):
    # OpenCode can print forward slashes and different drive-letter case on Windows.
    expected = os.path.normcase(os.path.normpath(str(project)))
    reported = re.findall(r'^\s*Working directory: ([^\r\n]+)', system, re.M)
    return any(os.path.normcase(os.path.normpath(value)) == expected for value in reported)


def capture(binary, project, config, agents):
    """Use isolated OpenCode state and a loopback fake provider for each agent."""
    captures = []
    marker = 'OPENCODE_PREFIX_CAPTURE_' + uuid.uuid4().hex

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            raw = json.dumps({'object': 'list', 'data': [
                {'id': 'qwen3-4b-local', 'object': 'model'}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path.rstrip('/') != '/v1/chat/completions':
                self.send_error(400, 'Only chat capture is supported')
                return
            captures.append(body)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            for delta, finish in [({'role': 'assistant', 'content': 'OK'}, None), ({}, 'stop')]:
                chunk = {'id': 'prefix-capture', 'object': 'chat.completion.chunk',
                         'created': int(time.time()), 'model': 'qwen3-4b-local',
                         'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
            self.close_connection = True

    WORK.mkdir(parents=True, exist_ok=True, mode=0o700)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='capture-', dir=WORK) as temporary:
            directory = Path(temporary)
            config = copy.deepcopy(config)
            config['providers']['radeon']['settings'].update(
                baseURL='http://127.0.0.1:%d/v1' % server.server_port, apiKey='capture-only')
            config_path = directory / 'opencode.json'
            private_json(config_path, config)
            env = dict(os.environ, PWD=str(project), OPENCODE_CONFIG=str(config_path),
                       OPENCODE_DISABLE_FILEWATCHER='1', OPENCODE_FILEWATCHER_DISABLE='1')
            # Prevent inherited inline overrides from redirecting the capture provider.
            env.pop('OPENCODE_CONFIG_CONTENT', None)
            for kind in ('DATA', 'STATE', 'CONFIG', 'CACHE'):
                path = directory / kind.lower()
                path.mkdir()
                env['XDG_' + kind + '_HOME'] = str(path)
            private_json(directory / 'config/opencode/service.json', {'disabled': True})
            result = {}
            for agent in agents:
                before = len(captures)
                run = subprocess.run([str(binary), 'run', '--standalone', '--agent', agent,
                    '--model', 'radeon/qwen3-4b-local', '--format', 'json', '--title', 'Prefix capture', marker],
                    cwd=project, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=120)
                matching = [b for b in captures[before:] if marker in json.dumps(b.get('messages', []))]
                if run.returncode or len(matching) != 1:
                    # Do not print bodies, credentials, or arbitrary CLI diagnostic output.
                    raise RuntimeError('OpenCode capture failed for %s (exit %s, requests %d)' %
                                       (agent, run.returncode, len(matching)))
                system = '\n'.join(str(m.get('content', '')) for m in matching[0].get('messages', [])
                                   if m.get('role') == 'system')
                if not matches_project(system, project):
                    raise RuntimeError('OpenCode reported a different working directory for ' + agent)
                result[agent] = matching[0]
                private_json(WORK / ('last-capture-' + agent + '.json'), matching[0])
                print('%s: captured %d tool definitions' % (agent, len(matching[0].get('tools', []))), flush=True)
            return result, marker
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def extract(api, body, marker):
    messages = body.get('messages', [])
    # Plan adds a static reminder as a separate user message before our marker.
    # Capture always creates a new isolated session, so that reminder is also static.
    if not messages or messages[-1]['role'] != 'user' or messages[-1].get('content') != marker or any(
            m['role'] not in ('system', 'developer', 'user') for m in messages[:-1]):
        raise ValueError('Capture contains conversation history; refusing to warm it')
    prompt = api('/apply-template', body)['prompt']
    if prompt.count(marker) != 1:
        raise ValueError('Capture marker is not unique in the rendered prompt')
    text = prompt.split(marker)[0]
    # A tokenizer can merge the boundary with the first character of user text.
    # Use only tokens that are also an exact prefix of the complete real request.
    tokens = api.tokens(text)
    tokens = tokens[:common_prefix(tokens, api.tokens(prompt))]
    if len(tokens) < 512:
        raise ValueError('Static prefix too short for this daemon cache policy')
    return tokens


def idle(api, wait):
    deadline = time.monotonic() + wait
    while True:
        status = api('/cache/status')
        if not status.get('busy') and not any(s['is_processing'] for s in api('/slots')):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('Daemon busy; profiles captured. Retry when idle, or use --wait SECONDS')
        time.sleep(2)


def warm(api, tokens):
    started = time.monotonic()
    finished = threading.Event()

    def progress():
        while not finished.wait(15):
            try:
                slots = api('/slots', timeout=3)
                slot = next((s for s in slots if s['is_processing']), None)
                if slot:
                    print('  prefix: cached %d, evaluated %d / %d; elapsed %.0fs' %
                          (slot.get('n_prompt_tokens_cache', 0), slot.get('n_prompt_tokens_processed', 0),
                           len(tokens), time.monotonic() - started), flush=True)
            except (OSError, ValueError, KeyError):
                pass

    monitor = threading.Thread(target=progress, daemon=True)
    monitor.start()
    # Force exact reuse even when the daemon's experimental chunk shifting is on.
    try:
        result = api('/completion', {'prompt': tokens, 'n_predict': 0,
                     'cache_prompt': True, 'n_cache_reuse': 0, 'stream': False}, timeout=2400)
    finally:
        finished.set()
        monitor.join(timeout=4)
    # This pinned llama.cpp build samples one token even for n_predict=0.
    # It is not decoded back into the KV state (slot n_tokens remains len(prompt)).
    # Accept that single sample, but reject any actual continued generation.
    if result.get('tokens_predicted', 0) > 1:
        raise RuntimeError('Warmup unexpectedly continued generation')
    return {'tokens': len(tokens), 'wall_seconds': time.monotonic() - started,
            'cached': result.get('timings', {}).get('cache_n', 0),
            'native_sampled_tokens': result.get('tokens_predicted', 0), 'timings': result.get('timings', {})}


def wait_saved(api, tokens, cache_dir, timeout=45):
    """Observe the daemon's own committed snapshot; never write its private state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = api('/cache/status')
        try:
            fingerprint = json.loads((cache_dir / 'latest.json').read_text())['fingerprint']
            expected = hashlib.sha256(fingerprint.encode() + packed(tokens)).hexdigest()
            entry = next((e for e in status.get('snapshots', []) if e['id'] == expected and e['compatible']), None)
            if entry:
                return {'snapshot': expected, 'bytes': entry['bytes']}
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(1)
    raise RuntimeError('Warmup evaluated, but its disk snapshot was not confirmed within %ss' % timeout)


def verify(api, body, minimum):
    body = copy.deepcopy(body)
    body.update(stream=False, max_completion_tokens=1, max_tokens=1, n_cache_reuse=0)
    body.pop('stream_options', None)
    body['messages'][-1]['content'] = 'Reply only OK.'
    started = time.monotonic()
    result = api('/v1/chat/completions', body, timeout=2400)
    cached = result.get('usage', {}).get('prompt_tokens_details', {}).get('cached_tokens', 0)
    if cached < minimum - 8:
        raise RuntimeError('Chat verification reused only %d/%d static tokens' % (cached, minimum))
    return {'cached': cached, 'wall_seconds': time.monotonic() - started, 'timings': result.get('timings', {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', nargs='?', type=Path, default=Path.cwd())
    parser.add_argument('--agents', nargs='+', choices=['radeon', 'build', 'plan'], default=['radeon', 'build', 'plan'])
    parser.add_argument('--capture-only', action='store_true')
    parser.add_argument('--verify', action='store_true', help='Check real chat requests after warming all agents (one output token each)')
    parser.add_argument('--wait', type=int, default=0, help='Wait for an idle daemon before each prefix')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--key-file', type=Path, default=ROOT / 'server/api-key')
    parser.add_argument('--config', type=Path, default=ROOT / 'opencode.json')
    parser.add_argument('--cache-dir', type=Path, default=Path.home() / 'Library/Application Support/radeon-llm/cache/kv')
    parser.add_argument('--opencode', type=Path, default=Path(shutil.which('opencode') or Path.home() / '.opencode/bin/opencode'))
    args = parser.parse_args()
    project = args.project.resolve(strict=True)
    if not project.is_dir():
        parser.error('Project must be a directory')
    config = json.loads(args.config.read_text())
    api = API(args.port, args.key_file.read_text().strip())
    profiles, marker = capture(args.opencode, project, config, args.agents)
    project_id = hashlib.sha256(str(project).encode()).hexdigest()[:16]
    report = {'project': str(project), 'started': time.time(), 'capture_only': args.capture_only, 'agents': {}}
    destination = ROOT / 'prefix-profiles' / project_id
    for agent, body in profiles.items():
        tokens = extract(api, body, marker)
        profile = {'project': str(project), 'agent': agent, 'tokens': tokens,
                   'token_sha256': hashlib.sha256(packed(tokens)).hexdigest(), 'captured_at': time.time()}
        private_json(destination / (agent + '.json'), profile)
        report['agents'][agent] = {'tokens': len(tokens), 'profile': str(destination / (agent + '.json'))}
        print('%s: static prefix %d tokens' % (agent, len(tokens)), flush=True)
    report_path = ROOT / 'logs/opencode-prefix-warmup.json'
    private_json(report_path, report)
    if args.capture_only:
        print('Captured only; no GPU inference performed. Report: ' + str(report_path), flush=True)
        return
    for agent in args.agents:
        idle(api, args.wait)
        tokens = json.loads((destination / (agent + '.json')).read_text())['tokens']
        print('%s: evaluating static prefix (n_predict=0)...' % agent, flush=True)
        result = warm(api, tokens)
        result.update(wait_saved(api, tokens, args.cache_dir))
        report['agents'][agent].update(result)
        private_json(report_path, report)
        print('%s: ready, %.2fs; persisted %.1f MiB' %
              (agent, result['wall_seconds'], result['bytes'] / 2**20), flush=True)
    if args.verify:
        fresh, fresh_marker = capture(args.opencode, project, config, args.agents)
        for agent in args.agents:
            idle(api, args.wait)
            original = json.loads((destination / (agent + '.json')).read_text())['tokens']
            new = extract(api, fresh[agent], fresh_marker)
            if common_prefix(original, new) < len(original) - 8:
                raise RuntimeError('Static prefix changed between OpenCode processes for ' + agent)
            result = verify(api, fresh[agent], report['agents'][agent]['tokens'])
            report['agents'][agent]['chat_verification'] = result
            private_json(report_path, report)
            print('%s: chat reused %d prefix tokens' % (agent, result['cached']), flush=True)
    report['completed'] = time.time()
    private_json(report_path, report)
    print('Prefixes persisted; normal requests select them automatically. Report: ' + str(report_path), flush=True)


if __name__ == '__main__':
    directory = ROOT / 'prefix-profiles'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with FileLock(directory / 'warm.lock'):
            main()
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(str(exc))
