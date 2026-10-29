#!/usr/bin/python3
"""Exercise A -> B -> A, then restart and return to B through the public API."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = Path.home() / 'Library/Application Support/radeon-llm'


def check_indexed(port, key, restart, output, backend='installed Radeon daemon'):
    base = 'http://127.0.0.1:%d' % port

    def api(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=300) as response:
            return json.load(response)

    def status():
        deadline = time.monotonic() + 180
        while True:
            result = api('/cache/status')
            if not result.get('busy'):
                return result
            if time.monotonic() > deadline:
                raise RuntimeError('Cache remains busy')
            time.sleep(.5)

    nonce = str(time.time_ns())
    props = api('/props')
    def body(name):
        return {'model': 'qwen3-4b-local', 'temperature': 0, 'max_tokens': 8, 'cache_prompt': True,
                'messages': [{'role': 'system', 'content': 'PROJECT_%s_%s\n' % (name, nonce) + '\n'.join(
                    'Entry %04d: no action needed.' % i for i in range(55))},
                             {'role': 'user', 'content': 'What is 2+2? Reply only with 4.'}]}

    results = []
    def complete(label, payload, cached=False):
        print(label + ': processing...', flush=True)
        response = api('/v1/chat/completions', payload)
        assert response['choices'][0]['message']['content'].strip() == '4', response
        timings = response['timings']
        if cached:
            ratio = timings['cache_n'] / (timings['cache_n'] + timings['prompt_n'])
            assert ratio > .9, ('Expected cached prefix', label, timings)
        results.append({'label': label, 'timings': timings})
        print('%s: cached=%d, evaluated=%d, prefill=%.2fs' %
              (label, timings['cache_n'], timings['prompt_n'], timings['prompt_ms']/1000), flush=True)

    a, b = body('A'), body('B')
    complete('A cold', a)
    complete('B cold', b)
    complete('A indexed return', a, cached=True)
    before = status()
    assert before['entries'] >= 2 and before['disk_restore_hits'] >= 1, before
    assert max(e['hits'] for e in before['snapshots']) >= 2, before
    assert before['hot_mapped_bytes'] <= before['hot_limit_bytes'], before
    assert before['disk_bytes'] <= before['disk_limit_bytes'], before
    # Wait for A's latest completed state to be saved before testing process restart.
    deadline = time.monotonic() + 180
    while True:
        state = status()
        if any(e['hits'] >= 2 for e in state['snapshots']) and state.get('active_snapshot'):
            # Touch counters are persisted immediately; the old A snapshot is also valid.
            break
        if time.monotonic() > deadline:
            raise RuntimeError('Snapshot not indexed')
        time.sleep(.5)
    print('Restarting; checking indexed return to B...', flush=True)
    restart()
    complete('B after restart', b, cached=True)
    after = status()
    assert after['entries'] >= 2, after
    report = {'passed': True, 'backend': backend,
              'actual_context': props['default_generation_settings']['n_ctx'],
              'requests': results, 'before_restart': before, 'after_restart': after,
              'counter_persisted': max(e['hits'] for e in after['snapshots']) >= 2}
    Path(output).write_text(json.dumps(report, indent=2) + '\n')
    print('PASS: two independent contexts reused, counters persisted, restart restore passed.', flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8080)
    options = parser.parse_args()
    key = (RUNTIME / 'server/api-key').read_text().strip()
    def restart():
        subprocess.run([str(ROOT / 'daemon.command'), 'restart'], check=True)
    check_indexed(options.port, key, restart, ROOT / 'logs/indexed-cache-gpu-check.json')
