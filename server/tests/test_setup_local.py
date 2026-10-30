import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_local as setup
import bootstrap


class SetupTests(unittest.TestCase):
    def test_backend_matches_host_and_rejects_cross_platform_choice(self):
        self.assertEqual(setup.backend_for('Darwin', 'arm64'), 'metal')
        self.assertEqual(setup.backend_for('Darwin', 'x86_64'), 'vulkan')
        self.assertEqual(setup.backend_for('Darwin', 'arm64', 'vulkan'), 'vulkan')
        self.assertEqual(setup.backend_for('Windows', 'AMD64'), 'vulkan')
        for system, machine, backend in [('Windows','AMD64','metal'), ('Linux','x86_64','auto'), ('Windows','ARM64','auto')]:
            with self.assertRaises(RuntimeError): setup.backend_for(system, machine, backend)

    def test_plan_does_not_install_build_or_read_host_dependency_state(self):
        with patch.object(sys, 'argv', ['setup_local.py','--plan']), \
             patch.object(setup.platform, 'system', return_value='Darwin'), \
             patch.object(setup.platform, 'machine', return_value='arm64'), \
             patch.object(setup, 'dependencies') as dependencies, \
             patch.object(bootstrap, 'build') as build, patch.object(setup, 'install') as install, \
             patch.object(setup.sys, 'stdout', io.StringIO()):
            self.assertEqual(setup.main(), 0)
            dependencies.assert_not_called(); build.assert_not_called(); install.assert_not_called()

    def test_build_only_preserves_configuration_model_and_service(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); (root/'config.json').write_text('existing configuration')
            with patch.object(setup, 'ROOT', root), \
                 patch.object(sys, 'argv', ['setup_local.py','--build-only','--no-deps']), \
                 patch.object(setup.platform, 'system', return_value='Windows'), \
                 patch.object(setup.platform, 'machine', return_value='AMD64'), \
                 patch.object(setup, 'dependencies'), patch.object(bootstrap, 'build') as build, \
                 patch.object(bootstrap, 'download') as download, patch.object(setup, 'install') as install, \
                 patch.object(setup.sys, 'stdout', io.StringIO()):
                self.assertEqual(setup.main(), 0)
                self.assertEqual(build.call_args.args[0], 'vulkan')
                download.assert_not_called(); install.assert_not_called()
                self.assertEqual((root/'config.json').read_text(), 'existing configuration')

    def test_no_deps_fails_before_running_package_manager(self):
        with patch.object(setup.shutil, 'which', return_value=None), \
             patch.object(setup, 'windows_sdk', return_value=None), \
             patch.object(setup, 'windows_compiler', return_value=False), \
             patch.object(setup, 'winget') as winget:
            with self.assertRaisesRegex(RuntimeError, 'Missing dependencies'):
                setup.dependencies('Windows', 'vulkan', no_deps=True)
            winget.assert_not_called()

    def test_verified_dependency_rejects_bad_download_and_reuses_valid_cache(self):
        content=b'verified dependency';digest=hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as name:
            destination=Path(name)/'dependency.zip'
            with patch.object(setup.urllib.request, 'urlopen', return_value=io.BytesIO(content)) as open_url:
                setup.download_verified('https://vendor.invalid/dependency.zip', digest, destination)
                self.assertEqual(open_url.call_args.args[0].get_header('User-agent'),'radeon-llm-setup/1.0')
            with patch.object(setup.urllib.request, 'urlopen', side_effect=AssertionError('Must not download again')):
                self.assertEqual(setup.download_verified('https://vendor.invalid/dependency.zip', digest, destination), destination)
            with patch.object(setup.urllib.request, 'urlopen', return_value=io.BytesIO(b'corrupt')):
                with self.assertRaisesRegex(RuntimeError, 'SHA-256 mismatch'):
                    setup.download_verified('https://vendor.invalid/other.zip', digest, Path(name)/'other.zip')
            self.assertFalse((Path(name)/'other.zip').exists())

    def test_existing_mac_context_and_api_key_are_preserved(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)/'repo';home=Path(name)/'home';(root/'server').mkdir(parents=True)
            old=home/'Library/Application Support/radeon-llm/server';old.mkdir(parents=True)
            (old/'config.json').write_text(json.dumps({'context':8192,'alias':'custom','cache_reuse_tokens':64}))
            (old/'api-key').write_text('existing-test-key')
            with patch.object(setup, 'ROOT', root), patch.object(Path, 'home', return_value=home):
                config=setup.configure_mac('metal')
            self.assertEqual(config['context'],8192)
            self.assertEqual(config['alias'],'custom')
            self.assertEqual(config['cache_reuse_tokens'],0)
            self.assertEqual(config['native_binary'],'llama.cpp/build-metal/bin/llama-server')
            self.assertEqual((root/'server/api-key').read_text(),'existing-test-key')

    def test_macos_no_deps_never_runs_sdk_installer(self):
        with tempfile.TemporaryDirectory() as name, patch.object(setup, 'ROOT', Path(name)), \
             patch.object(setup, 'download_verified') as download, patch.object(setup, 'invoke') as invoke:
            with self.assertRaisesRegex(RuntimeError, 'Vulkan SDK'):
                setup.mac_vulkan(no_deps=True)
            download.assert_not_called();invoke.assert_not_called()

    def test_bootstrap_build_keeps_backend_outputs_separate(self):
        with tempfile.TemporaryDirectory() as name, \
             patch.object(bootstrap,'source',return_value=Path(name)), \
             patch.object(bootstrap.subprocess,'run') as run:
            bootstrap.build('metal',2)
            args=run.call_args_list[0].args[0]
            self.assertIn('-DGGML_VULKAN=OFF',args)
            self.assertIn('-DGGML_METAL=ON',args)
            self.assertIn('-DGGML_METAL_EMBED_LIBRARY=ON',args)
            self.assertIn(str(Path(name)/'build-metal'),args)
            self.assertEqual(run.call_args_list[1].args[0][-1],'llama-server')

    def test_mac_install_checks_installed_key_and_configured_alias(self):
        with tempfile.TemporaryDirectory() as name, patch.object(Path,'home',return_value=Path(name)), \
             patch.object(setup,'configure_mac',return_value={'port':8099,'alias':'custom-model'}), \
             patch.object(setup,'invoke') as invoke:
            setup.install('Darwin','metal')
            self.assertEqual(invoke.call_args_list[0].args[0][-1],'install')
            check=invoke.call_args_list[1].args[0]
            self.assertEqual(check[check.index('--key-file')+1],Path(name)/'Library/Application Support/radeon-llm/server/api-key')
            self.assertEqual(check[check.index('--model')+1],'custom-model')

    def test_windows_install_backs_up_config_and_checks_runtime_key(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);runtime=root/'runtime';runtime.mkdir()
            config={'model':'models/model.gguf','port':8080,'alias':'custom-model','context':8192}
            (runtime/'config.json').write_text(json.dumps(config))
            (root/'models').mkdir();(root/'models/model.gguf').write_bytes(b'model')
            (root/'models/qwen3-source.json').write_text(json.dumps({'file':'model.gguf'}))
            binary=root/'llama.cpp/build-setup-vulkan/bin/Release';binary.mkdir(parents=True)
            (binary/'llama-server.exe').write_bytes(b'native');(binary/'ggml.dll').write_bytes(b'library')
            windows=SimpleNamespace(DEFAULT_RUNTIME=runtime,configure=Mock(return_value=dict(config)),
                binary_path=Mock(return_value=root/'llama-server.exe'),opencode_config=Mock())
            with patch.dict(sys.modules,{'windows':windows}),patch.object(setup,'ROOT',root), \
                 patch.object(setup,'invoke') as invoke:
                setup.install('Windows','vulkan')
            self.assertEqual(len(list(runtime.glob('config.before-setup-*.json'))),1)
            saved=json.loads((runtime/'config.json').read_text())
            self.assertTrue(Path(saved['model']).is_absolute())
            self.assertEqual(saved['context'],8192)
            self.assertEqual(invoke.call_args_list[0].args[0][2],'stop')
            self.assertEqual(invoke.call_args_list[1].args[0][2],'install')
            self.assertEqual((runtime/'native/ggml.dll').read_bytes(),b'library')
            self.assertEqual(saved['native_binary'],str(runtime/'native/llama-server.exe'))
            check=invoke.call_args_list[-1].args[0]
            self.assertEqual(check[check.index('--key-file')+1],runtime/'api-key')


if __name__=='__main__': unittest.main()
