import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import windows
from slot_cache import identity


class WindowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'project with spaces'
        self.root.mkdir()
        self.runtime = self.root / 'runtime'
        self.config = windows.configure(self.runtime)
        self.binary = self.root / 'llama-server.exe'
        self.binary.write_bytes(b'native')
        (self.root / 'model.gguf').write_bytes(b'model')
        self.config.update(native_binary=str(self.binary), model='model.gguf')

    def tearDown(self): self.temp.cleanup()

    def test_native_gpu_selection_and_moltenvk_flags_are_removed(self):
        listing = subprocess.CompletedProcess([], 0, 'Vulkan0: NVIDIA test\nVulkan1: AMD test\n', '')
        with patch.dict(os.environ, {'VK_ICD_FILENAMES': 'Mac driver',
                                    'GGML_VK_AMD_MOLTENVK_FA_SHARED': '1', 'LLAMA_KV_SHIFT_ACTIVE_ONLY': '1'}), \
             patch.object(windows.subprocess, 'run', return_value=listing):
            args, env = windows.command(self.config, self.runtime, root=self.root)
            self.assertEqual(args[args.index('--device') + 1], 'Vulkan0')
            self.assertEqual(args[args.index('--port') + 1], '8081')
            self.assertNotIn('--cache-reuse', args)
            self.assertNotIn('VK_ICD_FILENAMES', env)
            self.assertNotIn('GGML_VK_AMD_MOLTENVK_FA_SHARED', env)
            self.assertIn(str(self.binary.parent), env['PATH'])
            self.config['gpu_device'] = 'Vulkan1'
            args, _ = windows.command(self.config, self.runtime, root=self.root)
            self.assertEqual(args[args.index('--device') + 1], 'Vulkan1')

    def test_dll_change_invalidates_disk_cache_and_cpu_needs_no_gpu_probe(self):
        with patch.object(windows.subprocess, 'run', side_effect=AssertionError('No GPU probe on CPU')):
            args, _ = windows.command(self.config, self.runtime, cpu=True, root=self.root)
        original = identity(args)
        (self.root / 'ggml-vulkan.dll').write_bytes(b'changed backend')
        self.assertNotEqual(identity(args), original)

    def test_configuration_preserves_key_and_context(self):
        first = (self.runtime / 'api-key').read_bytes()
        self.config['context'] = 4096
        (self.runtime / 'config.json').write_text(json.dumps(self.config))
        result = windows.configure(self.runtime)
        self.assertEqual(result['context'], 4096)
        self.assertEqual((self.runtime / 'api-key').read_bytes(), first)

    def test_bad_ports_and_unvalidated_chunk_reuse_fail_before_start(self):
        self.config['kv_backend_port'] = self.config['port']
        with self.assertRaises(ValueError): windows.command(self.config, self.runtime, cpu=True, root=self.root)
        self.config['cache_reuse_tokens'] = 64
        with self.assertRaises(RuntimeError): windows.command(self.config, self.runtime, cpu=True, root=self.root)

    def test_logon_task_quotes_paths_and_uses_current_user_without_password(self):
        script = windows.task_script('install', Path("C:/Users/Test Person/App Data/LLM's cache"))
        self.assertIn("LLM''s cache", script)
        self.assertIn('-LogonType Interactive -RunLevel Limited', script)
        self.assertIn('-AtLogOn', script)
        self.assertNotIn('-Password', script)

    def test_opencode_limit_uses_actual_context_after_vram_fit(self):
        with patch.object(windows, 'api', return_value=lambda *a, **k: [{'n_ctx': 8192}]):
            path = windows.opencode_config(self.config, self.runtime)
        body = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(body['providers']['radeon']['models']['qwen3-4b-local']['limit']['context'], 8192)
        self.assertNotIn((self.runtime / 'api-key').read_text(), path.read_text())

    def test_background_startup_failure_is_saved_before_stderr_is_detached(self):
        with patch.object(windows, 'WINDOWS', True), \
             patch.object(sys, 'argv', ['windows.py', 'run', '--runtime', str(self.runtime), '--background']), \
             patch.object(windows, 'run', side_effect=RuntimeError('GPU unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'GPU unavailable'):
                windows.main()
        self.assertIn('GPU unavailable', (self.runtime / 'logs/server.log').read_text(encoding='utf-8'))


if __name__ == '__main__': unittest.main()
