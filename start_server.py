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
from pathlib import Path

PORT = 3000
SURVEY_SCRIPT = Path(__file__).parent / "Survey" / "main.pyw"
SURVEY_WS_PORT = 8765


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


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/api/start-survey":
            try:
                status = _launch_survey()
                body = json.dumps({"status": status}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                body = json.dumps({"status": "error", "message": str(exc)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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
    with http.server.HTTPServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
