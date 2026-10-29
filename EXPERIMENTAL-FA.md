# Experimental GPU Flash Attention on Radeon

This is an opt-in local test for AMD Radeon Pro 560X with MoltenVK 1.4.2. The installed 8k LaunchAgent and its native libraries are unchanged.

## Patch

The existing Apple/AMD subgroup arithmetic workaround remains active. Only the subgroup shuffle workaround can be bypassed with:

```bash
GGML_VK_AMD_MOLTENVK_SHUFFLE=1
```

The patch does not force an unsupported capability: it keeps the shuffle capability advertised by the driver. Without the exact value `1`, existing behavior is retained. Patch and build provenance are saved in `experimental/amd-moltenvk-shuffle.patch` and `experimental/manifest.json`.

The experimental native bundle is in `experimental/bin/`. Its directory is explicitly included in DYLD_LIBRARY_PATH. A loader trace confirmed that the experimental ggml-vulkan library is selected. No global environment variable or daemon configuration was changed.

## Run

Leave OpenCode without an active request, then run in the macOS Terminal:

```bash
/path/to/radeon-llm/test-gpu-fa.command
```

The script temporarily stops the stable server and runs these gates:

1. Small F32/F16/Q4_K/Q6_K matrix operations compared with the CPU reference.
2. Nine Q8 Flash Attention tests with the Qwen3 GQA shape, covering prefill and decode, KV lengths up to 16384, and 128-token prefill against KV 8192.
3. Model startup with context 16384, Q8 K/V and Flash Attention on; explicit confirmation of the experimental flag and 37/37 weight layers offloaded.
4. Graph partition check: if more than eight partitions remain, skip the long benchmark. The previous CPU fallback created 74 partitions. A small partition count is supporting evidence, not an exhaustive proof that every operation runs on the GPU.
5. A short checkpoint-retrieval and prefix-cache benchmark, followed by the 10k-token benchmark.
6. Streaming function-call API test and tool-result round trip.

On failure, normal completion, Ctrl+C, SIGTERM or SIGHUP the script attempts to stop its test process and restart the stable 8k daemon. As with the previous experiment, power loss or SIGKILL requires manual recovery with `./daemon.command start`.

A test failure leaves the current production configuration unchanged. Do not enable the experimental flag globally. The test script sets it only in its own child-process environment.

## Logs

- `logs/experimental-matrix-tests.log`
- `logs/experimental-attention-tests.log`
- `logs/context16k-gpu-fa-server.log`
- `logs/context16k-gpu-fa-short.json`
- `logs/context16k-gpu-fa-benchmark.json`

The original 19-minute baseline is preserved in `logs/context16k-benchmark.json`.

## Completed checks

The patched server and test binary compiled. All nine selected attention cases passed on CPU. Python syntax, shell syntax, dry-run binary selection and git diff whitespace checks passed. Self-review covered the backend capability gate, default behavior, scope and security; no new operator, network surface or dependency was added. GPU correctness and speed of the bypass remain unverified until the Terminal test runs.

To rebuild the bundle:

```bash
./build-experimental.command
```

This reuses the existing CMake build cache, then packages an independent bundle. The installed production runtime continues to use its existing copied libraries.

## Second trial: scalar FA without subgroup operations

The first GPU trial passed 50/50 matrix cases and 4/9 attention cases. All four single-token decode cases passed; all five batched-prefill cases failed with large errors. The failed attention report is preserved in `logs/experimental-attention-shuffle-only.log`. The 8k daemon was restored.

A second opt-in flag selects the backend's existing no-subgroup scalar attention variant:

```bash
GGML_VK_AMD_MOLTENVK_FA_NO_SUBGROUPS=1
```

The test launcher now sets both experimental flags in its own environment. Subgroup arithmetic remains disabled. Production settings are unchanged. This mode reuses the existing shared-memory synchronization/reduction path rather than adding a new shader.

Start with operation checks only:

```bash
./test-gpu-fa.command --ops-only
```

This stops the stable service, checks matrices and all nine attention cases, then restores the service without loading the experimental model. If all attention cases pass, the full experiment can be run separately without `--ops-only`. A dedicated runtime log marker confirms that the new shared-memory mode is selected during a full model test.

This second GPU mode is not yet verified. Compilation and host-side validation do not establish numerical correctness on Radeon.

## Current trial: FA-only override and realistic masks

The full shared-memory/global-shuffle trial passed the selected operation tests but failed the real model's 21-token arithmetic prompt (`.8888888`). Its server log is preserved as `logs/context16k-gpu-fa-shared-global-failed.log`.

The current patch no longer changes device-wide shuffle. Both original AMD subgroup arithmetic and subgroup shuffle workarounds remain in place for ordinary operations, including the FWHT rotations used by Q8 KV. A separate, strictly scoped opt-in permits only FLASH_ATTN_EXT to use its existing no-subgroup shared-memory implementation:

```bash
GGML_VK_AMD_MOLTENVK_FA_SHARED=1
```

The older two experimental flags are no longer used. The runner also sets `MVK_CONFIG_FAST_MATH_ENABLED=0` to test causal masks containing negative infinity without relaxed Metal floating-point assumptions.

Coverage was expanded from nine random-mask attention cases to 34 cases, including short and partial batches, padded 256-token KV, and causal masks for prefill and decode. Nine selected Hadamard/FWHT cases now test KV rotation preparation separately. CPU checks passed 34/34 attention and 9/9 rotation cases; compilation, packaging, syntax and diff checks passed. GPU/model correctness of this current trial remains pending.

Run the full gated experiment using the same command:

```bash
./test-gpu-fa.command
```

It checks matrices, rotations and attention before loading the model. The real-model arithmetic primer and short retrieval test must pass before the 10k benchmark. The installed 8k runtime remains the fallback and is restored after the experiment.

## FA-only GPU result and timeout

The Radeon passed 50/50 matrix, 9/9 rotation and 34/34 attention checks. The model passed arithmetic and all three short retrieval/cache requests (cold 39.21s, repeat 5.13s, changed suffix 7.96s). The graph had two splits. The 10k-token cold request hit the previous 1200-second client timeout at approximately 89 percent prefill; long-context correctness and experimental API checks remain unconfirmed. There is no demonstrated cold-prefill speedup. The stable 8k daemon was restored.

The default benchmark socket timeout is now 2400 seconds, configurable with `--request-timeout`. This only allows slow requests to finish; it does not accelerate inference. Run `./test-gpu-fa.command --short-only` for operation, short retrieval/cache and API checks without the long document, or run the full command with `--request-timeout 2400` to complete the long-context check. Short-only success does not validate long-context retrieval.

## Completed long-context run

With the 2400-second timeout, all selected operations, short/long retrieval, prefix cache and API/tool-call tests passed. Long document: cold 1505.53s, exact repeat 9.02s, changed suffix 10.98s; two graph splits. The cold pass is approximately 32% slower than the previous fallback run, while the repeat is approximately 2.43x faster (single runs). This validates the tested cases, not a general numerical proof. No production promotion: the stable 8k daemon was restored.
