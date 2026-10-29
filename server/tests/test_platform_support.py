import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from platform_support import FileLock


class LockTests(unittest.TestCase):
    def test_exclusion_crosses_process_boundary_and_close_releases_it(self):
        server = str(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'owner.lock'
            code = 'import sys;sys.path.insert(0,%r);from platform_support import FileLock;FileLock(%r).close()' % (server, str(path))
            with FileLock(path):
                child = subprocess.run([sys.executable, '-c', code], capture_output=True)
                self.assertNotEqual(child.returncode, 0)
                self.assertIn(b'BlockingIOError', child.stderr)
            child = subprocess.run([sys.executable, '-c', code], capture_output=True)
            self.assertEqual(child.returncode, 0, child.stderr)

    def test_crashed_process_releases_owner(self):
        server = str(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'owner.lock'
            code = 'import sys,os;sys.path.insert(0,%r);from platform_support import FileLock;l=FileLock(%r);os._exit(7)' % (server, str(path))
            child = subprocess.run([sys.executable, '-c', code])
            self.assertEqual(child.returncode, 7)
            with FileLock(path): pass


if __name__ == '__main__': unittest.main()
