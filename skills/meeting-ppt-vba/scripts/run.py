#!/usr/bin/env python3
"""
meeting-ppt-vba: Optional COM automation runner
Executes VBA code in PowerPoint via win32com (Windows only)

Usage:
    python run.py --vba-file presentation.bas --output output.pptx
    python run.py setup  # Install dependencies
"""

import argparse
import os
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent
VENV_DIR = SKILL_DIR / ".venv"
REQUIREMENTS = SKILL_DIR / "requirements.txt"


def get_python():
    """Get venv Python path"""
    if sys.platform == "win32":
        return str(VENV_DIR / "Scripts" / "python.exe")
    return str(VENV_DIR / "bin" / "python")


def setup_environment():
    """Create venv and install dependencies"""
    if not VENV_DIR.exists():
        print("Creating virtual environment...")
        os.system(f'"{sys.executable}" -m venv "{VENV_DIR}"')

    python = get_python()
    print("Upgrading pip...")
    os.system(f'"{python}" -m pip install --upgrade pip')

    if REQUIREMENTS.exists():
        print("Installing dependencies...")
        os.system(f'"{python}" -m pip install -r "{REQUIREMENTS}"')

    print("Setup complete!")


def run_vba(vba_file: str, output: str):
    """Execute VBA file in PowerPoint via COM automation"""
    try:
        import win32com.client
    except ImportError:
        print("ERROR: pywin32 not installed. Run: python run.py setup")
        sys.exit(1)

    vba_path = Path(vba_file).resolve()
    output_path = Path(output).resolve()

    if not vba_path.exists():
        print(f"ERROR: VBA file not found: {vba_path}")
        sys.exit(1)

    # Read VBA code
    with open(vba_path, "r", encoding="utf-8-sig") as f:
        vba_code = f.read()

    print("Starting PowerPoint...")
    app = win32com.client.Dispatch("PowerPoint.Application")
    app.Visible = True

    try:
        # Create new presentation
        prs = app.Presentations.Add()

        # Add VBA module
        vba_module = prs.VBProject.VBComponents.Add(1)  # vbext_ct_StdModule
        vba_module.CodeModule.AddFromString(vba_code)

        # Run the main sub
        print("Executing VBA macro...")
        app.Run(f"{vba_module.Name}.GeneratePresentation")

        # Save as .pptx (remove VBA)
        # ppSaveAsOpenXMLPresentation = 24
        output_path.parent.mkdir(parents=True, exist_ok=True)
        prs.SaveAs(str(output_path), 24)
        print(f"Saved: {output_path}")

        # Remove VBA module
        prs.VBProject.VBComponents.Remove(vba_module)
        prs.Save()

    except Exception as e:
        print(f"ERROR: {e}")
        print("\nTroubleshooting:")
        print("1. Ensure PowerPoint is installed")
        print("2. Enable 'Trust access to the VBA project object model':")
        print("   File > Options > Trust Center > Trust Center Settings")
        print("   > Macro Settings > Check the box")
        raise
    finally:
        # Don't close - let user review
        print("Done! PowerPoint is open for review.")


def main():
    parser = argparse.ArgumentParser(description="Meeting PPT VBA Runner")
    subparsers = parser.add_subparsers(dest="command")

    # Setup command
    subparsers.add_parser("setup", help="Install dependencies")

    # Run command
    run_parser = subparsers.add_parser("run", help="Execute VBA in PowerPoint")
    run_parser.add_argument("--vba-file", required=True, help="Path to .bas file")
    run_parser.add_argument("--output", required=True, help="Output .pptx path")

    args = parser.parse_args()

    if args.command == "setup":
        setup_environment()
    elif args.command == "run":
        run_vba(args.vba_file, args.output)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
