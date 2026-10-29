import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SERVER=Path(__file__).resolve().parents[1]


class LogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)

    def tearDown(self): self.tmp.cleanup()

    def launch(self,code):
        bootstrap=('import sys; from pathlib import Path; sys.path.insert(0,'+repr(str(SERVER))+'); '
                   'import logged_run; logged_run.ROOT=Path('+repr(str(self.root))+'); '
                   'sys.argv=["logged_run.py","logging-test","--",sys.executable,"-c",'+repr(code)+']; '
                   'logged_run.main()')
        return subprocess.Popen([sys.executable,'-c',bootstrap],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)

    def result(self):
        return json.loads((self.root/'logs/latest-logging-test.json').read_text())

    def test_success_captures_both_streams_utf8_and_completion(self):
        process=self.launch('import sys; print("Привет stdout"); print("stderr marker",file=sys.stderr)')
        output=process.communicate(timeout=10)[0]
        self.assertEqual(process.returncode,0,output)
        data=self.result()
        self.assertEqual(data['status'],'completed')
        self.assertEqual(data['exit_code'],0)
        text=Path(data['log_file']).read_text()
        self.assertIn('Привет stdout',text)
        self.assertIn('stderr marker',text)
        self.assertIn('finished_at',data)

    def test_failure_retains_log_and_original_exit_code(self):
        process=self.launch('import sys; print("before failure"); sys.exit(7)')
        process.communicate(timeout=10)
        self.assertEqual(process.returncode,7)
        data=self.result()
        self.assertEqual(data['status'],'failed')
        self.assertEqual(data['exit_code'],7)
        self.assertIn('before failure',Path(data['log_file']).read_text())

    @unittest.skipIf(os.name == 'nt', 'POSIX SIGINT forwarding; Windows uses CTRL_BREAK_EVENT')
    def test_interrupt_allows_child_cleanup_and_captures_it(self):
        code='import time\ntry:\n print("child ready",flush=True)\n time.sleep(20)\nfinally:\n print("cleanup completed",flush=True)'
        process=self.launch(code)
        deadline=time.monotonic()+10
        try:
            while time.monotonic()<deadline:
                try:
                    data=self.result()
                    if 'child ready' in Path(data['log_file']).read_text(): break
                except (OSError,ValueError): pass
                time.sleep(.05)
            else: self.fail('Child did not become ready')
            process.send_signal(signal.SIGINT)
            process.communicate(timeout=10)
            data=self.result()
            self.assertEqual(data['exit_code'],130)
            self.assertIn('cleanup completed',Path(data['log_file']).read_text())
        finally:
            if process.poll() is None: process.kill();process.communicate()


if __name__=='__main__':unittest.main()
