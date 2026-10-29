"""Real HTTP/process/cache lifecycle; no GPU or downloaded model required."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from platform_support import FileLock

FAKE_NATIVE = '''import json,sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
directory=Path(sys.argv[2])
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def reply(self,data):
  raw=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
 def do_GET(self):
  if self.path=='/health':self.reply({'status':'ok'})
  elif self.path=='/slots':self.reply([{'id':0,'is_processing':False,'n_prompt_tokens':600}])
  else:self.reply({})
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if self.path=='/tokenize':self.reply({'tokens':body['content']})
  elif 'action=save' in self.path:
   (directory/body['filename']).write_bytes(b'fake KV'*600)
   self.reply({'n_saved':600,'n_written':4200,'timings':{'save_ms':1}})
  elif 'action=restore' in self.path:self.reply({'n_restored':600})
  else:self.reply({'content':'4','tokens_predicted':1})
ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()
'''


class SupervisorTests(unittest.TestCase):
    def test_idle_save_restart_restore_and_stop_request_release_ports_and_owner(self):
        server = str(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            native = directory / 'fake_native.py'
            native.write_text(FAKE_NATIVE, encoding='utf-8')
            sockets = [socket.socket(), socket.socket()]
            for s in sockets: s.bind(('127.0.0.1', 0))
            public, backend = [s.getsockname()[1] for s in sockets]
            for s in sockets: s.close()
            code = ('import sys,os;from pathlib import Path;sys.path.insert(0,%r);'
                    'from tiered_cache import TieredCache;from cache_proxy import supervise_proxy;'
                    'd=Path(%r);c=TieredCache(d,%d,"test-key","fp",{"kv_hot_mib":0});'
                    'sys.exit(supervise_proxy([sys.executable,%r,str(%d),str(d)],dict(os.environ),c,d/"ready.json",%d,%d,.1))') % (
                        server, str(directory), backend, str(native), backend, public, backend)

            def request(path, body=None):
                req = urllib.request.Request('http://127.0.0.1:%d' % public + path,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={'Authorization': 'Bearer test-key'})
                with urllib.request.urlopen(req, timeout=5) as response: return json.load(response)

            for restart in (False, True):
                (directory / 'stop.request').unlink(missing_ok=True)
                log = (directory / ('restart.log' if restart else 'initial.log')).open('wb')
                process = subprocess.Popen([sys.executable, '-u', '-c', code], stdout=log, stderr=subprocess.STDOUT,
                                           env={**os.environ, 'PYTHONUTF8': '1'})
                try:
                    deadline = time.monotonic() + 15
                    while not (directory / 'ready.json').exists():
                        if process.poll() is not None: self.fail('Supervisor exited; see ' + str(log.name))
                        if time.monotonic() > deadline: self.fail('Supervisor startup timeout')
                        time.sleep(.05)
                    self.assertEqual(request('/health')['status'], 'ok')
                    if restart:
                        self.assertEqual(request('/cache/status')['entries'], 1)
                    else:
                        self.assertEqual(request('/completion', {'prompt': [42]*600})['content'], '4')
                        while not (directory / 'latest.json').exists():
                            if time.monotonic() > deadline: self.fail('No idle snapshot')
                            time.sleep(.05)
                    (directory / 'stop.request').write_text('stop')
                    self.assertEqual(process.wait(timeout=20), 0)
                    self.assertFalse((directory / 'ready.json').exists())
                    with FileLock(directory / 'owner.lock'): pass
                    for port in (public, backend):
                        with socket.socket() as sock: self.assertNotEqual(sock.connect_ex(('127.0.0.1', port)), 0)
                finally:
                    if process.poll() is None: process.kill(); process.wait()
                    log.close()
            self.assertIn('restored indexed snapshot', (directory / 'restart.log').read_text(encoding='utf-8'))


if __name__ == '__main__': unittest.main()
