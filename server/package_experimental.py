#!/usr/bin/python3
from pathlib import Path
import json
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "llama.cpp/build-vulkan/bin"
TARGET = ROOT / "experimental/bin"
TARGET.mkdir(parents=True, exist_ok=True)
files = [SOURCE / name for name in ["llama-server", "test-backend-ops"]]
files += list(SOURCE.glob("*.dylib"))
for source in files:
    target = TARGET / source.name
    if target.exists() or target.is_symlink():
        target.unlink()
    shutil.copy2(source, target, follow_symlinks=False)
patch = subprocess.check_output(["git", "diff", "--", "ggml/src/ggml-vulkan/ggml-vulkan.cpp",
                                 "tests/test-backend-ops.cpp"], cwd=ROOT / "llama.cpp")
(ROOT / "experimental/amd-moltenvk-shuffle.patch").write_bytes(patch)
manifest = {"base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"],
                                                  cwd=ROOT / "llama.cpp", text=True).strip(),
            "opt_in": "GGML_VK_AMD_MOLTENVK_FA_SHARED=1",
            "attention_mode": "shared memory, FA-only override",
            "default_behavior_changed": False, "subgroup_arithmetic_workaround_retained": True,
            "subgroup_shuffle_workaround_retained": True, "override_scope": "FLASH_ATTN_EXT only"}
(ROOT / "experimental/manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print("Experimental binaries packaged: " + str(TARGET))
