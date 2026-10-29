"""Request-aware disk snapshots with exact token-prefix selection."""
from platform_support import FileLock
import json
import os
from pathlib import Path
import threading
import time
from cache_index import CacheIndex, common_prefix, unpacked
from slot_cache import SlotCache, log


class TieredCache(SlotCache):
    def __init__(self, directory, port, key, fingerprint, settings):
        super().__init__(directory, port, key, fingerprint)
        self.process_lock = FileLock(self.directory / 'owner.lock')
        self.lock = threading.RLock()
        try:
            self.store = CacheIndex(directory, fingerprint,
                disk_bytes=settings.get('kv_disk_mib', 8192) * 2**20,
                hot_bytes=settings.get('kv_hot_mib', 1024) * 2**20,
                max_entries=settings.get('kv_max_entries', 16), ttl_days=settings.get('kv_ttl_days', 30))
        except BaseException:
            self.process_lock.close()
            raise
        self.minimum = settings.get('kv_min_tokens', 512)
        self.experimental_reuse = settings.get('cache_reuse_tokens', 0)
        self.active_only_shift = settings.get('kv_shift_active_only', False)
        self.active_tokens = []
        self.active_id = None
        self.pending_hits = 1
        self.dirty = False
        self.retry_after = 0
        self.requests = self.restore_hits = self.active_hits = self.misses = 0

    def restore_entry(self, entry):
        path = self.directory / entry['filename']
        if not self.store.safe_name(entry['filename']) or path.stat().st_size != entry['bytes']:
            raise ValueError('Invalid snapshot file')
        started = time.monotonic()
        result = self.api('/slots/0?action=restore', {'filename': entry['filename']})
        if result.get('n_restored', 0) <= 0:
            raise ValueError('Snapshot restored no tokens')
        self.active_tokens = unpacked(entry['input_tokens'])
        self.active_id = entry['id']
        self.dirty = False
        log('restored indexed snapshot %s (%d tokens, %.2fs)' %
            (entry['id'][:10], result['n_restored'], time.monotonic() - started))
        return result

    def restore(self):
        entries = sorted(self.store.rows(compatible=True), key=lambda e: e['last_used'], reverse=True)
        for entry in entries:
            try:
                return self.restore_entry(entry)
            except (OSError, ValueError, KeyError) as exc:
                log('discarding invalid snapshot: ' + str(exc))
                self.store.remove(entry)
                self.invalidate()
        log('no compatible indexed snapshot; cold start')
        return None

    def tokenize(self, path, body):
        if body.get('cache_prompt', True) is False or body.get('n', 1) != 1 or body.get('lora'):
            return None
        if path.rstrip('/') in ('/v1/chat/completions', '/chat/completions'):
            prompt = self.api('/apply-template', body)['prompt']
        elif path.rstrip('/') in ('/completion', '/v1/completions', '/completions'):
            prompt = body.get('prompt')
            if prompt is None or (isinstance(prompt, list) and prompt and isinstance(prompt[0], list)):
                return None
        else:
            return None
        tokens = self.api('/tokenize', {'content': prompt, 'add_special': True, 'parse_special': True})['tokens']
        if not all(isinstance(t, int) for t in tokens):
            return None
        return tokens

    def prepare(self, tokens):
        """Called under the request lock, before forwarding inference."""
        self.requests += 1
        if tokens is None:
            if self.dirty:
                self.save_if_idle()
            return None
        active_common = common_prefix(self.active_tokens, tokens)
        matches = self.store.candidates(tokens, self.minimum)
        chosen = matches[0] if matches else None
        if chosen and chosen[0] > active_common:
            # Preserve the outgoing branch before replacing GPU state.
            self.save_if_idle(protect=chosen[1]['id'])
            entry = self.store.entry(chosen[1]['id'])
            if entry:
                try:
                    self.restore_entry(entry)
                    self.restore_hits += 1
                    self.store.prune(protected=entry['id'])
                    return entry['id']
                except (OSError, ValueError, KeyError) as exc:
                    log('indexed restore failed; falling back to recomputation: ' + str(exc))
                    self.store.remove(entry)
                    self.invalidate()
                    return None
        if active_common >= self.minimum:
            self.active_hits += 1
            return self.active_id
        if self.dirty:
            self.save_if_idle()
        self.misses += 1
        return None

    def completed(self, tokens, used_entry):
        if tokens is None:
            self.invalidate()
            return
        if used_entry and self.store.entry(used_entry):
            self.store.touch(used_entry)
            self.pending_hits = self.store.entry(used_entry)['hits']
        elif common_prefix(tokens, self.active_tokens) >= self.minimum:
            self.pending_hits += 1
        else:
            self.pending_hits = 1
        self.active_tokens = tokens
        # A new input may be a continuation/branch. Its saved key will be recomputed.
        self.active_id = used_entry
        self.dirty = True

    def invalidate(self):
        self.active_tokens = []
        self.active_id = None
        self.dirty = False
        self.pending_hits = 1

    def save_if_idle(self, minimum_tokens=None, protect=None):
        minimum = self.minimum if minimum_tokens is None else minimum_tokens
        if not self.dirty or len(self.active_tokens) < minimum:
            return False
        slots = self.api('/slots', timeout=3)
        slot = next(s for s in slots if s['id'] == 0)
        if slot['is_processing'] or slot.get('n_prompt_tokens', 0) < minimum:
            return False
        filename = 'slot-%d.bin' % time.time_ns()
        path = self.directory / filename
        committed = False
        try:
            result = self.api('/slots/0?action=save', {'filename': filename})
            if result.get('n_saved', 0) < minimum or result.get('n_written', 0) <= 0:
                raise ValueError('Empty snapshot')
            path.chmod(0o600)
            size = path.stat().st_size
            if size > self.store.disk_bytes:
                log('snapshot exceeds disk budget; keeping previous indexed contexts')
                self.dirty = False
                return False
            with path.open('r+b') as f:
                os.fsync(f.fileno())
            metadata = {'fingerprint': self.fingerprint, 'filename': filename, 'bytes': size,
                        'tokens': result['n_saved'], 'saved_at': time.time(),
                        'save_ms': result['timings']['save_ms']}
            ancestors = []
            for entry in self.store.rows(compatible=True):
                old_tokens = unpacked(entry['input_tokens'])
                if old_tokens and len(old_tokens) < len(self.active_tokens) and common_prefix(old_tokens, self.active_tokens) == len(old_tokens):
                    ancestors.append(entry)
            inherited = max([self.pending_hits] + [e['hits'] for e in ancestors])
            self.active_id = self.store.commit(metadata, self.active_tokens, inherited)
            committed = True
            # Maintain the old pointer for existing health/restart-check tooling.
            temporary = self.index.with_suffix('.tmp')
            temporary.write_text(json.dumps(metadata, indent=2) + '\n')
            temporary.chmod(0o600)
            with temporary.open('r+b') as f:
                os.fsync(f.fileno())
            temporary.replace(self.index)
            self.dirty = False
            for entry in ancestors:
                self.store.remove(entry)
            self.store.prune(protected={self.active_id, protect} - {None})
            log('indexed %d tokens, %.1f MiB; %d disk snapshots, %d hot mappings' %
                (metadata['tokens'], size / 2**20, len(self.store.rows()), len(self.store.hot)))
            return True
        finally:
            if not committed:
                path.unlink(missing_ok=True)

    def status(self):
        with self.lock:
            return {**self.store.status(), 'requests': self.requests,
                    'disk_restore_hits': self.restore_hits, 'active_prefix_hits': self.active_hits,
                    'misses': self.misses, 'active_snapshot': self.active_id,
                    'policy': 'longest exact token prefix; decayed frequency + recency eviction',
                    'experimental_chunk_reuse': bool(self.experimental_reuse),
                    'chunk_reuse_min_tokens': self.experimental_reuse,
                    'active_only_shift': self.active_only_shift,
                    'native_ram_cache_mib': 0}

    def close(self):
        self.store.close()
        self.process_lock.close()
