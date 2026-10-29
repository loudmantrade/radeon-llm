# Third-party components

llama.cpp is fetched from https://github.com/ggml-org/llama.cpp at the revision
recorded in `server/bootstrap.py`. Its MIT license is retained in
`third_party/llama.cpp-LICENSE`; the patch contains context from that source.

Qwen3-4B-Instruct-2507 originates from https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507.
The GGUF is downloaded from the Unsloth repository recorded in
`models/qwen3-source.json`. Its Apache-2.0 license is in `models/Qwen3-LICENSE`.
`models/LICENSE` belongs to the earlier Qwen2.5 model used in the macOS lab.

Vulkan SDK, GPU drivers, MoltenVK and OpenCode are external dependencies obtained
from their respective publishers. Their binaries are not distributed here.
