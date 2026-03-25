import ctypes
import ctypes.wintypes
from typing import Optional

from PyQt5.QtCore import Qt, QTimer, QRectF
from PyQt5.QtGui import QPainter, QColor, QPen, QFont, QBrush
from PyQt5.QtWidgets import QWidget

# Win32 constants for making a window truly click-through
_GWL_EXSTYLE      = -20
_WS_EX_LAYERED    = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020


def _apply_click_through(hwnd: int) -> None:
    """Set WS_EX_LAYERED | WS_EX_TRANSPARENT on a Win32 window handle so that
    all mouse events fall through to whatever is beneath the window."""
    try:
        user32 = ctypes.windll.user32
        h = int(hwnd)
        style = user32.GetWindowLongW(h, _GWL_EXSTYLE)
        user32.SetWindowLongW(h, _GWL_EXSTYLE,
                              style | _WS_EX_LAYERED | _WS_EX_TRANSPARENT)
        _SWP_NOMOVE      = 0x0002
        _SWP_NOSIZE      = 0x0001
        _SWP_NOZORDER    = 0x0004
        _SWP_FRAMECHANGED = 0x0020
        user32.SetWindowPos(h, 0, 0, 0, 0, 0,
                            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_FRAMECHANGED)
    except Exception:
        pass


class InventoryOverlay(QWidget):
    """Transparent overlay that highlights the next inventory slot to scan.

    The overlay is positioned to start at the FIRST SLOT, not the top-left
    of the whole inventory window.  Use padding_left / padding_top to push
    past any title-bar or border the inventory window has.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowStaysOnTopHint
            | Qt.FramelessWindowHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        self._slot_width = 48
        self._slot_height = 48
        self._grid_cols = 10
        self._grid_rows = 5
        self._slot_gap = 2
        self._current_slot = 0     # 0-indexed, row-major (setup mode)
        self._total_slots = 50
        self._visible = False
        self._slot_labels: dict = {}  # slot_index -> route number string
        self._highlight_slot: Optional[int] = None  # next unvisited slot to highlight

        # Blink animation
        self._blink_on = True
        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._toggle_blink)
        self._blink_timer.start(500)

    def configure(self, screen_x: int, screen_y: int,
                  slot_width: int, slot_height: int,
                  grid_cols: int, grid_rows: int, slot_gap: int,
                  padding_left: int = 0, padding_top: int = 0):
        self._slot_width = slot_width
        self._slot_height = slot_height
        self._grid_cols = grid_cols
        self._grid_rows = grid_rows
        self._slot_gap = slot_gap
        self._total_slots = grid_cols * grid_rows

        total_w = grid_cols * (slot_width + slot_gap)
        total_h = grid_rows * (slot_height + slot_gap)

        self.setGeometry(
            screen_x + padding_left,
            screen_y + padding_top,
            total_w,
            total_h,
        )

    def set_current_slot(self, slot_index: int):
        self._current_slot = slot_index
        self.update()

    def set_slot_labels(self, labels: dict,
                        highlight_slot: Optional[int] = None):
        """Set route number labels for slots.

        labels: {slot_index: "1", ...}
        highlight_slot: slot index of the next unvisited survey to highlight
        """
        self._slot_labels = labels
        self._highlight_slot = highlight_slot
        self.update()

    def advance_slot(self):
        self._current_slot = min(self._current_slot + 1, self._total_slots - 1)
        self.update()

    def _toggle_blink(self):
        self._blink_on = not self._blink_on
        if self._visible:
            self.update()

    def set_overlay_visible(self, visible: bool):
        self._visible = visible
        if visible:
            self.show()
            QTimer.singleShot(80, lambda: _apply_click_through(self.winId()))
        else:
            self.hide()

    def _slot_rect(self, slot_idx: int):
        """Return (x, y) top-left of a slot in overlay coords."""
        sr = slot_idx // self._grid_cols
        sc = slot_idx % self._grid_cols
        sx = sc * (self._slot_width + self._slot_gap)
        sy = sr * (self._slot_height + self._slot_gap)
        return sx, sy

    def paintEvent(self, event):
        if not self._visible:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if self._slot_labels:
            # --- Route mode ---

            # Light highlight on the next slot to visit
            if self._highlight_slot is not None and self._highlight_slot in self._slot_labels:
                sx, sy = self._slot_rect(self._highlight_slot)
                if self._blink_on:
                    painter.setPen(QPen(QColor(100, 255, 100), 2))
                    painter.setBrush(QColor(100, 255, 100, 35))
                else:
                    painter.setPen(QPen(QColor(100, 255, 100, 120), 2))
                    painter.setBrush(QColor(100, 255, 100, 15))
                painter.drawRect(sx, sy, self._slot_width, self._slot_height)

            # Number badges on each labeled slot
            for slot_idx, label in self._slot_labels.items():
                sx, sy = self._slot_rect(slot_idx)
                badge_size = min(20, self._slot_width // 2)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(QColor(255, 180, 0, 220)))
                painter.drawRoundedRect(sx + 2, sy + 2, badge_size, badge_size, 3, 3)
                painter.setPen(QColor(0, 0, 0))
                painter.setFont(QFont("Arial", 9, QFont.Bold))
                painter.drawText(
                    QRectF(sx + 2, sy + 2, badge_size, badge_size),
                    Qt.AlignCenter, str(label),
                )
        else:
            # --- Setup mode: highlight scanned and current slots ---
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 200, 0, 30))
            for s in range(self._current_slot):
                sx, sy = self._slot_rect(s)
                painter.drawRect(sx, sy, self._slot_width, self._slot_height)

            # Current slot: bright blinking highlight
            sx, sy = self._slot_rect(self._current_slot)
            if self._blink_on:
                painter.setPen(QPen(QColor(255, 255, 0), 3))
                painter.setBrush(QColor(255, 255, 0, 45))
            else:
                painter.setPen(QPen(QColor(255, 255, 0, 110), 2))
                painter.setBrush(QColor(255, 255, 0, 15))
            painter.drawRect(sx, sy, self._slot_width, self._slot_height)

        painter.end()
