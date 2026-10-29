import concurrent.futures
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cache_proxy import Proxy, CacheHTTPServer


class FakeCache:
    key='test-key'
    def __init__(self):
        self.lock=threading.RLock()
        self.finished=0
        self.invalidations=0
    def tokenize(self,path,body): return [1]*512
    def prepare(self,tokens): return None
    def completed(self,tokens,used): self.finished+=1
    def invalidate(self): self.invalidations+=1
    def status(self): return {'ok':True}


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.started=threading.Event()
        self.count_lock=threading.Lock()
        self.active=self.maximum=0
        test=self
        class Backend(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length','0')))
                if self.path.startswith('/slots/'):
                    self.send_response(200);self.end_headers();self.wfile.write(b'{}');return
                with test.count_lock:
                    test.active+=1
                    test.maximum=max(test.maximum,test.active)
                try:
                    self.send_response(200)
                    self.send_header('Content-Type','text/event-stream')
                    self.end_headers()
                    self.wfile.write('data: {"text":"Привет"}\n\n'.encode());self.wfile.flush()
                    test.started.set()
                    time.sleep(.15)
                    self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
                finally:
                    with test.count_lock: test.active-=1
        self.backend=ThreadingHTTPServer(('127.0.0.1',0),Backend)
        self.backend_thread=threading.Thread(target=self.backend.serve_forever,daemon=True)
        self.backend_thread.start()
        self.cache=FakeCache()
        self.proxy=Proxy(self.cache,0,self.backend.server_port)
        self.proxy.start();self.proxy.ready.set()
        self.base='http://127.0.0.1:%d' % self.proxy.http.server_port

    def tearDown(self):
        self.proxy.stop();self.backend.shutdown();self.backend.server_close();self.backend_thread.join()

    def request(self,path='/v1/chat/completions',body=b'{}',auth=True):
        headers={'Authorization':'Bearer test-key'} if auth else {}
        req=urllib.request.Request(self.base+path,data=body,headers=headers)
        return urllib.request.urlopen(req,timeout=5)

    def test_streaming_auth_serialization_and_nonblocking_status(self):
        def inference():
            with self.request() as response:
                first=response.readline()
                self.assertEqual(first,'data: {"text":"Привет"}\n'.encode())
                return first+response.read()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            first=pool.submit(inference)
            self.assertTrue(self.started.wait(3))
            second=pool.submit(inference)
            with self.request('/cache/status',body=None) as response:
                self.assertTrue(json.load(response)['busy'])
            with self.assertRaises(urllib.error.HTTPError) as exc:
                self.request('/cache/status',body=None,auth=False)
            self.assertEqual(exc.exception.code,401)
            expected='data: {"text":"Привет"}\n\ndata: [DONE]\n\n'.encode()
            self.assertEqual(first.result(),expected)
            self.assertEqual(second.result(),expected)
        self.assertEqual(self.maximum,1)
        self.assertEqual(self.cache.finished,2)
        with self.request('/slots/0?action=erase') as response:
            self.assertEqual(json.load(response),{})
        self.assertEqual(self.cache.invalidations,1)

    def test_index_write_failure_does_not_break_inference(self):
        def failed(*args):
            raise sqlite3.OperationalError('Simulated full disk')
        self.cache.prepare=failed
        self.cache.completed=failed
        with self.request() as response:
            self.assertEqual(response.status,200)
            self.assertIn(b'data: [DONE]',response.read())
        deadline=time.monotonic()+2
        while self.cache.invalidations<2 and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertEqual(self.cache.invalidations,2)

    def test_loopback_start_does_not_depend_on_reverse_dns(self):
        with patch('socket.getfqdn', side_effect=AssertionError('DNS unavailable')):
            server=CacheHTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler)
            try:
                self.assertGreater(server.server_port,0)
            finally:
                server.server_close()


if __name__=='__main__': unittest.main()
