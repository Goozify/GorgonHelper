"""
ws_bridge.py — GorgonHelper WebSocket bridge

Watches the Project: Gorgon Reports folder and Player.log, then streams
changes to connected browser clients over WebSocket. Enables automatic
folder-watch and live player-log support in Firefox and any browser that
doesn't support the File System Access API.

Usage:
    python ws_bridge.py

The bridge listens on ws://localhost:8766. GorgonHelper.html connects
automatically on page load and falls back to the native File System
Access API when the bridge is not running.
"""

import asyncio
import glob
import json
import logging
import os
import re
import sys
from pathlib import Path

try:
    import websockets
except ImportError:
    sys.exit(
        "ERROR: 'websockets' is not installed.\n"
        "Run:  pip install -r requirements.txt"
    )

# Suppress noisy tracebacks when a client connects but immediately closes
# before completing the WebSocket handshake (browser probes, port scanners, etc.)
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)

# ── Configuration ──────────────────────────────────────────────────────────
PORT          = 8766
POLL_INTERVAL = 2.0        # seconds between file-system polls

# ── Path detection ─────────────────────────────────────────────────────────
def _locate_game() -> tuple[Path, Path]:
    """Return (reports_folder, player_log) auto-detected from %APPDATA%."""
    appdata = Path(os.environ.get("APPDATA", ""))
    base    = appdata.parent / "LocalLow" / "Elder Game" / "Project Gorgon"
    return base / "Reports", base / "Player.log"

REPORTS_FOLDER, PLAYER_LOG = _locate_game()
CHAT_LOGS_DIR = REPORTS_FOLDER.parent / "ChatLogs"

# XP gain regex — matches both normal and level-up lines
_XP_RE = re.compile(
    r"\[Status\] You earned (\d+) XP in ([^.!]+?)(?:\s+and reached level (\d+))?[.!]"
)

# Favor gain regex — "You gained 50 favor with Falkrin Overstrike."
_FAVOR_RE = re.compile(r"You gained (\d+) favor with (.+?)\.")

# ── Per-connection state ───────────────────────────────────────────────────
_clients: set = set()

# Global file-mtime cache shared across all poll cycles
_file_mtimes: dict[str, float] = {}

# Player-log position — starts at end-of-file so we don't replay old history
_log_pos: int = 0

# Chat-log tailing state
_chat_file: str = ""   # absolute path of the Chat-*.log currently being tailed
_chat_pos:  int = 0    # byte offset read so far in that file

# ── Helpers ────────────────────────────────────────────────────────────────
EXPORTS_FOLDER = REPORTS_FOLDER / "character_exports"

# Matches game-exported files in the root Reports folder that should be moved:
#   Character_*.json   — CharacterSheet reports
#   *_items_*Z.json    — Storage/inventory snapshot reports
_ROOT_RE = re.compile(r"^(?:Character_.+|.+_items_.+Z)\.json$")


def _move_exports() -> None:
    """
    Move game-exported character files from the root Reports folder into
    character_exports/, mirroring what Chrome's moveGameExports() does.
    This keeps one canonical location for character data.
    """
    if not REPORTS_FOLDER.exists():
        return
    try:
        EXPORTS_FOLDER.mkdir(exist_ok=True)
    except Exception:
        return
    for p in list(REPORTS_FOLDER.iterdir()):
        if not p.is_file() or p.suffix != ".json":
            continue
        if not _ROOT_RE.match(p.name):
            continue
        dest = EXPORTS_FOLDER / p.name
        try:
            p.replace(dest)
            print(f"[bridge] ↳ {p.name} → character_exports/")
        except Exception:
            pass


def _iter_watched() -> list[tuple[Path, str, float]]:
    """
    Return list of (abs_path, bridge_path, mtime) for every file the bridge tracks.

    bridge_path is the relative path sent to the client:
      - character_exports/  →  "character_exports/Foo_items_...Z.json"
      - Json/ game data     →  "Json/items.json"

    For character_exports/ the rules are:
      - Character_*.json  — always included (one per character)
      - *_items_*Z.json   — only the SINGLE NEWEST file per character key
        (the key is the filename prefix before "_items_").
        Sending all 200+ stale snapshots wastes bandwidth and CPU; the HTML
        already picks the newest by mtime when multiple arrive, but there is no
        reason to send the old ones at all.
    """
    result = []

    # ── character_exports/ ───────────────────────────────────────────────────
    if EXPORTS_FOLDER.exists():
        # Pass 1: collect every file with its mtime
        char_files: list[tuple[Path, float]] = []
        inv_best: dict[str, tuple[Path, float]] = {}  # charKey → (path, mtime)

        for p in EXPORTS_FOLDER.iterdir():
            if not p.is_file() or p.suffix != ".json":
                continue
            try:
                mtime = p.stat().st_mtime
            except Exception:
                continue
            name = p.name
            if name.startswith("Character_"):
                char_files.append((p, mtime))
            elif "_items_" in name and name.endswith("Z.json"):
                # Key = everything before "_items_"
                key = name[: name.index("_items_")]
                if key not in inv_best or mtime > inv_best[key][1]:
                    inv_best[key] = (p, mtime)

        for p, mtime in char_files:
            result.append((p, "character_exports/" + p.name, mtime))
        for p, mtime in inv_best.values():
            result.append((p, "character_exports/" + p.name, mtime))

    # ── Json/ game data ──────────────────────────────────────────────────────
    json_dir = REPORTS_FOLDER / "Json"
    if json_dir.exists():
        for p in json_dir.iterdir():
            if not p.is_file() or p.suffix != ".json":
                continue
            try:
                mtime = p.stat().st_mtime
            except Exception:
                continue
            result.append((p, "Json/" + p.name, mtime))

    return result


async def _broadcast(msg: dict) -> None:
    if not _clients:
        return
    data = json.dumps(msg, ensure_ascii=False)
    await asyncio.gather(
        *(c.send(data) for c in list(_clients)),
        return_exceptions=True,
    )


def _read_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _tail_log(from_pos: int) -> tuple[str, int]:
    """
    Read new bytes from Player.log starting at from_pos.
    Returns (text_chunk, new_position).
    """
    if not PLAYER_LOG.exists():
        return "", from_pos
    try:
        size = PLAYER_LOG.stat().st_size
        if size <= from_pos:
            return "", from_pos
        with open(PLAYER_LOG, "rb") as f:
            f.seek(from_pos)
            raw = f.read(size - from_pos)
        text = raw.decode("utf-8", errors="replace")
        # Only process up to the last complete newline
        last_nl = text.rfind("\n")
        if last_nl < 0:
            return "", from_pos
        chunk = text[: last_nl + 1]
        new_pos = from_pos + len(chunk.encode("utf-8"))
        return chunk, new_pos
    except Exception:
        return "", from_pos


def _find_newest_chat_log() -> Path | None:
    """Return the most recently modified Chat-*.log in the ChatLogs directory."""
    if not CHAT_LOGS_DIR.exists():
        return None
    files = glob.glob(str(CHAT_LOGS_DIR / "Chat-*.log"))
    if not files:
        return None
    return Path(max(files, key=lambda p: Path(p).stat().st_mtime))


def _tail_chat_log() -> list[dict]:
    """Read new lines from the newest Chat-*.log and return XP/level-up events."""
    global _chat_file, _chat_pos

    chat_path = _find_newest_chat_log()
    if not chat_path:
        return []

    # Switched to a new session log → seek to end, skip history
    if str(chat_path) != _chat_file:
        _chat_file = str(chat_path)
        try:
            _chat_pos = chat_path.stat().st_size
        except Exception:
            _chat_pos = 0
        return []

    try:
        size = chat_path.stat().st_size
        if size <= _chat_pos:
            return []
        with open(chat_path, "rb") as f:
            f.seek(_chat_pos)
            raw = f.read(size - _chat_pos)
        text = raw.decode("utf-8", errors="replace")
        last_nl = text.rfind("\n")
        if last_nl < 0:
            return []
        chunk = text[: last_nl + 1]
        _chat_pos += len(chunk.encode("utf-8"))

        events = []
        for line in chunk.splitlines():
            m = _XP_RE.search(line)
            if m:
                xp        = int(m.group(1))
                skill     = m.group(2).strip()
                new_level = int(m.group(3)) if m.group(3) else None
                events.append({"type": "xp_gained", "skill": skill, "xp": xp})
                if new_level:
                    events.append({"type": "level_up", "skill": skill, "level": new_level})
            fm = _FAVOR_RE.search(line)
            if fm:
                events.append({
                    "type":     "favor_gained",
                    "npc_name": fm.group(2).strip(),
                    "amount":   int(fm.group(1)),
                })
        return events
    except Exception:
        return []


def _find_session_start() -> int:
    """
    Return the byte offset of the last ProcessAddPlayer line in Player.log.
    This is where the current game session began, so currentLogChar can be
    established on the client even if the login happened hours ago.
    Scans the last 10 MB; falls back to 0 if not found.
    """
    if not PLAYER_LOG.exists():
        return 0
    try:
        size = PLAYER_LOG.stat().st_size
        scan_from = max(0, size - 10 * 1024 * 1024)
        with open(PLAYER_LOG, "rb") as f:
            f.seek(scan_from)
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        idx = text.rfind("ProcessAddPlayer(")
        if idx < 0:
            return scan_from  # no login event found in last 10 MB — start here anyway
        # Walk back to the start of that line
        line_start = text.rfind("\n", 0, idx)
        line_start = line_start + 1 if line_start >= 0 else 0
        return scan_from + len(text[:line_start].encode("utf-8"))
    except Exception:
        return 0


# ── WebSocket handler ──────────────────────────────────────────────────────
async def _handler(ws) -> None:
    global _log_pos

    _clients.add(ws)
    addr = getattr(ws, "remote_address", "?")
    print(f"[bridge] + client {addr}  ({len(_clients)} connected)")

    try:
        # 1. Hello
        await ws.send(json.dumps({
            "type":      "hello",
            "version":   "1",
            "folder":    str(REPORTS_FOLDER),
            "playerLog": str(PLAYER_LOG),
        }))

        # 2. Move any pending game exports from root into character_exports/
        _move_exports()

        # 3. Initial file dump
        # mtime is included so the client can pick the newest file when multiple
        # timestamped snapshots exist for the same character (e.g. *_items_*Z.json).
        # Record mtimes so the poll loop doesn't re-broadcast unchanged files.
        for path, bpath, mtime in _iter_watched():
            content = _read_file(path)
            if content is not None:
                await ws.send(json.dumps({"type": "file", "path": bpath, "content": content, "mtime": mtime}))
                _file_mtimes[str(path)] = mtime

        # 4. Initial player-log send — from the last ProcessAddPlayer line so the
        #    client always receives the login event and can detect the current character,
        #    regardless of how long ago the session started.
        session_start = _find_session_start()
        chunk, _ = _tail_log(session_start)
        if chunk:
            lines = [l for l in chunk.splitlines() if l]
            if lines:
                await ws.send(json.dumps({"type": "playerlog", "lines": lines}))

        # 5. Signal that the initial dump is complete
        await ws.send(json.dumps({"type": "ready"}))

        await ws.wait_closed()

    finally:
        _clients.discard(ws)
        print(f"[bridge] - client {addr}  ({len(_clients)} connected)")


# ── Poll loop ──────────────────────────────────────────────────────────────
async def _poll_loop() -> None:
    global _log_pos

    # Initialise log positions to current end so we don't replay history
    if PLAYER_LOG.exists():
        _log_pos = PLAYER_LOG.stat().st_size
    # Initialise chat log position (sets _chat_file / _chat_pos via the tail helper)
    _tail_chat_log()

    while True:
        await asyncio.sleep(POLL_INTERVAL)

        # Move any new game exports, then watch for changed files
        _move_exports()
        for path, bpath, mtime in _iter_watched():
            key = str(path)
            if _file_mtimes.get(key) == mtime:
                continue
            _file_mtimes[key] = mtime
            content = _read_file(path)
            if content is not None:
                await _broadcast({"type": "file", "path": bpath, "content": content, "mtime": mtime})
                print(f"[bridge] → {bpath}")

        # Tail Player.log for new lines
        chunk, new_pos = _tail_log(_log_pos)
        if chunk:
            _log_pos = new_pos
            lines = [l for l in chunk.splitlines() if l]
            if lines:
                await _broadcast({"type": "playerlog", "lines": lines})

        # Tail Chat-*.log for XP / favor / level-up events
        for event in _tail_chat_log():
            await _broadcast(event)
            t = event.get("type", "?")
            if t == "favor_gained":
                print(f"[bridge] ♥   {event['npc_name']} +{event['amount']} favor")
            else:
                print(f"[bridge] XP  {event.get('skill','?')} +{event.get('xp', event.get('level', '?'))}")


# ── Entry point ────────────────────────────────────────────────────────────
async def main() -> None:
    print(f"[bridge] Reports folder : {REPORTS_FOLDER}")
    print(f"[bridge] Player.log     : {PLAYER_LOG}")
    if not REPORTS_FOLDER.exists():
        print("[bridge] WARNING: Reports folder not found — will watch when it appears")
    if not PLAYER_LOG.exists():
        print("[bridge] WARNING: Player.log not found — will watch when it appears")
    print(f"[bridge] Listening on   ws://localhost:{PORT}")
    print(f"[bridge] Press Ctrl+C to stop.\n")

    async with websockets.serve(_handler, "localhost", PORT):
        await _poll_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[bridge] Stopped.")
