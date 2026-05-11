#!/usr/bin/env python3
"""
Universal runner for MeetingMind skill scripts.
Ensures all scripts run with the correct virtual environment.
"""

import os
import sys
import subprocess
from pathlib import Path


def get_venv_python():
    skill_dir = Path(__file__).parent.parent
    venv_dir = skill_dir / ".venv"
    if os.name == 'nt':
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def ensure_venv():
    skill_dir = Path(__file__).parent.parent
    venv_dir = skill_dir / ".venv"
    setup_script = skill_dir / "scripts" / "setup_environment.py"

    if not venv_dir.exists():
        print("[Setup] First-time setup: Creating virtual environment...")
        result = subprocess.run([sys.executable, str(setup_script)])
        if result.returncode != 0:
            print("[Error] Failed to set up environment")
            sys.exit(1)
        print("[OK] Environment ready!")

    return get_venv_python()


def main():
    if len(sys.argv) < 2:
        print("Usage: python run.py <script_name> [args...]")
        print("\nAvailable scripts:")
        print("  session.py     - Start recording + capture session")
        print("  transcribe.py  - Transcribe audio to text")
        sys.exit(1)

    script_name = sys.argv[1]
    script_args = sys.argv[2:]

    if script_name.startswith('scripts/') or script_name.startswith('scripts\\'):
        script_name = script_name.split('/', 1)[-1].split('\\', 1)[-1]

    if not script_name.endswith('.py'):
        script_name += '.py'

    skill_dir = Path(__file__).parent.parent
    script_path = skill_dir / "scripts" / script_name

    if not script_path.exists():
        print(f"[Error] Script not found: {script_name}")
        print(f"   Looked for: {script_path}")
        sys.exit(1)

    venv_python = ensure_venv()

    cmd = [str(venv_python), str(script_path)] + script_args

    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    env['PYTHONUNBUFFERED'] = '1'

    import signal

    proc = subprocess.Popen(cmd, env=env)

    def forward_signal(sig, frame):
        if proc.poll() is None:
            proc.send_signal(sig)

    signal.signal(signal.SIGINT, forward_signal)
    signal.signal(signal.SIGTERM, forward_signal)

    try:
        sys.exit(proc.wait())
    except KeyboardInterrupt:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=10)
        sys.exit(130)


if __name__ == "__main__":
    main()
