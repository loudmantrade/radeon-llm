"""Small OS adapters shared by the cache and both service launchers."""
import os
import signal
import subprocess

WINDOWS = os.name == 'nt'
if WINDOWS:
    import msvcrt
else:
    import fcntl


class FileLock:
    """Exclusive, nonblocking owner lock, released even after a process crash."""
    def __init__(self, path):
        self.stream = open(path, 'a+b')
        try:
            if WINDOWS:
                if os.fstat(self.stream.fileno()).st_size == 0:
                    self.stream.write(b'\0')
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise BlockingIOError('Cache or service directory is already owned') from exc

    def close(self):
        if self.stream.closed:
            return
        try:
            if WINDOWS:
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()

    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def shutdown_signals():
    names = ('SIGINT', 'SIGTERM', 'SIGBREAK') if WINDOWS else ('SIGINT', 'SIGTERM', 'SIGHUP')
    return tuple(getattr(signal, name) for name in names if hasattr(signal, name))


def child_options():
    return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {}


def stop_child(child, timeout=15):
    if child is None or child.poll() is not None:
        return
    try:
        if WINDOWS:
            child.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            child.terminate()
    except OSError:
        child.terminate()
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
