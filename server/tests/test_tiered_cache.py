import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tiered_cache import TieredCache


class TieredTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = TieredCache(self.root, 1, 'test-key', 'fingerprint',
                                 {'kv_hot_mib': 0, 'kv_min_tokens': 512})
        self.fail_save = False
        self.restored = []
        self.cache.api = self.api

    def tearDown(self):
        self.cache.close()
        self.tmp.cleanup()

    def api(self, path, body=None, timeout=120):
        if path == '/slots':
            return [{'id':0, 'id_task':1, 'is_processing':False, 'n_prompt_tokens':900}]
        if 'action=save' in path:
            (self.root / body['filename']).write_bytes(b'valid' * 100)
            if self.fail_save:
                raise OSError('Interrupted save')
            return {'n_saved':900,'n_written':500,'timings':{'save_ms':1}}
        if 'action=restore' in path:
            self.restored.append(body['filename'])
            return {'n_restored':900, 'timings':{'restore_ms':1}}
        raise AssertionError(path)

    def save(self, tokens):
        self.cache.completed(tokens, None)
        self.assertTrue(self.cache.save_if_idle())
        return self.cache.active_id

    def test_failed_save_preserves_committed_index_and_snapshot(self):
        old = self.save([1]*800)
        previous = (self.root/'latest.json').read_bytes()
        self.cache.completed([2]*800, None)
        self.fail_save = True
        with self.assertRaises(OSError):
            self.cache.save_if_idle()
        self.assertEqual((self.root/'latest.json').read_bytes(), previous)
        self.assertIsNotNone(self.cache.store.entry(old))
        self.assertEqual(len(list(self.root.glob('slot-*.bin'))), 1)

    def test_switch_restore_counts_and_ancestor_deduplication(self):
        a = self.save([1]*800)
        self.save([2]*800)
        chosen = self.cache.prepare([1]*800)
        self.assertEqual(chosen, a)
        self.assertEqual(len(self.restored), 1)
        self.cache.completed([1]*800, chosen)
        self.cache.save_if_idle()
        self.assertEqual(self.cache.store.entry(a)['hits'], 2)
        self.cache.completed([1]*800+[3]*100, a)
        self.cache.save_if_idle()
        self.assertIsNone(self.cache.store.entry(a))
        self.assertEqual(len(self.cache.store.rows()), 2)
        self.assertGreaterEqual(self.cache.store.entry(self.cache.active_id)['hits'], 3)

    def test_exclusive_directory_owner(self):
        with self.assertRaises(BlockingIOError):
            TieredCache(self.root, 1, 'test-key', 'fingerprint', {})


if __name__ == '__main__':
    unittest.main()
