"""Reproducible native source checkout/build and verified GGUF download."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
COMMIT = '2280f0fd163b03d4331d49e08460f66502e35854'


def source():
    target = ROOT / 'llama.cpp'
    if not target.exists():
        subprocess.run(['git', '-c', 'core.autocrlf=false', 'clone', '--no-checkout',
                        'https://github.com/ggml-org/llama.cpp.git', str(target)], check=True)
        subprocess.run(['git', 'config', 'core.autocrlf', 'false'], cwd=target, check=True)
        subprocess.run(['git', 'checkout', '--detach', COMMIT], cwd=target, check=True)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=target, text=True).strip()
    if head != COMMIT:
        raise RuntimeError('llama.cpp checkout differs from the pinned revision; refusing to reset existing work')
    patch = ROOT / 'patches/llama.cpp.patch'
    if patch.exists():
        done = subprocess.run(['git', 'apply', '--reverse', '--check', str(patch)], cwd=target,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not done:
            subprocess.run(['git', 'apply', '--check', str(patch)], cwd=target, check=True)
            subprocess.run(['git', 'apply', str(patch)], cwd=target, check=True)
    return target


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 2**20), b''): digest.update(chunk)
    return digest.hexdigest()


def download():
    info = json.loads((ROOT / 'models/qwen3-source.json').read_text(encoding='utf-8'))
    path = ROOT / 'models' / info['file']
    if path.exists():
        if path.stat().st_size == info['bytes'] and sha256(path) == info['sha256']:
            print('Model already verified: ' + str(path)); return
        raise RuntimeError('Existing GGUF failed verification; move it aside before downloading again')
    partial = path.with_suffix('.gguf.part')
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > info['bytes']: raise RuntimeError('Partial download exceeds expected size; remove the .part file')
    if offset < info['bytes']:
        url = 'https://huggingface.co/%s/resolve/%s/%s' % (info['repository'], info['revision'], info['file'])
        req = urllib.request.Request(url, headers={'Range': 'bytes=%d-' % offset} if offset else {})
        with urllib.request.urlopen(req, timeout=120) as response:
            resumed = offset > 0 and response.status == 206
            if resumed and not response.headers.get('Content-Range', '').startswith('bytes %d-' % offset):
                raise RuntimeError('Download resume returned an unexpected byte range')
            mode = 'ab' if resumed else 'wb'
            with partial.open(mode) as stream:
                for chunk in iter(lambda: response.read(4 * 2**20), b''): stream.write(chunk)
    if partial.stat().st_size != info['bytes'] or sha256(partial) != info['sha256']:
        raise RuntimeError('Downloaded model failed size/SHA-256 verification; retained .part for inspection')
    os.replace(partial, path)
    print('Downloaded and SHA-256 verified: ' + str(path))


def build(backend='vulkan', jobs=6, env=None):
    target = source()
    directory = target / ('build-' + backend)
    args = ['cmake', '-S', str(target), '-B', str(directory),
            '-DGGML_VULKAN=' + ('ON' if backend == 'vulkan' else 'OFF'),
            '-DGGML_METAL=' + ('ON' if backend == 'metal' else 'OFF'),
            '-DLLAMA_BUILD_SERVER=ON', '-DLLAMA_BUILD_UI=OFF',
            '-DLLAMA_BUILD_APP=OFF', '-DCMAKE_BUILD_TYPE=Release']
    if backend == 'metal': args.append('-DGGML_METAL_EMBED_LIBRARY=ON')
    subprocess.run(args, check=True, env=env)
    subprocess.run(['cmake', '--build', str(directory), '--config', 'Release', '--parallel', str(jobs),
                    '--target', 'llama-server'], check=True, env=env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['source', 'build', 'download-model'])
    parser.add_argument('--backend', choices=['vulkan', 'metal'], default='vulkan')
    parser.add_argument('--jobs', type=int, default=6)
    opts = parser.parse_args()
    if opts.action == 'download-model': download(); return
    if opts.action == 'build':
        build(opts.backend, opts.jobs)
    else: source()


if __name__ == '__main__': main()
