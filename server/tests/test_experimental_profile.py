import os
import unittest
if os.name == 'nt':
    raise unittest.SkipTest('macOS experimental installer and harness')
from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import daemon
import apply_experimental_reuse as installer
from slot_cache import identity


class ExperimentalProfileTests(unittest.TestCase):
    def test_mode_uses_active_shift_and_different_cache_identity(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); (root/'model.gguf').write_bytes(b'model'); (root/'native').write_bytes(b'native')
            config=dict(daemon.CONFIG, model='model.gguf')
            config.update(cache_reuse_tokens=0,kv_shift_active_only=False)
            with patch.object(daemon,'CONFIG',config), patch.object(daemon,'ROOT',root), patch.object(daemon,'BIN',root/'native'):
                exact,env=daemon.command(cpu=True,port=18097,cache_dir=name)
                self.assertNotIn('LLAMA_KV_SHIFT_ACTIVE_ONLY',env)
                config.update(cache_reuse_tokens=64,kv_shift_active_only=True)
                experimental,env=daemon.command(cpu=True,port=18097,cache_dir=name)
                self.assertEqual(experimental[experimental.index('--cache-reuse')+1],'64')
                self.assertEqual(env['LLAMA_KV_SHIFT_ACTIVE_ONLY'],'1')
                self.assertNotEqual(identity(exact),identity(experimental))

    def test_stop_waits_for_index_owner_when_pid_probe_forbidden(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);owner=root/'cache/kv/owner.lock'
            owner.parent.mkdir(parents=True);owner.write_text('')
            with patch.object(daemon,'RUNTIME',root), \
                 patch.object(daemon,'launchctl',side_effect=[SimpleNamespace(returncode=0,stdout='pid = 999999'),None]), \
                 patch.object(daemon,'loaded',side_effect=[False,False]), \
                 patch.object(daemon.socket,'create_connection',side_effect=OSError), \
                 patch.object(daemon.os,'kill',side_effect=PermissionError), \
                 patch.object(daemon.fcntl,'flock',side_effect=[BlockingIOError,None,None]) as lock, \
                 patch.object(daemon.time,'sleep'):
                daemon.stop_service()
                self.assertEqual(lock.call_count,3)

    def test_rollback_restores_clean_cache_native_build_and_source_config(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);runtime=root/'runtime';project=root/'project';backup=runtime/'backups/previous'
            project.mkdir();(project/'server').mkdir();backup.mkdir(parents=True)
            (runtime/'server').mkdir();(runtime/'cache/kv').mkdir(parents=True)
            (runtime/'experimental/shift-active/bin').mkdir(parents=True)
            (backup/'previous-shift-active-bin').mkdir()
            (backup/'kv').mkdir()
            (backup/'kv/slot-old.bin').write_bytes(b'exact snapshot')
            (runtime/'cache/kv/slot-experimental.bin').write_bytes(b'approximate snapshot')
            (backup/'previous-shift-active-bin/native').write_text('old native')
            (runtime/'experimental/shift-active/bin/native').write_text('new native')
            for file in installer.MODULES+['config.json']:
                (backup/file).write_text('old '+file)
                (runtime/'server'/file).write_text('new '+file)
            (backup/'source-config.json').write_text('old source profile')
            (project/'server/config.json').write_text('experimental source profile')
            with patch.object(installer,'ROOT',project),patch.object(installer,'RUNTIME',runtime), \
                 patch.object(installer,'prepare_runtime'),patch.object(installer,'manager') as manager:
                installer.rollback(backup)
                self.assertEqual([c.args[0] for c in manager.call_args_list],['stop','start'])
            self.assertEqual((runtime/'cache/kv/slot-old.bin').read_bytes(),b'exact snapshot')
            self.assertFalse((runtime/'cache/kv/slot-experimental.bin').exists())
            self.assertTrue(list(backup.glob('experimental-cache-*/slot-experimental.bin')))
            self.assertEqual((runtime/'experimental/shift-active/bin/native').read_text(),'old native')
            self.assertEqual((project/'server/config.json').read_text(),'old source profile')


if __name__=='__main__':unittest.main()
