"""Run the OS-specific source installer with a durable console log."""
import sys
from pathlib import Path

if sys.version_info < (3, 11):
    raise SystemExit('Python 3.11+ required; use setup.command or setup.ps1 to bootstrap Python')
root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / 'server'))
import logged_run
sys.argv = [sys.argv[0], 'setup', '--', sys.executable, str(root / 'server/setup_local.py')] + sys.argv[1:]
logged_run.main()
