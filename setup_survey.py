"""
setup_survey.py — run once to install dependencies and register custom
URL protocols used by GorgonHelper.

  gorgon-survey://  →  Survey/main.pyw   (Survey helper, background)
  gorgon-bridge://  →  ws_bridge.py      (WebSocket file-watch bridge)

After running this, click the relevant buttons inside GorgonHelper.html
to start each tool. No manual terminal required.

Usage:
    py setup_survey.py          # install deps + register protocols
    py setup_survey.py remove   # unregister all protocols
"""

import os
import subprocess
import sys
import winreg

REG_ROOT = winreg.HKEY_CURRENT_USER
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

SURVEY_PROTOCOL = "gorgon-survey"
SURVEY_SCRIPT   = os.path.join(ROOT_DIR, "Survey", "main.pyw")

BRIDGE_PROTOCOL = "gorgon-bridge"
BRIDGE_SCRIPT   = os.path.join(ROOT_DIR, "ws_bridge.py")


# ── Dependency installation ────────────────────────────────────────────────

def _install_req(req_path: str, label: str) -> None:
    if not os.path.exists(req_path):
        print(f"WARNING: {req_path} not found — skipping.")
        return
    print(f"Installing {label} dependencies…")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", req_path],
        check=False,
    )
    if result.returncode != 0:
        sys.exit("ERROR: pip install failed. Fix the errors above and try again.")
    print(f"{label} dependencies installed.\n")


def install_requirements() -> None:
    # Root requirements (websockets for ws_bridge.py)
    _install_req(os.path.join(ROOT_DIR, "requirements.txt"), "Bridge")
    # Survey requirements (PyQt5, opencv, etc.)
    _install_req(os.path.join(ROOT_DIR, "Survey", "requirements.txt"), "Survey")


# ── Protocol registration ──────────────────────────────────────────────────

def _register_protocol(protocol: str, script: str, use_pythonw: bool) -> None:
    """Register a custom URL protocol that launches a Python script."""
    reg_path = rf"Software\Classes\{protocol}"

    # Choose interpreter: pythonw (no console) for GUI tools, python (console) for servers
    if use_pythonw:
        interp = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(interp):
            sys.exit(f"ERROR: pythonw.exe not found — is pythonw installed?")
    else:
        interp = sys.executable  # python.exe — shows a console window

    cmd = f'"{interp}" "{script}" "%1"'

    with winreg.CreateKey(REG_ROOT, reg_path) as key:
        winreg.SetValue(key, "", winreg.REG_SZ, f"URL:Gorgon {protocol} Protocol")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")

    with winreg.CreateKey(REG_ROOT, rf"{reg_path}\shell\open\command") as key:
        winreg.SetValue(key, "", winreg.REG_SZ, cmd)

    print(f"Registered  {protocol}://  →  {cmd}")


def _unregister_protocol(protocol: str) -> None:
    try:
        os.system(f'reg delete "HKCU\\Software\\Classes\\{protocol}" /f >nul 2>&1')
        print(f"Unregistered {protocol}://")
    except Exception as e:
        print(f"Failed to unregister {protocol}://: {e}")


# ── Main actions ───────────────────────────────────────────────────────────

def register() -> None:
    install_requirements()

    if not os.path.exists(SURVEY_SCRIPT):
        print(f"WARNING: {SURVEY_SCRIPT} not found — skipping survey protocol.")
    else:
        _register_protocol(SURVEY_PROTOCOL, SURVEY_SCRIPT, use_pythonw=True)
        print("You can now click '▶ Start Survey Helper' in GorgonHelper.")

    if not os.path.exists(BRIDGE_SCRIPT):
        print(f"WARNING: {BRIDGE_SCRIPT} not found — skipping bridge protocol.")
    else:
        _register_protocol(BRIDGE_PROTOCOL, BRIDGE_SCRIPT, use_pythonw=False)
        print("You can now click '▶ Start Bridge' in GorgonHelper Settings.")

    print("\nSetup complete.")


def remove() -> None:
    _unregister_protocol(SURVEY_PROTOCOL)
    _unregister_protocol(BRIDGE_PROTOCOL)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "remove":
        remove()
    else:
        register()
