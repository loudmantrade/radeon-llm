"""CI-only relocation smoke test: real native files, no model or login service."""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

from setup_local import copy_windows_runtime, ROOT
from warm_opencode import private_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',choices=['vulkan','metal'],required=True)
    opts=parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS')!='true':
        parser.error('This smoke test runs only in a fresh GitHub Actions checkout')
    with tempfile.TemporaryDirectory(prefix='radeon-install-smoke-') as temporary:
        runtime=Path(temporary)
        if platform.system()=='Windows':
            binary=copy_windows_runtime(ROOT/'llama.cpp/build-setup-vulkan',runtime)
            import windows
            env=windows.environment(binary)
        else:
            import daemon
            config_path=ROOT/'server/config.json'
            key=ROOT/'server/api-key'
            if config_path.exists() or key.exists():
                raise RuntimeError('Refusing to overwrite an existing configuration/key for smoke testing')
            model=ROOT/'work/setup-smoke-model.gguf'
            model.parent.mkdir(exist_ok=True);model.write_bytes(b'CI placeholder; never loaded as a model')
            config=json.loads((ROOT/'server/config.example.json').read_text())
            config.update(backend=opts.backend,model=str(model.relative_to(ROOT)),python=sys.executable,
                          native_binary='llama.cpp/build-%s/bin/llama-server'%opts.backend)
            try:
                private_json(config_path,config);key.write_text('CI-smoke-not-a-production-key')
                daemon.CONFIG=config;daemon.BIN=ROOT/config['native_binary']
                daemon.deploy(runtime)
                binary=runtime/config['native_binary']
                env=dict(os.environ,DYLD_LIBRARY_PATH=str(binary.parent))
                for name in ('VK_DRIVER_FILES','VK_ICD_FILENAMES'):env.pop(name,None)
                if opts.backend=='vulkan':
                    driver=runtime/'moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS'
                    env['DYLD_LIBRARY_PATH']=':'.join([str(driver),str(runtime/'vulkan-sdk/macOS/lib'),str(binary.parent)])
                    env['VK_DRIVER_FILES']=str(driver/'MoltenVK_icd.json')
                    env['VK_ICD_FILENAMES']=env['VK_DRIVER_FILES']
            finally:
                config_path.unlink(missing_ok=True);key.unlink(missing_ok=True);model.unlink(missing_ok=True)
        subprocess.run([str(binary),'--version'],env=env,check=True,timeout=30)
        print('Installed native binary/DLLs load successfully from the staged runtime.')


if __name__=='__main__':main()
