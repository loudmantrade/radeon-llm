#!/usr/bin/python3
"""Package an isolated native build; do not replace the tested FA bundle."""
import json
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parent.parent
source=ROOT/'llama.cpp/build-vulkan/bin'
target=ROOT/'experimental/shift-active/bin'
target.mkdir(parents=True,exist_ok=True)
for path in [source/'llama-server']+list(source.glob('*.dylib')):
    destination=target/path.name
    if destination.exists() or destination.is_symlink(): destination.unlink()
    shutil.copy2(path,destination,follow_symlinks=False)
patch=subprocess.check_output(['git','diff','--','src/llama-kv-cache.cpp'],cwd=ROOT/'llama.cpp')
(target.parent/'shift-active.patch').write_bytes(patch)
(target.parent/'manifest.json').write_text(json.dumps({
    'base_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT/'llama.cpp',text=True).strip(),
    'opt_in':'LLAMA_KV_SHIFT_ACTIVE_ONLY=1','scope':'single-stream K-shift graph; occupied physical cell range',
    'default_behavior_changed':False,'production_runtime_changed':False},indent=2)+'\n')
print('Isolated active-shift build: '+str(target))
