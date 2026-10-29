"""SQLite snapshot index and bounded, page-warmed mmap hot set."""
import array
import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import sqlite3
import time
import zlib


def common_prefix(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return min(len(a), len(b))


def packed(tokens):
    data = array.array('i', tokens)
    if data.itemsize != 4:
        raise RuntimeError('32-bit token storage required')
    if __import__('sys').byteorder != 'little':
        data.byteswap()
    return data.tobytes()


def unpacked(data):
    result = array.array('i')
    result.frombytes(zlib.decompress(data))
    if __import__('sys').byteorder != 'little':
        result.byteswap()
    return result.tolist()


def score(entry, now):
    # Recency decays historical popularity rather than letting old hits dominate forever.
    age = max(0, now - entry['last_used']) / 86400
    return (1 + math.log2(1 + entry['hits'])) / (1 + age)


class CacheIndex:
    def __init__(self, directory, fingerprint, disk_bytes=8*2**30, hot_bytes=2**30,
                 max_entries=16, ttl_days=30):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        if disk_bytes <= 0 or hot_bytes < 0 or max_entries < 1 or ttl_days <= 0:
            raise ValueError('Invalid cache limits')
        self.fingerprint = fingerprint
        self.disk_bytes, self.hot_bytes = disk_bytes, hot_bytes
        self.max_entries, self.ttl_seconds = max_entries, ttl_days * 86400
        self.db = sqlite3.connect(str(self.directory / 'index.sqlite3'), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS snapshots (
            id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, filename TEXT UNIQUE NOT NULL,
            input_tokens BLOB NOT NULL, prefix_hash TEXT NOT NULL, tokens INTEGER NOT NULL,
            bytes INTEGER NOT NULL, created REAL NOT NULL, last_used REAL NOT NULL,
            hits INTEGER NOT NULL, save_ms REAL NOT NULL)''')
        self.db.execute('CREATE INDEX IF NOT EXISTS prefix_lookup ON snapshots(fingerprint,prefix_hash)')
        self.db.commit()
        (self.directory / 'index.sqlite3').chmod(0o600)
        self.hot = {}
        self.migrate_latest()
        self.reconcile()

    @staticmethod
    def safe_name(name):
        return isinstance(name, str) and Path(name).name == name and name.startswith('slot-') and name.endswith('.bin')

    def rows(self, compatible=False):
        if compatible:
            rows = self.db.execute('SELECT * FROM snapshots WHERE fingerprint=?', (self.fingerprint,))
        else:
            rows = self.db.execute('SELECT * FROM snapshots')
        return [dict(row) for row in rows]

    def migrate_latest(self):
        legacy = self.directory / 'latest.json'
        if self.db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] or not legacy.exists():
            return
        try:
            old = json.loads(legacy.read_text())
            filename = old['filename']
            if not self.safe_name(filename) or (self.directory / filename).stat().st_size != old['bytes']:
                return
            stamp = old.get('saved_at', time.time())
            # Legacy snapshots have no input-token index. They remain usable for startup restore.
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    ('legacy-' + filename, old['fingerprint'], filename, zlib.compress(packed([])), '',
                     old['tokens'], old['bytes'], stamp, stamp, 0, old.get('save_ms', 0)))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def reconcile(self):
        for entry in self.rows():
            name = entry['filename']
            try:
                valid = self.safe_name(name) and (self.directory / name).stat().st_size == entry['bytes']
            except OSError:
                valid = False
            if not valid:
                self.remove(entry)
        known = {e['filename'] for e in self.rows()}
        # A crash between file write and index commit can leave an unreferenced file.
        for path in self.directory.glob('slot-*.bin'):
            if path.name not in known:
                path.unlink()

    def commit(self, metadata, tokens, inherited_hits=1):
        raw = packed(tokens)
        entry_id = hashlib.sha256(self.fingerprint.encode() + raw).hexdigest()
        prefix_hash = hashlib.sha256(packed(tokens[:64])).hexdigest()
        old = self.db.execute('SELECT * FROM snapshots WHERE id=?', (entry_id,)).fetchone()
        now = time.time()
        created = old['created'] if old else now
        hits = max(old['hits'] if old else 0, inherited_hits)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (entry_id, self.fingerprint, metadata['filename'], zlib.compress(raw), prefix_hash,
                 metadata['tokens'], metadata['bytes'], created, now, hits, metadata['save_ms']))
        if old and old['filename'] != metadata['filename']:
            try:
                self.drop_file(old['filename'])
            except OSError:
                pass  # Reconcile the orphan on the next startup; the new row is committed.
        return entry_id

    def entry(self, entry_id):
        row = self.db.execute('SELECT * FROM snapshots WHERE id=?', (entry_id,)).fetchone()
        return dict(row) if row else None

    def candidates(self, tokens, minimum=512):
        if len(tokens) < minimum:
            return []
        prefix_hash = hashlib.sha256(packed(tokens[:64])).hexdigest()
        rows = self.db.execute('SELECT * FROM snapshots WHERE fingerprint=? AND prefix_hash=?',
                               (self.fingerprint, prefix_hash))
        matches = []
        for row in rows:
            entry = dict(row)
            try:
                count = common_prefix(unpacked(entry['input_tokens']), tokens)
            except (ValueError, zlib.error):
                self.remove(entry)
                continue
            if count >= minimum:
                matches.append((count, entry))
        return sorted(matches, key=lambda pair: (pair[0], score(pair[1], time.time())), reverse=True)

    def touch(self, entry_id):
        with self.db:
            self.db.execute('UPDATE snapshots SET hits=hits+1,last_used=? WHERE id=?', (time.time(), entry_id))

    def drop_file(self, filename):
        mapping = self.hot.pop(filename, None)
        if mapping is not None:
            mapping.close()
        if self.safe_name(filename):
            (self.directory / filename).unlink(missing_ok=True)

    def remove(self, entry):
        with self.db:
            self.db.execute('DELETE FROM snapshots WHERE id=?', (entry['id'],))
        self.drop_file(entry['filename'])

    def prune(self, protected=None):
        protected = {protected} if isinstance(protected, str) else set(protected or ())
        now = time.time()
        entries = self.rows()
        for entry in list(entries):
            if entry['id'] not in protected and now - entry['last_used'] > self.ttl_seconds:
                self.remove(entry)
                entries.remove(entry)
        total = sum(e['bytes'] for e in entries)
        for entry in sorted(entries, key=lambda e: score(e, now)):
            if total <= self.disk_bytes and len(entries) <= self.max_entries:
                break
            if entry['id'] in protected:
                continue
            self.remove(entry)
            entries.remove(entry)
            total -= entry['bytes']
        self.warm()

    def warm(self):
        budget = self.hot_bytes
        selected = []
        for entry in sorted(self.rows(compatible=True), key=lambda e: score(e, time.time()), reverse=True):
            if entry['bytes'] <= budget:
                selected.append(entry)
                budget -= entry['bytes']
        names = {e['filename'] for e in selected}
        for name in list(self.hot):
            if name not in names:
                self.hot.pop(name).close()
        for entry in selected:
            name = entry['filename']
            if name in self.hot:
                continue
            mapping = None
            try:
                with (self.directory / name).open('rb') as f:
                    mapping = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                # Populate file-backed pages. They are shared with the native reader,
                # not copied into a second Python byte buffer. The OS may reclaim them.
                for offset in range(0, len(mapping), mmap.PAGESIZE):
                    _ = mapping[offset]
                self.hot[name] = mapping
            except OSError:
                if mapping is not None and name not in self.hot:
                    mapping.close()

    def status(self):
        entries = self.rows()
        return {'entries': len(entries), 'disk_bytes': sum(e['bytes'] for e in entries),
                'disk_limit_bytes': self.disk_bytes, 'entry_limit': self.max_entries,
                'hot_mapped_bytes': sum(len(m) for m in self.hot.values()),
                'hot_limit_bytes': self.hot_bytes, 'hot_entries': len(self.hot),
                'hot_mode': 'page-warmed file-backed mmap; residency managed by OS',
                'snapshots': [{key: entry[key] for key in
                    ('id', 'tokens', 'bytes', 'hits', 'created', 'last_used')}
                    | {'reuse_count': max(0, entry['hits'] - 1), 'hot': entry['filename'] in self.hot,
                       'compatible': entry['fingerprint'] == self.fingerprint} for entry in entries]}

    def close(self):
        for mapping in self.hot.values():
            mapping.close()
        self.hot.clear()
        self.db.close()
