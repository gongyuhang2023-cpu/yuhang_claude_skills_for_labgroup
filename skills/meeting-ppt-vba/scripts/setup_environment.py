#!/usr/bin/env python3
"""Setup environment for meeting-ppt-vba COM automation (optional)"""

import os
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent
VENV_DIR = SKILL_DIR / ".venv"


def main():
    print("=== meeting-ppt-vba Environment Setup ===\n")

    # Check platform
    if sys.platform != "win32":
        print("WARNING: COM automation requires Windows + PowerPoint.")
        print("VBA code generation works on any platform (Claude generates code).")
        print("COM automation (auto-execute) is Windows-only.\n")

    # Create venv
    if not VENV_DIR.exists():
        print("Creating virtual environment...")
        os.system(f'"{sys.executable}" -m venv "{VENV_DIR}"')
    else:
        print("Virtual environment already exists.")

    # Get venv python
    if sys.platform == "win32":
        python = str(VENV_DIR / "Scripts" / "python.exe")
    else:
        python = str(VENV_DIR / "bin" / "python")

    # Upgrade pip
    print("Upgrading pip...")
    os.system(f'"{python}" -m pip install --upgrade pip')

    # Install requirements
    req_file = SKILL_DIR / "requirements.txt"
    if req_file.exists():
        print("Installing dependencies...")
        os.system(f'"{python}" -m pip install -r "{req_file}"')

    # Verify pywin32
    if sys.platform == "win32":
        print("\nVerifying pywin32...")
        result = os.system(f'"{python}" -c "import win32com.client; print(\'pywin32 OK\')"')
        if result != 0:
            print("WARNING: pywin32 installation may have failed.")
            print("Try: pip install pywin32 && python Scripts/pywin32_postinstall.py -install")

    print("\n=== Setup Complete ===")
    print("\nNote: This skill primarily generates VBA code for manual execution.")
    print("COM automation is an optional convenience feature.")


if __name__ == "__main__":
    main()
