#!/usr/bin/python3
import json
from pathlib import Path
import urllib.request
root = Path.home()/'Library/Application Support/radeon-llm'
config = json.loads((root/'server/config.json').read_text())
key = (root/'server/api-key').read_text().strip()
request = urllib.request.Request('http://127.0.0.1:%d/cache/status' % config['port'],
                                 headers={'Authorization':'Bearer '+key})
with urllib.request.urlopen(request,timeout=10) as response:
    result = json.load(response)
if result.get('busy'):
    print('Cache busy: inference or snapshot operation in progress.')
else:
    print('KV mode: '+('EXPERIMENTAL chunk reuse, min=%d tokens; active-only shift=%s' %
          (result['chunk_reuse_min_tokens'],result['active_only_shift'])
          if result.get('experimental_chunk_reuse') else 'exact prefix'))
    print('Disk: %d snapshots, %.1f / %.1f MiB' %
          (result['entries'],result['disk_bytes']/2**20,result['disk_limit_bytes']/2**20))
    print('Hot mmap: %d snapshots, %.1f / %.1f MiB (OS-managed residency)' %
          (result['hot_entries'],result['hot_mapped_bytes']/2**20,result['hot_limit_bytes']/2**20))
    print('Requests: %d; active-prefix hits: %d; indexed restores: %d; misses: %d' %
          (result['requests'],result['active_prefix_hits'],result['disk_restore_hits'],result['misses']))
    for entry in sorted(result['snapshots'],key=lambda e:e['last_used'],reverse=True):
        print('%s  %5d tokens  %6.1f MiB  reuses=%d  hot=%s  compatible=%s' %
              (entry['id'][:10],entry['tokens'],entry['bytes']/2**20,entry['reuse_count'],entry['hot'],entry['compatible']))
