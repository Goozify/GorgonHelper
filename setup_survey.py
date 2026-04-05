"""
setup_survey.py — run once to install dependencies and register the
gorgon-survey:// URL protocol.

After running this, any browser on this machine can launch Survey/main.pyw
by clicking "Start Survey Helper" in the Gorgon Helper — no HTTP server required.

Usage:
    py setup_survey.py          # install deps + register
    py setup_survey.py remove   # unregister
"""

import os
import subprocess
import sys
import winreg

PROTOCOL   = "gorgon-survey"
SCRIPT     = os.path.abspath(os.path.join(os.path.dirname(__file__), "Survey", "main.pyw"))
PYTHONW    = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
REG_ROOT   = winreg.HKEY_CURRENT_USER
REG_PATH   = rf"Software\Classes\{PROTOCOL}"


def install_requirements() -> None:
    req = os.path.join(os.path.dirname(__file__), "Survey", "requirements.txt")
    if not os.path.exists(req):
        print(f"WARNING: {req} not found — skipping pip install.")
        return
    print("Installing Survey dependencies…")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", req],
        check=False,
    )
    if result.returncode != 0:
        sys.exit("ERROR: pip install failed. Fix the errors above and try again.")
    print("Dependencies installed.\n")


def register() -> None:
    install_requirements()

    if not os.path.exists(SCRIPT):
        sys.exit(f"ERROR: {SCRIPT} not found.")
    if not os.path.exists(PYTHONW):
        sys.exit(f"ERROR: {PYTHONW} not found — is pythonw installed?")

    cmd = f'"{PYTHONW}" "{SCRIPT}" "%1"'

    with winreg.CreateKey(REG_ROOT, REG_PATH) as key:
        winreg.SetValue(key, "", winreg.REG_SZ, "URL:Gorgon Survey Protocol")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")

    with winreg.CreateKey(REG_ROOT, rf"{REG_PATH}\shell\open\command") as key:
        winreg.SetValue(key, "", winreg.REG_SZ, cmd)

    print(f"Registered  {PROTOCOL}://  →  {cmd}")
    print("You can now click '▶ Start Survey Helper' in the browser.")


def remove() -> None:
    try:
        os.system(f'reg delete "HKCU\\Software\\Classes\\{PROTOCOL}" /f')
        print(f"Unregistered {PROTOCOL}://")
    except Exception as e:
        print(f"Failed: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "remove":
        remove()
    else:
        register()
