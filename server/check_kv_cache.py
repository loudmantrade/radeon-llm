#!/usr/bin/python3
"""Verify persistent prefix reuse through an actual installed-daemon restart."""
import json
from pathlib import Path
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = Path.home() / 'Library/Application Support/radeon-llm'
CONFIG = json.loads((RUNTIME / 'server/config.json').read_text())
KEY = (RUNTIME / 'server/api-key').read_text().strip()
CACHE = RUNTIME / 'cache/kv'


def complete(body):
    request = urllib.request.Request('http://127.0.0.1:%d/v1/chat/completions' % CONFIG['port'],
        data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + KEY,
                                                 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=300) as response:
        result = json.load(response)
    assert result['choices'][0]['message']['content'].strip() == '4', result
    return result['timings']


def main():
    assert CONFIG.get('persistent_kv'), 'Run ./daemon.command update-scripts first'
    body = {'model': CONFIG['alias'], 'temperature': 0, 'max_tokens': 8, 'cache_prompt': True,
            'messages': [{'role': 'system', 'content': 'Diagnostic %d\n' % time.time_ns() + '\n'.join(
                'Entry %04d: no action needed.' % i for i in range(70))},
                         {'role': 'user', 'content': 'What is 2+2? Reply only with 4.'}]}
    print('Checking cold prompt on installed Radeon daemon...', flush=True)
    started = time.time()
    cold = complete(body)
    deadline = time.monotonic() + 180
    while True:
        metadata = json.loads((CACHE / 'latest.json').read_text()) if (CACHE / 'latest.json').exists() else {}
        if metadata.get('saved_at', 0) >= started and metadata.get('tokens', 0) >= 512:
            break
        if time.monotonic() > deadline:
            raise RuntimeError('Automatic snapshot did not appear; inspect ./daemon.command logs')
        time.sleep(1)
    previous_pid = json.loads((CACHE / 'ready.json').read_text())['supervisor_pid']
    print('Saved %d tokens (%.1f MiB). Restarting daemon...' %
          (metadata['tokens'], metadata['bytes'] / 2**20), flush=True)
    subprocess.run([str(ROOT / 'daemon.command'), 'restart'], check=True)
    assert json.loads((CACHE / 'ready.json').read_text())['supervisor_pid'] != previous_pid
    restored = complete(body)
    fraction = restored['cache_n'] / (restored['cache_n'] + restored['prompt_n'])
    assert fraction > .9, ('Prefix was not restored', restored)
    report = {'passed': True, 'backend': 'installed Radeon daemon', 'context': CONFIG['context'],
              'cache_type_k': CONFIG.get('cache_type_k', 'f16'),
              'cache_type_v': CONFIG.get('cache_type_v', 'f16'),
              'snapshot_tokens': metadata['tokens'], 'snapshot_bytes': metadata['bytes'],
              'save_ms': metadata['save_ms'], 'cold_timings': cold, 'restored_timings': restored,
              'restored_cached_fraction': fraction}
    output = ROOT / 'logs/kv-persistence-gpu-check.json'
    output.write_text(json.dumps(report, indent=2) + '\n')
    print('PASS: after process restart, reused %d tokens and evaluated %d; prefill %.2fs -> %.2fs.' %
          (restored['cache_n'], restored['prompt_n'], cold['prompt_ms'] / 1000,
           restored['prompt_ms'] / 1000), flush=True)
    print('Report: ' + str(output), flush=True)


if __name__ == '__main__':
    main()
