#!/usr/bin/env python3
"""
Environment Setup for Meeting PPT Capture Skill
Manages virtual environment and dependencies automatically
"""

import os
import sys
import subprocess
import venv
from pathlib import Path


class SkillEnvironment:
    """Manages skill-specific virtual environment"""

    def __init__(self):
        self.skill_dir = Path(__file__).parent.parent
        self.venv_dir = self.skill_dir / ".venv"
        self.requirements_file = self.skill_dir / "requirements.txt"

        if os.name == 'nt':  # Windows
            self.venv_python = self.venv_dir / "Scripts" / "python.exe"
            self.venv_pip = self.venv_dir / "Scripts" / "pip.exe"
        else:
            self.venv_python = self.venv_dir / "bin" / "python"
            self.venv_pip = self.venv_dir / "bin" / "pip"

    def ensure_venv(self) -> bool:
        """Ensure virtual environment exists and is set up"""

        if self.is_in_skill_venv():
            print("[OK] Already running in skill virtual environment")
            return True

        if not self.venv_dir.exists():
            print(f"[Setup] Creating virtual environment in {self.venv_dir.name}/")
            try:
                venv.create(self.venv_dir, with_pip=True)
                print("[OK] Virtual environment created")
            except Exception as e:
                print(f"[Error] Failed to create venv: {e}")
                return False

        if self.requirements_file.exists():
            print("[Setup] Installing dependencies...")
            try:
                # Use python -m pip (required on newer pip versions)
                subprocess.run(
                    [str(self.venv_python), "-m", "pip", "install", "--upgrade", "pip"],
                    check=True, capture_output=True, text=True
                )

                subprocess.run(
                    [str(self.venv_python), "-m", "pip", "install", "-r", str(self.requirements_file)],
                    check=True, capture_output=True, text=True
                )
                print("[OK] Dependencies installed")
                return True
            except subprocess.CalledProcessError as e:
                print(f"[Error] Failed to install dependencies: {e}")
                if e.stderr:
                    print(f"   stderr: {e.stderr[:500]}")
                return False
        else:
            print("[Warning] No requirements.txt found")
            return True

    def is_in_skill_venv(self) -> bool:
        """Check if we're already running in the skill's venv"""
        if hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix):
            return Path(sys.prefix) == self.venv_dir
        return False


def main():
    env = SkillEnvironment()
    if env.ensure_venv():
        print("\n[OK] Environment ready!")
        print(f"   Virtual env: {env.venv_dir}")
    else:
        print("\n[Error] Environment setup failed")
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
