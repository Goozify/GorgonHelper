"""Headless Survey Helper backend.

Replaces the PyQt5 MainWindow with a WebSocket server so the browser-based
GorgonHelper app can serve as the full UI.  All OS-level work (overlays,
screen capture, mouse hooks, chat tailing) stays in Python; state and
events stream to the browser over ws://localhost:8765.
"""
import asyncio
import ctypes
import time
import http.server
import json
import logging
import subprocess
import threading
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Set

import websockets

from config import Config
from chat_watcher import ChatWatcher
from survey_store import SurveyStore, SurveyLocation
from safecracking import SafecrackingSolver, capture_symbols
from route_solver import nearest_neighbor_route
from PyQt5.QtWidgets import QApplication
from ui_inventory_overlay import InventoryOverlay
from ui_game_map_overlay import GameMapOverlay
from ui_region_selector import RegionSelector
from ui_region_highlighter import RegionHighlighter
from inventory_click_watcher import InventoryClickWatcher
from keyboard_hotkey import KeyboardHotkey

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [Survey] %(levelname)s %(message)s")
log = logging.getLogger(__name__)
# Set to logging.INFO to suppress debug trace; logging.DEBUG to see full visit flow
log.setLevel(logging.DEBUG)

WS_HOST = "localhost"
WS_PORT = 8765
HTTP_PORT = 3000
_HTTP_ROOT = Path(__file__).parent.parent  # Reports root
_VERSION_URL = "https://raw.githubusercontent.com/Streak5274/GorgonHelper/master/version.json"


# ---------------------------------------------------------------------------
# HTTP file server (serves the GorgonHelper app + API endpoints)
# ---------------------------------------------------------------------------

def _local_version() -> str:
    try:
        return json.loads((_HTTP_ROOT / "version.json").read_text())["version"]
    except Exception:
        return "unknown"


def _check_update() -> dict:
    local = _local_version()
    try:
        req = urllib.request.Request(_VERSION_URL, headers={"User-Agent": "GorgonHelper"})
        with urllib.request.urlopen(req, timeout=5) as r:
            remote = json.loads(r.read())["version"]
        return {"local": local, "remote": remote, "upToDate": local == remote}
    except Exception as exc:
        return {"local": local, "remote": None, "upToDate": None, "error": str(exc)}


def _do_update() -> dict:
    try:
        result = subprocess.run(
            ["git", "pull", "origin", "master"],
            cwd=str(_HTTP_ROOT), capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return {"status": "ok", "output": result.stdout.strip()}
        return {"status": "error", "output": result.stderr.strip() or result.stdout.strip()}
    except FileNotFoundError:
        return {"status": "error", "output": "git not found — re-download to update."}
    except Exception as exc:
        return {"status": "error", "output": str(exc)}


class _GorgonHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(_HTTP_ROOT), **kwargs)

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
            self._send_json({"status": "already_running"})
        elif self.path == "/api/update":
            try:
                self._send_json(_do_update())
            except Exception as exc:
                self._send_json({"status": "error", "output": str(exc)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        if args and not any(str(args[0]).startswith(p) for p in ("/icons/", "/maps/")):
            log.debug("HTTP %s", args[0])


def _start_http_server() -> bool:
    """Start the HTTP file server in a daemon thread. Returns False if port taken."""
    try:
        srv = http.server.ThreadingHTTPServer(("", HTTP_PORT), _GorgonHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True, name="http-server")
        t.start()
        log.info("HTTP server started on http://localhost:%d", HTTP_PORT)
        return True
    except OSError as exc:
        # errno 10048 = WSAEADDRINUSE on Windows; 98 = EADDRINUSE on Linux
        if exc.errno in (98, 10048) or "address already in use" in str(exc).lower():
            log.info("Port %d already in use — start_server.py is probably running", HTTP_PORT)
        else:
            log.warning("Could not start HTTP server: %s", exc)
        return False


def _loc_to_dict(loc: SurveyLocation) -> dict:
    d = asdict(loc)
    # Remove heavy/internal fields not needed by browser
    d.pop("east_relative", None)
    d.pop("south_relative", None)
    d.pop("pixel_x", None)
    d.pop("pixel_y", None)
    return d


class SurveyServer:
    def __init__(self):
        self.config = Config.load()
        self.store = SurveyStore()
        self.store.clear_all()  # fresh session each launch

        # Qt overlay widgets (created here, shown/hidden as needed)
        self.inv_overlay = InventoryOverlay()
        self.map_overlay = GameMapOverlay()

        # Register callbacks so the overlay fires into our broadcast loop
        self.map_overlay.set_position_callback(self._on_player_pos)
        self.map_overlay.set_pin_callback(self._on_circle_pin)

        # Chat watcher (created on start_setup, None otherwise)
        self.chat_watcher: Optional[ChatWatcher] = None

        # Inventory click watcher (global Win32 mouse hook)
        self.click_watcher = InventoryClickWatcher()

        # State
        self._surveying = False
        self._setup_complete = False
        self._current_scan_slot = 0
        self._route_id_order: List[int] = []
        self._route_mapped: List[int] = []
        self._current_slot_labels: dict = {}
        self._pending_visit_loc: Optional[SurveyLocation] = None
        self._pending_timeout_handle = None
        # After a pending times out we keep the loc here briefly so that late
        # chat-log confirmations (arriving a few seconds after the timeout) can
        # still mark the survey as visited.
        self._grace_loc: Optional[SurveyLocation] = None
        self._grace_time: float = 0.0   # time.monotonic() when grace started
        self._last_arrow_px: Optional[int] = None
        self._last_arrow_py: Optional[int] = None

        # WebSocket clients
        self._clients: Set[websockets.WebSocketServerProtocol] = set()

        # Clean shutdown signal — set by cmd_shutdown to exit run() normally
        self._shutdown_event = asyncio.Event()

        # Auto-use hotkey state
        self._auto_use_active: bool = False
        self._auto_use_event: Optional[asyncio.Event] = None   # wakes on survey_detected
        self._auto_use_pin_event: Optional[asyncio.Event] = None  # wakes on circle_pin_added
        self._hotkey: Optional[KeyboardHotkey] = None
        self._single_use_hotkey: Optional[KeyboardHotkey] = None

        # Region selector overlay (keep reference so Qt doesn't GC it)
        self._region_selector: Optional[RegionSelector] = None

        # ── Safecracking state ──────────────────────────────────────────
        self._sc_symbols: List[str] = []          # base64 thumbnails (up to 12)
        self._sc_solver: Optional[SafecrackingSolver] = None

        # Region highlighter — transparent border shown while editing coords
        self._highlighter = RegionHighlighter()

        # Apply saved map region if configured
        if self.config.map_capture.w > 0:
            self.map_overlay.configure_region(
                self.config.map_capture.x, self.config.map_capture.y,
                self.config.map_capture.w, self.config.map_capture.h,
            )

    # ------------------------------------------------------------------
    # Async entry point
    # ------------------------------------------------------------------

    async def run(self):
        """Top-level coroutine started from main.pyw."""
        # Start HTTP file server (serves root GorgonHelper app + API endpoints)
        _start_http_server()

        # Connect click watcher signal — use lambda so asyncio.ensure_future
        # schedules the coroutine; @asyncSlot doesn't work with typed signals.
        self.click_watcher.double_clicked_slot.connect(
            lambda slot: asyncio.ensure_future(self._on_inv_double_click(slot))
        )

        # Global keyboard hotkeys
        # Auto-use is a debug/advanced feature — only register if explicitly enabled in config.json.
        if self.config.debug_auto_use:
            self._hotkey = KeyboardHotkey(
                self.config.auto_use_hotkey_vk,
                lambda: asyncio.ensure_future(self._on_hotkey_press()),
                self.config.auto_use_hotkey_mods,
                active_window_contains="Project Gorgon",
            )
            self._hotkey.start()
            log.info("Auto-use hotkey registered (VK 0x%02X mods=0x%X)",
                     self.config.auto_use_hotkey_vk, self.config.auto_use_hotkey_mods)
        else:
            self._hotkey = None
            log.info("Auto-use hotkey disabled (debug_auto_use=false in config.json)")

        self._single_use_hotkey = KeyboardHotkey(
            self.config.single_use_hotkey_vk,
            lambda: asyncio.ensure_future(self._on_single_use_press()),
            self.config.single_use_hotkey_mods,
            active_window_contains="Project Gorgon",
        )
        self._single_use_hotkey.start()
        log.info("Single-use hotkey registered (VK 0x%02X mods=0x%X)",
                 self.config.single_use_hotkey_vk, self.config.single_use_hotkey_mods)

        log.info("Starting WebSocket server on ws://%s:%d", WS_HOST, WS_PORT)
        async with websockets.serve(self._handle_client, WS_HOST, WS_PORT):
            log.info("Survey Helper running — waiting for browser connection")
            await self._shutdown_event.wait()  # run until cmd_shutdown sets this

        log.info("WebSocket server closed — exiting")
        QApplication.quit()

    # ------------------------------------------------------------------
    # WebSocket handling
    # ------------------------------------------------------------------

    async def _handle_client(self, ws):
        self._clients.add(ws)
        log.info("Browser connected (%d total)", len(self._clients))
        try:
            # Send full state immediately on connect
            await self._send_state_full(ws)
            await self._send(ws, self._sc_build_state())
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    await self._handle_command(ws, msg)
                except json.JSONDecodeError:
                    pass
        except websockets.ConnectionClosed:
            pass
        except Exception:
            log.exception("Unhandled error in WebSocket handler")
        finally:
            self._clients.discard(ws)
            log.info("Browser disconnected (%d remaining)", len(self._clients))

    async def _send(self, ws, obj: dict):
        try:
            await ws.send(json.dumps(obj))
        except websockets.ConnectionClosed:
            pass

    async def broadcast(self, obj: dict):
        if not self._clients:
            return
        msg = json.dumps(obj)
        await asyncio.gather(
            *(self._safe_send(ws, msg) for ws in list(self._clients)),
            return_exceptions=True,
        )

    @staticmethod
    async def _safe_send(ws, msg: str):
        try:
            await ws.send(msg)
        except websockets.ConnectionClosed:
            pass

    # ------------------------------------------------------------------
    # State serialisation
    # ------------------------------------------------------------------

    def _build_state_full(self) -> dict:
        locs = self.store.get_all(self.config.active_area)
        slot_labels_str = {str(k): v for k, v in self._current_slot_labels.items()}
        return {
            "type": "state_full",
            "surveying": self._surveying,
            "setup_complete": self._setup_complete,
            "active_area": self.config.active_area,
            "player_east": self.config.player_east,
            "player_south": self.config.player_south,
            "config": {
                "inventory": asdict(self.config.inventory),
                "map_capture": asdict(self.config.map_capture),
                "safecracking_region": asdict(self.config.safecracking_region),
                "chat_log_dir": self.config.chat_log_dir,
                "debug_auto_use":         self.config.debug_auto_use,
                "auto_use_hotkey_vk":     self.config.auto_use_hotkey_vk,
                "auto_use_hotkey_mods":   self.config.auto_use_hotkey_mods,
                "single_use_hotkey_vk":   self.config.single_use_hotkey_vk,
                "single_use_hotkey_mods": self.config.single_use_hotkey_mods,
            },
            "locations": [_loc_to_dict(l) for l in locs],
            "route_id_order": self._route_id_order,
            "slot_labels": slot_labels_str,
            "status": "Connected — Survey Helper running",
        }

    async def _send_state_full(self, ws=None):
        msg = self._build_state_full()
        if ws:
            await self._send(ws, msg)
        else:
            await self.broadcast(msg)

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    async def _handle_command(self, ws, msg: dict):
        t = msg.get("type", "")

        if t == "cmd_start_setup":
            await self._start_setup()
        elif t == "cmd_stop_setup":
            await self._stop_setup()
        elif t == "cmd_recalculate_route":
            await self._calculate_route()
        elif t == "cmd_mark_visited":
            loc_id = msg.get("location_id")
            if loc_id is not None:
                loc = self.store.get_by_id(loc_id)
                if loc:
                    await self._mark_location_visited(loc)
        elif t == "cmd_unmark_visited":
            loc_id = msg.get("location_id")
            if loc_id is not None:
                loc = self.store.get_by_id(loc_id)
                if loc and loc.visited:
                    await self._unmark_location_visited(loc)
        elif t == "cmd_clear_area":
            await self._clear_area()
        elif t == "cmd_clear_all":
            self.store.clear_all()
            self._route_id_order = []
            self._route_mapped = []
            self._current_slot_labels = {}
            self._pending_visit_loc = None
            self.map_overlay.clear_circle_pins()
            self.map_overlay.update_survey_data([], [])
            self.map_overlay.set_visible(False)
            self.inv_overlay.set_overlay_visible(False)
            self.click_watcher.stop()
            await self._send_state_full()
        elif t == "cmd_capture_screenshot":
            purpose = msg.get("purpose", "inventory")
            self._highlighter.hide_region()
            self._launch_region_selector(purpose)
        elif t == "cmd_highlight_region":
            self._highlighter.show_region(
                int(msg.get("x", 0)), int(msg.get("y", 0)),
                int(msg.get("w", 0)), int(msg.get("h", 0)),
            )
        elif t == "cmd_hide_highlight":
            self._highlighter.hide_region()
        elif t == "cmd_update_config":
            await self._update_config(msg)
        # ── Safecracking commands ───────────────────────────────────────
        elif t == "sc_select_region":
            self._highlighter.hide_region()
            self._launch_region_selector("safecracking")
        elif t == "sc_capture":
            await self._sc_capture()
        elif t == "sc_reset":
            symbol_count = int(msg.get("symbol_count", 12))
            await self._sc_reset(symbol_count)
        elif t == "sc_record":
            guess   = [int(v) for v in msg.get("guess", [])]
            exact   = int(msg.get("exact", 0))
            misplaced = int(msg.get("misplaced", 0))
            await self._sc_record(guess, exact, misplaced)
        elif t == "sc_undo":
            await self._sc_undo()
        elif t == "cmd_ping":
            await self._send(ws, {"type": "pong"})
        elif t == "cmd_shutdown":
            await self._send(ws, {"type": "shutdown_ack"})
            log.info("Shutdown requested by browser")
            self._shutdown_event.set()  # lets run() exit async with cleanly

    # ------------------------------------------------------------------
    # Setup / Stop
    # ------------------------------------------------------------------

    async def _start_setup(self):
        self._surveying = True
        self._setup_complete = False

        # Stop any existing watcher
        self.click_watcher.stop()
        if self.chat_watcher:
            self.chat_watcher.stop()
            self.chat_watcher.wait(2000)
            self.chat_watcher = None

        # Reset state
        self.store.clear_area(self.config.active_area)
        self._route_id_order = []
        self._route_mapped = []
        self._current_scan_slot = 0
        self._current_slot_labels = {}
        self._pending_visit_loc = None
        self.map_overlay.clear_circle_pins()
        self.map_overlay._setup_active = True
        self.inv_overlay.set_slot_labels({})

        # Configure and show overlays
        inv = self.config.inventory
        self.inv_overlay.configure(
            inv.screen_x, inv.screen_y,
            inv.slot_width, inv.slot_height,
            inv.grid_cols, inv.grid_rows, inv.slot_gap,
            inv.padding_left, inv.padding_top,
        )
        self.inv_overlay.set_current_slot(0)
        self.inv_overlay.set_overlay_visible(True)

        if self.config.map_capture.w > 0:
            self.map_overlay.configure_region(
                self.config.map_capture.x, self.config.map_capture.y,
                self.config.map_capture.w, self.config.map_capture.h,
            )
        self.map_overlay.set_visible(True)

        # Start chat watcher
        self.chat_watcher = ChatWatcher(self.config.chat_log_dir, skip_existing=True)
        self.chat_watcher.survey_detected.connect(
            lambda name, e, s: asyncio.ensure_future(self._on_survey_detected(name, e, s))
        )
        self.chat_watcher.survey_completed.connect(
            lambda name: asyncio.ensure_future(self._on_survey_completed(name))
        )
        self.chat_watcher.area_changed.connect(
            lambda area: asyncio.ensure_future(self._on_area_detected(area))
        )
        self.chat_watcher.error_occurred.connect(
            lambda msg: asyncio.ensure_future(self._on_watch_error(msg))
        )
        self.chat_watcher.start()

        await self.broadcast({
            "type": "state_full",
            **{k: v for k, v in self._build_state_full().items() if k != "type"},
            "status": "Setup active — use survey items to mark locations",
        })
        log.info("Setup started in area %s", self.config.active_area)

    async def _stop_setup(self):
        self._surveying = False
        self._setup_complete = True

        self.inv_overlay.set_overlay_visible(False)
        self.map_overlay._setup_active = False

        # Assign detected circle pins to locations by scan order
        all_locs = self.store.get_all(self.config.active_area)
        pins = self.map_overlay._circle_pins
        for i, loc in enumerate(all_locs):
            if i < len(pins):
                loc.pixel_x = float(pins[i][0])
                loc.pixel_y = float(pins[i][1])
        self.store.save()

        # Always tell the frontend setup ended so the button flips even when
        # there are 0 locations (route_calculated is never sent in that case).
        await self.broadcast({
            "type": "setup_stopped",
            "locations": [_loc_to_dict(l) for l in all_locs],
        })

        self.map_overlay.calibrate(all_locs)
        await self._calculate_route()
        self.map_overlay.update()
        log.info("Setup stopped, route calculated for %d locations", len(all_locs))

    # ------------------------------------------------------------------
    # Chat watcher callbacks  (Qt signals → asyncio via qasync)
    # ------------------------------------------------------------------

    async def _on_survey_detected(self, item_name: str, east: int, south: int):
        log.debug("DETECTED  item=%r  rel=(%+d E, %+d S)  pending=%s",
                  item_name, east, south,
                  f"#{self._pending_visit_loc.id}" if self._pending_visit_loc else "none")
        if self._setup_complete:
            # Route mode: coordinate hint — only set pending if not already set
            # by a slot double-click. Overwriting causes the wrong location to be
            # marked when surveys are used back-to-back faster than 10 seconds.
            if self._pending_visit_loc is not None:
                log.debug("DETECTED  skipped (pending already set to #%d)", self._pending_visit_loc.id)
                return
            east_abs = self.config.player_east + east
            south_abs = self.config.player_south + south
            best_loc = None
            best_dist = float("inf")
            for loc in self.store.get_unvisited(self.config.active_area):
                if loc.east_absolute is None:
                    continue
                dx = loc.east_absolute - east_abs
                dy = loc.south_absolute - south_abs
                d = (dx * dx + dy * dy) ** 0.5
                if d < best_dist:
                    best_dist = d
                    best_loc = loc
            if best_loc and best_dist < 50:
                log.debug("DETECTED  coord match → #%d %r  dist=%.1f  abs=(%+d E, %+d S)",
                          best_loc.id, best_loc.item_name, best_dist, east_abs, south_abs)
                self._pending_visit_loc = best_loc
                self._reset_pending_timeout()
            else:
                log.debug("DETECTED  no coord match  abs=(%+d E, %+d S)  best_dist=%.1f",
                          east_abs, south_abs, best_dist if best_loc else float("inf"))
            return

        # Setup mode: add new location
        loc = self.store.add(
            area=self.config.active_area,
            item_name=item_name,
            east_rel=east,
            south_rel=south,
            player_east=self.config.player_east,
            player_south=self.config.player_south,
            inventory_slot=self._current_scan_slot,
        )

        if loc:
            self._current_scan_slot += 1
            self.inv_overlay.set_current_slot(self._current_scan_slot)
            # Wake up auto-use loop if it's waiting for this detection
            if self._auto_use_event and not self._auto_use_event.is_set():
                self._auto_use_event.set()
            await self.broadcast({
                "type": "survey_detected",
                "location": _loc_to_dict(loc),
                "scan_slot": self._current_scan_slot,
                "status": f"Detected: {item_name}  ({east:+d}E, {south:+d}S)  [slot {self._current_scan_slot}]",
            })
        else:
            # Also wake auto-use on duplicate so it doesn't timeout
            if self._auto_use_event and not self._auto_use_event.is_set():
                self._auto_use_event.set()
            await self.broadcast({
                "type": "survey_duplicate",
                "east": east, "south": south,
                "status": f"Duplicate skipped: {east:+d}E, {south:+d}S",
            })

    @staticmethod
    def _normalize_item_name(name: str) -> str:
        """Strip leading 'The '/'A '/'An ' and lowercase for loose comparison."""
        return name.lower().removeprefix("the ").removeprefix("a ").removeprefix("an ").strip()

    async def _on_survey_completed(self, item_name: str):
        if not self._setup_complete:
            return

        # Resolve which pending loc to use — active pending takes priority,
        # then fall back to the grace-period loc (recently timed out).
        loc = self._pending_visit_loc
        from_grace = False
        if loc is None:
            if (self._grace_loc is not None and
                    time.monotonic() - self._grace_time < self._GRACE_PERIOD):
                loc = self._grace_loc
                from_grace = True
                log.debug("COMPLETED item=%r  pending=none → using grace-period #%d %r",
                          item_name, loc.id, loc.item_name)
            else:
                log.debug("COMPLETED item=%r  pending=none (will NOT mark)", item_name)
                return
        else:
            log.debug("COMPLETED item=%r  pending=#%d %r",
                      item_name, loc.id, loc.item_name)

        # Guard: only mark if the completed item matches the pending survey item.
        # Enemy loot also fires survey_completed ("You receive Hops") — reject those.
        if self._normalize_item_name(item_name) != self._normalize_item_name(loc.item_name):
            log.debug("COMPLETED item=%r doesn't match %r — ignoring (enemy loot?)",
                      item_name, loc.item_name)
            return

        # Clear pending / grace state.
        # When the *active* pending is marked we also wipe the grace ref so that
        # any further COMPLETED events from the same multi-item survey batch
        # (e.g. the game reports "You receive Slab A, You receive Slab B" all at
        # once) don't accidentally match a previously timed-out grace survey.
        if from_grace:
            self._grace_loc = None
        else:
            self._pending_visit_loc = None
            self._grace_loc = None          # ← prevent same-batch grace false-mark
            if self._pending_timeout_handle:
                self._pending_timeout_handle.cancel()
                self._pending_timeout_handle = None

        await self._mark_location_visited(loc)

    async def _on_area_detected(self, area: str):
        self.config.active_area = area
        await self.broadcast({
            "type": "area_changed",
            "area": area,
            "status": f"Area detected: {area}",
        })

    async def _on_watch_error(self, msg: str):
        await self.broadcast({"type": "error", "message": f"Chat watcher: {msg}"})

    # ------------------------------------------------------------------
    # Inventory double-click  (Win32 hook → asyncio)
    # ------------------------------------------------------------------

    async def _on_inv_double_click(self, slot_index: int):
        log.debug("CLICK     slot=%d  pending=%s",
                  slot_index,
                  f"#{self._pending_visit_loc.id}" if self._pending_visit_loc else "none")
        if self._pending_visit_loc is not None:
            log.debug("CLICK     slot=%d ignored — pending already set", slot_index)
            return
        for loc_id in self._route_id_order:
            loc = self.store.get_by_id(loc_id)
            if loc and not loc.visited and loc.inventory_slot == slot_index:
                log.debug("CLICK     slot=%d → #%d %r", slot_index, loc.id, loc.item_name)
                self._pending_visit_loc = loc
                label = self._current_slot_labels.get(slot_index, "?")
                self._reset_pending_timeout()
                await self.broadcast({
                    "type": "status",
                    "message": f"Surveying #{label}… waiting for chat confirmation",
                })
                return

    _PENDING_TIMEOUT  = 20.0   # seconds before giving up on chat confirmation
    _GRACE_PERIOD     = 8.0    # seconds after timeout where late COMPLETED can still match

    def _reset_pending_timeout(self):
        if self._pending_timeout_handle:
            self._pending_timeout_handle.cancel()
        loop = asyncio.get_event_loop()
        self._pending_timeout_handle = loop.call_later(
            self._PENDING_TIMEOUT, self._timeout_pending_visit)

    def _timeout_pending_visit(self):
        if self._pending_visit_loc is not None:
            log.debug("TIMEOUT   pending #%d %r cleared after %.0f s with no chat confirmation",
                      self._pending_visit_loc.id, self._pending_visit_loc.item_name,
                      self._PENDING_TIMEOUT)
            # Keep a grace-period reference so late COMPLETED events can still match
            self._grace_loc  = self._pending_visit_loc
            self._grace_time = time.monotonic()
            self._pending_visit_loc = None
            asyncio.ensure_future(self.broadcast({
                "type": "status",
                "message": "Survey timed out — no confirmation from chat log",
            }))

    # ------------------------------------------------------------------
    # Mark visited
    # ------------------------------------------------------------------

    async def _mark_location_visited(self, loc: SurveyLocation):
        log.info("MARK      #%d %r  slot=%s  coords=(%s E, %s S)",
                 loc.id, loc.item_name, loc.inventory_slot,
                 f"{loc.east_absolute:+.0f}" if loc.east_absolute is not None else "?",
                 f"{loc.south_absolute:+.0f}" if loc.south_absolute is not None else "?")
        self.store.mark_visited(loc.id)
        consumed_slot = loc.inventory_slot

        # Game shifts all higher-indexed items left by 1
        if consumed_slot is not None:
            for ul in self.store.get_unvisited(self.config.active_area):
                if ul.inventory_slot is not None and ul.inventory_slot > consumed_slot:
                    self.store.update_slot(ul.id, ul.inventory_slot - 1)

        self._rebuild_slot_labels()

        all_locs = self.store.get_all(self.config.active_area)
        self.map_overlay.update_survey_data(all_locs, self._route_mapped)

        remaining = len(self.store.get_unvisited(self.config.active_area))
        if remaining == 0:
            self.inv_overlay.set_overlay_visible(False)

        slot_labels_str = {str(k): v for k, v in self._current_slot_labels.items()}
        await self.broadcast({
            "type": "survey_completed",
            "location_id": loc.id,
            "locations": [_loc_to_dict(l) for l in all_locs],
            "slot_labels": slot_labels_str,
            "remaining": remaining,
            "status": f"Visited: {loc.item_name} — {remaining} remaining" if remaining else "All survey locations visited!",
        })

    async def _unmark_location_visited(self, loc: SurveyLocation):
        restored_slot = loc.inventory_slot
        log.info("UNMARK    #%d %r  restoring slot=%s", loc.id, loc.item_name, restored_slot)
        # Reverse the slot shift: every currently-unvisited item at slot >= restored_slot
        # was shifted left when this item was consumed — shift them right again.
        if restored_slot is not None:
            for ul in self.store.get_unvisited(self.config.active_area):
                if ul.inventory_slot is not None and ul.inventory_slot >= restored_slot:
                    self.store.update_slot(ul.id, ul.inventory_slot + 1)
        self.store.mark_unvisited(loc.id)
        # inventory_slot on loc was preserved through mark_visited, so it's still correct.

        self._rebuild_slot_labels()
        all_locs = self.store.get_all(self.config.active_area)
        self.map_overlay.update_survey_data(all_locs, self._route_mapped)

        remaining = len(self.store.get_unvisited(self.config.active_area))
        if remaining > 0:
            self.inv_overlay.set_overlay_visible(True)

        slot_labels_str = {str(k): v for k, v in self._current_slot_labels.items()}
        await self.broadcast({
            "type": "survey_unmarked",
            "location_id": loc.id,
            "locations": [_loc_to_dict(l) for l in all_locs],
            "slot_labels": slot_labels_str,
            "remaining": remaining,
            "status": f"Unmarked: {loc.item_name} — {remaining} remaining",
        })

    def _rebuild_slot_labels(self):
        labels = {}
        first_unvisited_slot = None
        for route_num_0, loc_id in enumerate(self._route_id_order):
            loc = self.store.get_by_id(loc_id)
            if loc and not loc.visited and loc.inventory_slot is not None:
                route_num = route_num_0 + 1
                labels[loc.inventory_slot] = str(route_num)
                if first_unvisited_slot is None:
                    first_unvisited_slot = loc.inventory_slot
        self._current_slot_labels = labels
        self.inv_overlay.set_slot_labels(labels, first_unvisited_slot)
        self.inv_overlay.repaint()
        self.click_watcher.set_active_slots(set(labels.keys()))

    # ------------------------------------------------------------------
    # Auto-use hotkey
    # ------------------------------------------------------------------

    async def _on_hotkey_press(self):
        if not self._surveying:
            return
        if self._setup_complete:
            return  # auto-use only makes sense in setup mode
        if self._auto_use_active:
            self._auto_use_active = False
            log.info("AUTO-USE  cancelled by hotkey")
            await self.broadcast({"type": "status", "message": "Auto-use cancelled"})
        else:
            asyncio.ensure_future(self._auto_use_surveys())

    async def _on_single_use_press(self):
        """Use the current survey slot once.

        Setup mode:  clicks _current_scan_slot (the next un-scanned slot).
        Route mode:  clicks the inventory_slot of the first unvisited survey
                     in route order.
        """
        # Active setup: _surveying=True, _setup_complete=False
        # Route mode:   _surveying=False, _setup_complete=True
        in_setup = self._surveying and not self._setup_complete
        in_route = self._setup_complete and bool(self._route_id_order)
        if (not in_setup and not in_route) or self._auto_use_active:
            return

        if not self._setup_complete:
            # Setup mode
            slot = self._current_scan_slot
        else:
            # Route mode — find the first unvisited survey's inventory slot
            slot = None
            for loc_id in self._route_id_order:
                loc = self.store.get_by_id(loc_id)
                if loc and not loc.visited and loc.inventory_slot is not None:
                    slot = loc.inventory_slot
                    break
            if slot is None:
                log.debug("SINGLE-USE  no unvisited surveys remaining")
                return

        x, y = self._slot_screen_center(slot)
        log.debug("SINGLE-USE  clicking slot %d at (%d, %d)", slot, x, y)
        self._simulate_double_click(x, y)

    async def _auto_use_surveys(self):
        """Simulate double-clicking each survey slot in sequence until timeout."""
        if self._auto_use_active:
            return
        self._auto_use_active = True
        count = 0
        log.info("AUTO-USE  starting from slot %d", self._current_scan_slot)
        await self.broadcast({"type": "status", "message": "Auto-use: starting…"})
        self.map_overlay.set_fast_scan(True)   # 120ms instead of 600ms

        CHAT_TIMEOUT = 7.0   # seconds to wait for survey_detected (chat log)
        PIN_TIMEOUT  = 8.0   # seconds to wait for circle_pin_added (map OCR)
        INTER_DELAY  = 0.3   # brief pause after pin detected before next click

        try:
            while self._auto_use_active:
                slot = self._current_scan_slot
                x, y = self._slot_screen_center(slot)
                log.debug("AUTO-USE  clicking slot %d at (%d, %d)", slot, x, y)

                # Create pin event BEFORE the click — the red circle appears on map
                # almost immediately after clicking (visual feedback), while the chat
                # log entry takes 2-3 s.  Creating the event here ensures we don't miss
                # the detection while waiting for the chat event.
                self._auto_use_event = asyncio.Event()
                self._auto_use_pin_event = asyncio.Event()
                self._simulate_double_click(x, y)

                # Step 1: wait for chat log to confirm survey was used
                try:
                    await asyncio.wait_for(self._auto_use_event.wait(), timeout=CHAT_TIMEOUT)
                except asyncio.TimeoutError:
                    log.info("AUTO-USE  chat timeout at slot %d — no survey detected, stopping", slot)
                    msg = f"Auto-use done — {count} survey{'s' if count != 1 else ''} used"
                    await self.broadcast({"type": "status", "message": msg})
                    break

                # Step 2: pin event may already be set (detected during chat wait)
                if self._auto_use_pin_event.is_set():
                    log.debug("AUTO-USE  pin already detected for slot %d", slot)
                else:
                    try:
                        await asyncio.wait_for(self._auto_use_pin_event.wait(), timeout=PIN_TIMEOUT)
                        log.debug("AUTO-USE  pin detected for slot %d", slot)
                    except asyncio.TimeoutError:
                        log.warning("AUTO-USE  pin timeout at slot %d — map OCR didn't detect crosshair", slot)

                count += 1
                await asyncio.sleep(INTER_DELAY)
        finally:
            self._auto_use_active = False
            self._auto_use_event = None
            self._auto_use_pin_event = None
            self.map_overlay.set_fast_scan(False)  # back to 600ms

    def _slot_screen_center(self, slot_index: int):
        """Return (screen_x, screen_y) of the centre of an inventory slot."""
        inv = self.config.inventory
        col = slot_index % inv.grid_cols
        row = slot_index // inv.grid_cols
        x = (inv.screen_x + inv.padding_left
             + col * (inv.slot_width + inv.slot_gap)
             + inv.slot_width // 2)
        y = (inv.screen_y + inv.padding_top
             + row * (inv.slot_height + inv.slot_gap)
             + inv.slot_height // 2)
        return x, y

    def _simulate_double_click(self, x: int, y: int):
        """Move cursor, fire two left-click pairs (double-click), then restore cursor."""
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP   = 0x0004
        # Save current cursor position so we can restore it afterwards
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        prev_x, prev_y = pt.x, pt.y

        ctypes.windll.user32.SetCursorPos(x, y)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP,   0, 0, 0, 0)
        time.sleep(0.05)   # within the system double-click window
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP,   0, 0, 0, 0)

        # Wait briefly before restoring so the game engine has time to register the
        # double-click at (x, y).  Moving the cursor immediately after the events are
        # posted can cause the game to see the item as no longer hovered and discard
        # the action before processing it.
        time.sleep(0.15)
        ctypes.windll.user32.SetCursorPos(prev_x, prev_y)

    # ------------------------------------------------------------------
    # Route calculation
    # ------------------------------------------------------------------

    async def _calculate_route(self):
        area = self.config.active_area
        unvisited = self.store.get_unvisited(area)
        if not unvisited:
            await self.broadcast({"type": "status", "message": "No unvisited locations — nothing to route."})
            return

        points = []
        valid_locs = []
        for loc in unvisited:
            if loc.east_absolute is not None:
                points.append((loc.east_absolute, loc.south_absolute))
                valid_locs.append(loc)

        if not points:
            await self.broadcast({"type": "status", "message": "No locations with coordinates yet."})
            return

        start = (self.config.player_east, self.config.player_south)
        route_indices = nearest_neighbor_route(points, start)

        self._route_id_order = [valid_locs[ri].id for ri in route_indices]

        all_locs = self.store.get_all(area)
        self._route_mapped = []
        for ri in route_indices:
            target_id = valid_locs[ri].id
            for i, loc in enumerate(all_locs):
                if loc.id == target_id:
                    self._route_mapped.append(i)
                    break

        self.map_overlay.update_survey_data(all_locs, self._route_mapped)

        total_dist = 0.0
        prev = start
        for ri in route_indices:
            p = points[ri]
            total_dist += ((p[0] - prev[0]) ** 2 + (p[1] - prev[1]) ** 2) ** 0.5
            prev = p

        # Build slot labels and configure inventory overlay
        slot_labels: dict = {}
        first_slot = None
        for route_num, ri in enumerate(route_indices, start=1):
            loc = valid_locs[ri]
            slot = loc.inventory_slot
            if slot is not None:
                slot_labels[slot] = str(route_num)
                if first_slot is None:
                    first_slot = slot
        self._current_slot_labels = slot_labels

        self.inv_overlay.set_slot_labels(slot_labels, first_slot)
        inv = self.config.inventory
        self.inv_overlay.configure(
            inv.screen_x, inv.screen_y,
            inv.slot_width, inv.slot_height,
            inv.grid_cols, inv.grid_rows, inv.slot_gap,
            inv.padding_left, inv.padding_top,
        )
        self.inv_overlay.set_overlay_visible(True)

        self.click_watcher.configure(
            inv.screen_x, inv.screen_y,
            inv.slot_width, inv.slot_height,
            inv.grid_cols, inv.grid_rows, inv.slot_gap,
        )
        self.click_watcher.set_active_slots(set(slot_labels.keys()))
        self.click_watcher.start()

        slot_labels_str = {str(k): v for k, v in slot_labels.items()}
        await self.broadcast({
            "type": "route_calculated",
            "route_id_order": self._route_id_order,
            "route_distance": round(total_dist),
            "slot_labels": slot_labels_str,
            "locations": [_loc_to_dict(l) for l in all_locs],
            "status": f"Route: {len(route_indices)} stops, ~{total_dist:.0f} m",
        })

    # ------------------------------------------------------------------
    # Clear area
    # ------------------------------------------------------------------

    async def _clear_area(self):
        area = self.config.active_area
        self.store.clear_area(area)
        self._route_id_order = []
        self._route_mapped = []
        self._current_slot_labels = {}
        self._pending_visit_loc = None
        self.map_overlay.clear_circle_pins()
        self.map_overlay.update_survey_data([], [])
        self.map_overlay.set_visible(False)
        self.inv_overlay.set_overlay_visible(False)
        self.click_watcher.stop()
        await self._send_state_full()

    # ------------------------------------------------------------------
    # Player position & circle pin callbacks (from GameMapOverlay)
    # ------------------------------------------------------------------

    def _on_player_pos(self, arrow_px: int, arrow_py: int):
        # Avoid flooding browser if position hasn't changed
        if arrow_px == self._last_arrow_px and arrow_py == self._last_arrow_py:
            return
        self._last_arrow_px = arrow_px
        self._last_arrow_py = arrow_py

        # Compute game-meter position if calibrated
        pos = self.map_overlay.get_player_pos()
        east = round(pos[0], 1) if pos else None
        south = round(pos[1], 1) if pos else None

        asyncio.ensure_future(self.broadcast({
            "type": "player_pos",
            "pixel_x": arrow_px, "pixel_y": arrow_py,
            "east": east, "south": south,
        }))

    def _on_circle_pin(self, cx: int, cy: int):
        # Wake auto-use loop if it's waiting for the map to detect this pin
        if self._auto_use_pin_event and not self._auto_use_pin_event.is_set():
            self._auto_use_pin_event.set()
        asyncio.ensure_future(self.broadcast({
            "type": "circle_pin_added",
            "pixel_x": cx, "pixel_y": cy,
            "count": len(self.map_overlay._circle_pins),
        }))

    # ------------------------------------------------------------------
    # Region selection via native Qt overlay
    # ------------------------------------------------------------------

    def _launch_region_selector(self, purpose: str):
        """Show the fullscreen Qt region-selector overlay.  No browser round-trip needed."""
        labels = {
            "inventory":    "Drag to select the first inventory slot — Esc to cancel",
            "map":          "Drag to select the game map region — Esc to cancel",
            "safecracking": "Drag to select the 12-rune symbol grid — Esc to cancel",
        }
        selector = RegionSelector(labels.get(purpose, "Drag to select a region — Esc to cancel"))
        self._region_selector = selector  # keep Qt reference alive

        selector.region_selected.connect(
            lambda x, y, w, h: asyncio.ensure_future(self._apply_region(purpose, x, y, w, h))
        )
        selector.cancelled.connect(lambda: setattr(self, "_region_selector", None))
        selector.start_selection()

    async def _apply_region(self, purpose: str, x: int, y: int, w: int, h: int):
        """Save a region selection (screen-absolute coordinates) to config."""
        self._region_selector = None
        if purpose == "map":
            self.config.map_capture.x = x
            self.config.map_capture.y = y
            self.config.map_capture.w = w
            self.config.map_capture.h = h
            self.map_overlay.configure_region(x, y, w, h)
        elif purpose == "inventory":
            inv = self.config.inventory
            inv.screen_x = x
            inv.screen_y = y
            inv.slot_width = w
            inv.slot_height = h
        elif purpose == "safecracking":
            sc = self.config.safecracking_region
            sc.x, sc.y, sc.w, sc.h = x, y, w, h
            # Auto-capture symbols after region is set
            self.config.save()
            await self._send_state_full()
            await self._sc_capture()
            return
        self.config.save()
        await self._send_state_full()

    # ------------------------------------------------------------------
    # Config update from browser
    # ------------------------------------------------------------------

    async def _update_config(self, msg: dict):
        if "inventory" in msg:
            inv_data = msg["inventory"]
            inv = self.config.inventory
            for field in ("screen_x", "screen_y", "slot_width", "slot_height",
                          "grid_cols", "grid_rows", "slot_gap", "padding_left", "padding_top"):
                if field in inv_data:
                    setattr(inv, field, int(inv_data[field]))
            # Resize the overlay window to match the new grid dimensions
            if inv.screen_x and inv.screen_y:
                self.inv_overlay.configure(
                    inv.screen_x, inv.screen_y,
                    inv.slot_width, inv.slot_height,
                    inv.grid_cols, inv.grid_rows, inv.slot_gap,
                    inv.padding_left, inv.padding_top,
                )
        if "map_capture" in msg:
            mc_data = msg["map_capture"]
            mc = self.config.map_capture
            for field in ("x", "y", "w", "h"):
                if field in mc_data:
                    setattr(mc, field, int(mc_data[field]))
            if mc.w > 0:
                self.map_overlay.configure_region(mc.x, mc.y, mc.w, mc.h)
        if "chat_log_dir" in msg:
            self.config.chat_log_dir = msg["chat_log_dir"]
        if "player_east" in msg:
            self.config.player_east = float(msg["player_east"])
        if "player_south" in msg:
            self.config.player_south = float(msg["player_south"])
        if "auto_use_hotkey_vk" in msg:
            vk = int(msg["auto_use_hotkey_vk"])
            self.config.auto_use_hotkey_vk = vk
            if self._hotkey:
                self._hotkey.update_vk(vk)
            log.info("Auto-use hotkey changed to VK 0x%02X mods=0x%X",
                     vk, self.config.auto_use_hotkey_mods)
        if "auto_use_hotkey_mods" in msg:
            mods = int(msg["auto_use_hotkey_mods"])
            self.config.auto_use_hotkey_mods = mods
            if self._hotkey:
                self._hotkey.update_modifiers(mods)
            log.info("Auto-use hotkey mods changed to 0x%X", mods)
        if "single_use_hotkey_vk" in msg:
            vk = int(msg["single_use_hotkey_vk"])
            self.config.single_use_hotkey_vk = vk
            if self._single_use_hotkey:
                self._single_use_hotkey.update_vk(vk)
            log.info("Single-use hotkey changed to VK 0x%02X mods=0x%X",
                     vk, self.config.single_use_hotkey_mods)
        if "single_use_hotkey_mods" in msg:
            mods = int(msg["single_use_hotkey_mods"])
            self.config.single_use_hotkey_mods = mods
            if self._single_use_hotkey:
                self._single_use_hotkey.update_modifiers(mods)
            log.info("Single-use hotkey mods changed to 0x%X", mods)
        self.config.save()
        await self.broadcast({"type": "status", "message": "Settings saved"})

    # ------------------------------------------------------------------
    # Safecracking helpers
    # ------------------------------------------------------------------

    def _sc_build_state(self) -> dict:
        sc = self.config.safecracking_region
        solver_state = self._sc_solver.to_dict() if self._sc_solver else None
        return {
            "type": "sc_state",
            "region": {"x": sc.x, "y": sc.y, "w": sc.w, "h": sc.h},
            "symbols": self._sc_symbols,
            "solver": solver_state,
        }

    async def _sc_broadcast(self):
        await self.broadcast(self._sc_build_state())

    async def _sc_capture(self):
        """Capture the rune-grid region and extract 12 symbol thumbnails."""
        sc = self.config.safecracking_region
        if sc.w <= 0 or sc.h <= 0:
            await self.broadcast({"type": "error", "message": "Safecracking region not set — use Select Region first"})
            return
        log.info("SC capture  region=(%d,%d,%d,%d)", sc.x, sc.y, sc.w, sc.h)
        thumbs = capture_symbols(sc.x, sc.y, sc.w, sc.h)
        if not thumbs:
            await self.broadcast({"type": "error", "message": "Screen capture failed — check Pillow installation"})
            return
        self._sc_symbols = thumbs
        # Auto-init solver with all 12 symbols if not yet created
        if self._sc_solver is None:
            self._sc_solver = SafecrackingSolver(list(range(1, len(thumbs) + 1)))
        await self._sc_broadcast()

    async def _sc_reset(self, symbol_count: int):
        """Reset the solver for a new puzzle."""
        n = min(max(symbol_count, 2), len(self._sc_symbols) or 12)
        symbols = list(range(1, n + 1))
        if self._sc_solver is None:
            self._sc_solver = SafecrackingSolver(symbols)
        else:
            self._sc_solver.reset(symbols)
        log.info("SC reset  symbols=%s", symbols)
        await self._sc_broadcast()

    async def _sc_record(self, guess: List[int], exact: int, misplaced: int):
        """Record a guess result and update solver state."""
        if self._sc_solver is None:
            self._sc_solver = SafecrackingSolver(list(range(1, 13)))
        self._sc_solver.record(guess, exact, misplaced)
        log.info("SC record  guess=%s exact=%d misplaced=%d  candidates=%d",
                 guess, exact, misplaced, self._sc_solver.candidates_count)
        await self._sc_broadcast()

    async def _sc_undo(self):
        """Undo the last recorded guess."""
        if self._sc_solver and self._sc_solver.undo():
            log.info("SC undo  candidates=%d", self._sc_solver.candidates_count)
        await self._sc_broadcast()
