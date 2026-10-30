import os
import unittest
if os.name=='nt': raise unittest.SkipTest('macOS launchd adapter')
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import daemon


class MacSetupTests(unittest.TestCase):
    def test_metal_command_uses_mtl_device_without_vulkan_environment(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);binary=root/'bin/llama-server';binary.parent.mkdir();binary.write_bytes(b'native')
            config=dict(daemon.CONFIG,backend='metal',amd_shared_fa=False,cache_reuse_tokens=0)
            listed=subprocess.CompletedProcess([],0,'Available devices:\n  MTL0: Apple M4\n','')
            with patch.object(daemon,'ROOT',root),patch.object(daemon,'BIN',binary), \
                 patch.object(daemon,'CONFIG',config),patch.object(daemon.subprocess,'run',return_value=listed):
                args,env=daemon.command()
            self.assertEqual(args[args.index('--device')+1],'MTL0')
            self.assertNotIn('VK_DRIVER_FILES',env)
            self.assertNotIn('GGML_VK_AMD_MOLTENVK_FA_SHARED',env)

    def test_launchagent_uses_configured_python(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);(root/'server').mkdir();key=root/'server/api-key';key.write_text('test')
            config=dict(daemon.CONFIG,python='/custom/python3.11')
            plist=root/'server/agent.plist'
            with patch.object(daemon,'ROOT',root),patch.object(daemon,'CONFIG',config), \
                 patch.object(daemon,'KEY',key),patch.object(daemon,'PLIST',plist): daemon.prepare()
            self.assertEqual(plistlib.loads(plist.read_bytes())['ProgramArguments'][0],'/custom/python3.11')

    def test_metal_deploy_needs_no_vulkan_bundle_and_resigns_changed_rpaths(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)/'repo';runtime=Path(name)/'runtime';(root/'server').mkdir(parents=True)
            (root/'models').mkdir();(root/'models/test.gguf').write_bytes(b'model');(root/'models/Qwen3-LICENSE').write_text('license')
            binary=root/'bin/llama-server';binary.parent.mkdir();binary.write_bytes(b'native')
            for filename in ['daemon.py','slot_cache.py','platform_support.py','cache_index.py','tiered_cache.py','cache_proxy.py']:
                (root/'server'/filename).write_text('# test module')
            key=root/'server/api-key';key.write_text('test')
            config=dict(daemon.CONFIG,backend='metal',model='models/test.gguf',native_binary='bin/llama-server',python='/custom/python')
            (root/'server/config.json').write_text(json.dumps(config))
            with patch.object(daemon,'ROOT',root),patch.object(daemon,'BIN',binary), \
                 patch.object(daemon,'__file__',str(root/'server/daemon.py')), \
                 patch.object(daemon,'CONFIG',config),patch.object(daemon,'KEY',key), \
                 patch.object(daemon.subprocess,'check_output',return_value=''),patch.object(daemon.subprocess,'run') as run:
                daemon.deploy(runtime)
            commands=[call.args[0] for call in run.call_args_list]
            self.assertTrue(any(cmd[0]=='/usr/bin/codesign' for cmd in commands))
            self.assertEqual(commands[-1][0],'/custom/python')
            self.assertFalse((runtime/'vulkan-sdk').exists())


if __name__=='__main__': unittest.main()
