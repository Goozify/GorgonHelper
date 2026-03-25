"""Low-level mouse hook that detects double-clicks on inventory slots.

The hook is non-blocking: all mouse events are passed through to the game.
We just observe WM_LBUTTONDOWN events and detect double-click timing ourselves.
"""
import ctypes
import ctypes.wintypes as wt
import time
from ctypes import CFUNCTYPE, c_int, POINTER

from PyQt5.QtCore import QObject, pyqtSignal, QTimer

WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wt.POINT),
        ("mouseData", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


HOOKPROC = CFUNCTYPE(c_int, c_int, wt.WPARAM, POINTER(MSLLHOOKSTRUCT))


class InventoryClickWatcher(QObject):
    """Watches for double-clicks on labeled inventory slots via a global mouse hook."""

    double_clicked_slot = pyqtSignal(int)  # emits the slot index

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hook = None
        self._hook_proc_ref = None  # prevent garbage collection

        self._last_click_time = 0.0
        self._last_click_pos = (0, 0)
        self._dbl_click_ms = ctypes.windll.user32.GetDoubleClickTime()

        # Inventory geometry (screen coords of first slot)
        self._inv_x = 0
        self._inv_y = 0
        self._slot_w = 0
        self._slot_h = 0
        self._cols = 0
        self._rows = 0
        self._gap = 0
        self._active_slots: set = set()  # slot indices that have labels

        # Deferred signal emission (keep hook callback fast)
        self._pending_slot = -1

    def configure(self, x: int, y: int, slot_w: int, slot_h: int,
                  cols: int, rows: int, gap: int):
        self._inv_x = x
        self._inv_y = y
        self._slot_w = slot_w
        self._slot_h = slot_h
        self._cols = cols
        self._rows = rows
        self._gap = gap

    def set_active_slots(self, slots: set):
        """Set which slot indices should trigger on double-click."""
        self._active_slots = slots

    def start(self):
        if self._hook:
            return

        def _low_level_proc(nCode, wParam, lParam):
            if nCode >= 0 and wParam == WM_LBUTTONDOWN:
                try:
                    pt = lParam.contents.pt
                    self._on_click(pt.x, pt.y)
                except Exception:
                    pass
            return ctypes.windll.user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._hook_proc_ref = HOOKPROC(_low_level_proc)
        self._hook = ctypes.windll.user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._hook_proc_ref, None, 0
        )

    def stop(self):
        if self._hook:
            ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
            self._hook_proc_ref = None

    # ------------------------------------------------------------------

    def _on_click(self, screen_x: int, screen_y: int):
        slot = self._screen_to_slot(screen_x, screen_y)
        if slot is None or slot not in self._active_slots:
            # Reset tracking if click was outside inventory
            self._last_click_time = 0
            return

        now = time.time() * 1000  # ms
        dx = abs(screen_x - self._last_click_pos[0])
        dy = abs(screen_y - self._last_click_pos[1])
        dt = now - self._last_click_time

        if dt < self._dbl_click_ms and dx < 10 and dy < 10:
            # Double-click detected — defer signal emission
            self._pending_slot = slot
            QTimer.singleShot(0, self._emit_pending)
            self._last_click_time = 0
        else:
            self._last_click_time = now
            self._last_click_pos = (screen_x, screen_y)

    def _emit_pending(self):
        if self._pending_slot >= 0:
            self.double_clicked_slot.emit(self._pending_slot)
            self._pending_slot = -1

    def _screen_to_slot(self, sx: int, sy: int):
        rx = sx - self._inv_x
        ry = sy - self._inv_y
        if rx < 0 or ry < 0:
            return None

        step_x = self._slot_w + self._gap
        step_y = self._slot_h + self._gap
        if step_x <= 0 or step_y <= 0:
            return None

        col = rx // step_x
        row = ry // step_y

        # Check we're inside a slot, not in the gap
        if rx % step_x >= self._slot_w or ry % step_y >= self._slot_h:
            return None

        if col >= self._cols or row >= self._rows:
            return None

        return int(row * self._cols + col)
