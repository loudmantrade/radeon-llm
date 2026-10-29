#!/usr/bin/python3
"""Tee a diagnostic command into a durable session log and completion manifest."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
from platform_support import WINDOWS, shutdown_signals, child_options, stop_child
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def atomic_json(path, data):
    temporary = path.with_name(path.name+'.%d.tmp' % os.getpid())
    temporary.write_text(json.dumps(data, indent=2)+'\n')
    temporary.replace(path)


def main():
    if len(sys.argv)<4 or sys.argv[2]!='--' or not re.fullmatch(r'[a-z0-9-]+',sys.argv[1]):
        raise SystemExit('Usage: logged_run.py label -- command [args...]')
    label=sys.argv[1]
    command=sys.argv[3:]
    directory=ROOT/'logs/sessions'
    directory.mkdir(parents=True,exist_ok=True)
    started=datetime.now(timezone.utc)
    name='%s-%s-%d' % (label,started.strftime('%Y%m%dT%H%M%S.%fZ'),os.getpid())
    logfile=directory/(name+'.log')
    manifest=directory/(name+'.json')
    data={'label':label,'status':'starting','started_at':started.isoformat(),
          'log_file':str(logfile),'manifest_file':str(manifest),'runner_pid':os.getpid(),
          'command':command,'exit_code':None}
    def save():
        for path in (manifest,ROOT/('logs/latest-%s.json' % label),ROOT/'logs/latest-test.json'):
            atomic_json(path,data)
    child=None
    def forward(signum,frame):
        if child is not None and child.poll() is None:
            child.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signum)
    for signum in shutdown_signals():
        signal.signal(signum,forward)
    code=1
    save()
    print('Session log: '+str(logfile),flush=True)
    try:
        with logfile.open('wb',buffering=0) as output:
            child=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                   **(child_options() if WINDOWS else {'start_new_session': True}),
                                   env={**os.environ, 'PYTHONUTF8': '1'})
            data.update(status='running',child_pid=child.pid)
            save()
            while True:
                chunk=child.stdout.read1(65536)
                if not chunk: break
                output.write(chunk)
                try:
                    sys.stdout.buffer.write(chunk);sys.stdout.buffer.flush()
                except (BrokenPipeError,OSError):
                    pass
            child.stdout.close()
            raw_code=child.wait()
            code=raw_code if raw_code>=0 else 128-raw_code
            data['return_code']=raw_code
    except BaseException as exc:
        data['runner_error']=type(exc).__name__+': '+str(exc)
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGTERM)
            # Test scripts restore the daemon in finally. Give them time to finish.
            try: child.wait(timeout=240)
            except subprocess.TimeoutExpired:
                child.kill();child.wait()
        raise
    finally:
        data.update(status='completed' if code==0 else 'failed',exit_code=code,
                    finished_at=datetime.now(timezone.utc).isoformat())
        save()
        print('Session %s (exit %d). Full output saved; report only that the test finished.' %
              (data['status'],code),flush=True)
    raise SystemExit(code)


if __name__=='__main__':main()
