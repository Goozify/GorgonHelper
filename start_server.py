"""Static file server + Survey Helper launcher.

Replaces `py -m http.server 3000`.  Serves all files in this directory and
also handles a single extra endpoint:

  POST /api/start-survey
      Checks if Survey/main.pyw is already running (ws://localhost:8765).
      If not, spawns it as a detached background process (no console window).
      Returns JSON: {"status": "launched" | "already_running" | "not_found"}

Usage:
  py start_server.py
"""
import http.server
import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

PORT = 3000
SURVEY_SCRIPT = Path(__file__).parent / "Survey" / "main.pyw"
SURVEY_WS_PORT = 8765
VERSION_FILE = Path(__file__).parent / "version.json"
VERSION_URL = "https://raw.githubusercontent.com/Streak5274/GorgonHelper/master/version.json"


def _survey_already_running() -> bool:
    """Return True if something is already listening on the Survey WS port."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        result = s.connect_ex(("127.0.0.1", SURVEY_WS_PORT))
        s.close()
        return result == 0
    except OSError:
        return False


def _launch_survey() -> str:
    """Spawn Survey/main.pyw as a detached background process.

    Returns 'launched', 'already_running', or 'not_found'.
    """
    if _survey_already_running():
        return "already_running"

    if not SURVEY_SCRIPT.exists():
        return "not_found"

    cwd = str(SURVEY_SCRIPT.parent)

    if sys.platform == "win32":
        # pythonw.exe = no console window
        exe = Path(sys.executable).parent / "pythonw.exe"
        if not exe.exists():
            exe = "pythonw"
        subprocess.Popen(
            [str(exe), str(SURVEY_SCRIPT)],
            cwd=cwd,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    else:
        subprocess.Popen(
            [sys.executable, str(SURVEY_SCRIPT)],
            cwd=cwd,
            start_new_session=True,
            close_fds=True,
        )

    return "launched"


def _local_version() -> str:
    try:
        return json.loads(VERSION_FILE.read_text())["version"]
    except Exception:
        return "unknown"


def _check_update() -> dict:
    local = _local_version()
    try:
        req = urllib.request.Request(VERSION_URL, headers={"User-Agent": "GorgonHelper"})
        with urllib.request.urlopen(req, timeout=5) as r:
            remote = json.loads(r.read())["version"]
        return {"local": local, "remote": remote, "upToDate": local == remote}
    except Exception as exc:
        return {"local": local, "remote": None, "upToDate": None, "error": str(exc)}


def _do_update() -> dict:
    root = str(Path(__file__).parent)
    try:
        result = subprocess.run(
            ["git", "pull", "origin", "master"],
            cwd=root, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return {"status": "ok", "output": result.stdout.strip()}
        else:
            return {"status": "error", "output": result.stderr.strip() or result.stdout.strip()}
    except FileNotFoundError:
        return {"status": "error", "output": "git not found — update manually by re-downloading the repository."}
    except Exception as exc:
        return {"status": "error", "output": str(exc)}


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/check-update":
            self._send_json(_check_update())
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/start-survey":
            try:
                self._send_json({"status": _launch_survey()})
            except Exception as exc:
                self._send_json({"status": "error", "message": str(exc)}, 500)
        elif self.path == "/api/update":
            try:
                self._send_json(_do_update())
            except Exception as exc:
                self._send_json({"status": "error", "output": str(exc)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        # Only log non-asset requests to keep the console readable
        if args and not any(str(args[0]).startswith(p) for p in ("/icons/", "/maps/")):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    print(f"GorgonHelper serving on http://localhost:{PORT}")
    with http.server.ThreadingHTTPServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
