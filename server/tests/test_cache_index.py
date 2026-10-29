import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cache_index import CacheIndex


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.index = CacheIndex(self.root, 'model-a', disk_bytes=12288, hot_bytes=8192, max_entries=3)
        self.counter = 0

    def tearDown(self):
        self.index.close()
        self.tmp.cleanup()

    def add(self, tokens, hits=1):
        self.counter += 1
        name = 'slot-%d.bin' % self.counter
        (self.root / name).write_bytes(bytes([self.counter]) * 4096)
        return self.index.commit({'filename': name, 'tokens': len(tokens), 'bytes': 4096, 'save_ms': 1}, tokens, hits)

    def test_longest_exact_prefix_and_model_isolation(self):
        a = list(range(800))
        b = a[:64] + [999] * 736
        aid = self.add(a)
        self.add(b)
        matches = self.index.candidates(a + [900])
        self.assertEqual([(n, e['id']) for n, e in matches], [(800, aid)])
        self.index.fingerprint = 'other-model'
        self.assertEqual(self.index.candidates(a), [])

    def test_decay_frequency_eviction_and_hot_budget(self):
        a = self.add([1] * 800, hits=10)
        b = self.add([2] * 800)
        c = self.add([3] * 800)
        with self.index.db:
            self.index.db.execute('UPDATE snapshots SET last_used=? WHERE id=?', (time.time()-43200, b))
        self.index.max_entries = 2
        self.index.prune(protected=c)
        self.assertIsNotNone(self.index.entry(a))
        self.assertIsNone(self.index.entry(b))
        self.assertLessEqual(self.index.status()['hot_mapped_bytes'], 8192)
        self.assertEqual(self.index.status()['hot_entries'], 2)
        self.assertFalse((self.root / 'slot-2.bin').exists())

    def test_expiry_and_recovery_of_orphan_and_broken_file(self):
        a = self.add([1] * 800)
        with self.index.db:
            self.index.db.execute('UPDATE snapshots SET last_used=? WHERE id=?', (time.time()-31*86400, a))
        self.index.prune()
        self.assertIsNone(self.index.entry(a))
        b = self.add([2] * 800)
        (self.root / 'slot-2.bin').write_bytes(b'truncated')
        (self.root / 'slot-orphan.bin').write_bytes(b'uncommitted')
        self.index.reconcile()
        self.assertIsNone(self.index.entry(b))
        self.assertFalse((self.root / 'slot-orphan.bin').exists())

    def test_legacy_migration_and_restart(self):
        name = 'slot-legacy.bin'
        (self.root / name).write_bytes(b'valid')
        (self.root / 'latest.json').write_text(json.dumps({'filename':name,'bytes':5,'tokens':800,
                                                        'fingerprint':'model-a','saved_at':time.time()}))
        self.index.migrate_latest()
        self.assertEqual(len(self.index.rows()), 1)
        self.index.close()
        self.index = CacheIndex(self.root, 'model-a')
        self.assertEqual(self.index.rows()[0]['tokens'], 800)
        self.assertEqual(self.index.candidates([1]*800), [])

    def test_replacement_keeps_hits_and_only_one_file(self):
        a = self.add([1] * 800, 7)
        self.index.touch(a)
        b = self.add([1] * 800)
        self.assertEqual(a, b)
        self.assertEqual(self.index.entry(a)['hits'], 8)
        self.assertEqual(len(self.index.rows()), 1)
        self.assertFalse((self.root / 'slot-1.bin').exists())

    def test_incoming_restore_target_is_not_evicted_during_outgoing_save(self):
        a = self.add([1] * 800)
        b = self.add([2] * 800)
        c = self.add([3] * 800)
        self.index.max_entries = 2
        self.index.prune(protected={a,c})
        self.assertIsNotNone(self.index.entry(a))
        self.assertIsNotNone(self.index.entry(c))
        self.assertIsNone(self.index.entry(b))


if __name__ == '__main__':
    unittest.main()
