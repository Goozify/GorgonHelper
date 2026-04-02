"""Safecracking overlay — highlights suggested symbols in the game window.

Shows numbered golden circles over each symbol position for the suggested guess,
cycling through them one by one so the player knows which rune to click next.

The overlay is fully transparent to mouse/keyboard input so clicks pass through
to the game beneath it.
"""
from typing import List, Optional, Tuple

from PyQt5.QtCore import Qt, QTimer, QRect, QPoint
from PyQt5.QtGui import QPainter, QColor, QPen, QFont, QFontMetrics
from PyQt5.QtWidgets import QWidget, QApplication


class SafecrackingOverlay(QWidget):
    """Full-screen transparent overlay that cycles through suggested symbol positions."""

    # Timing
    CYCLE_MS = 2500      # how long each symbol is highlighted (ms)
    FLASH_MS = 120       # brief flash at transition

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput   # click-through on Windows
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        # Positions: list of (screen_x, screen_y) — one entry per guess slot
        self._positions: List[Tuple[int, int]] = []
        # Which symbol index (1-based, into the known 12) each slot uses
        self._symbol_indices: List[int] = []
        self._current: int = 0          # which slot is currently highlighted
        self._flash: bool = False       # brief white flash on transition

        self._cycle_timer = QTimer(self)
        self._cycle_timer.timeout.connect(self._advance)

        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_suggestion(self, positions: List[Tuple[int, int]],
                        symbol_indices: List[int]) -> None:
        """Start cycling through the suggested symbol positions.

        positions      — screen (x, y) centre of each slot's symbol
        symbol_indices — 1-based symbol numbers (for the label)
        """
        if not positions:
            self.hide_overlay()
            return

        self._positions = list(positions)
        self._symbol_indices = list(symbol_indices)
        self._current = 0
        self._flash = False

        # Cover the full virtual desktop so we can draw anywhere
        screens = QApplication.screens()
        if screens:
            from PyQt5.QtCore import QRect as _QR
            combined = _QR()
            for s in screens:
                combined = combined.united(s.geometry())
        else:
            combined = QApplication.primaryScreen().geometry()
        self.setGeometry(combined)

        self.show()
        self.raise_()
        self._cycle_timer.start(self.CYCLE_MS)
        self.update()

    def hide_overlay(self) -> None:
        self._cycle_timer.stop()
        self._flash_timer.stop()
        self.hide()

    def advance(self) -> None:
        """Manually advance to the next slot."""
        self._advance()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _advance(self) -> None:
        self._current = (self._current + 1) % len(self._positions)
        # Brief flash to signal transition
        self._flash = True
        self.update()
        self._flash_timer.start(self.FLASH_MS)

    def _end_flash(self) -> None:
        self._flash = False
        self.update()

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        if not self._positions:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        idx = self._current
        x, y = self._positions[idx]
        sym_num = self._symbol_indices[idx] if idx < len(self._symbol_indices) else idx + 1
        slot_label = f"{idx + 1}/{len(self._positions)}"

        gold      = QColor(255, 215,   0)
        gold_dim  = QColor(255, 215,   0, 80)
        white     = QColor(255, 255, 255)
        flash_col = QColor(255, 255, 255, 180) if self._flash else None

        R = 44   # circle radius

        # --- Outer glow ring ---
        painter.setPen(QPen(gold_dim, 10))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPoint(x, y), R + 8, R + 8)

        # --- Main circle ---
        border_col = white if self._flash else gold
        painter.setPen(QPen(border_col, 3))
        if flash_col:
            painter.setBrush(QColor(255, 255, 255, 40))
        else:
            painter.setBrush(QColor(0, 0, 0, 80))
        painter.drawEllipse(QPoint(x, y), R, R)

        # --- Slot number inside circle ---
        font = QFont("Arial", 20, QFont.Bold)
        painter.setFont(font)
        painter.setPen(border_col)
        painter.drawText(
            QRect(x - R, y - R, R * 2, R * 2),
            Qt.AlignCenter,
            str(idx + 1)
        )

        # --- "Symbol N" label above circle ---
        small_font = QFont("Arial", 11, QFont.Bold)
        painter.setFont(small_font)
        fm = QFontMetrics(small_font)
        lbl = f"Symbol {sym_num}  ·  {slot_label}"
        tw = fm.horizontalAdvance(lbl) + 20
        th = fm.height() + 10
        lx = x - tw // 2
        ly = y - R - th - 6

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 190))
        painter.drawRoundedRect(lx, ly, tw, th, 5, 5)
        painter.setPen(gold)
        painter.drawText(QRect(lx, ly, tw, th), Qt.AlignCenter, lbl)

        # --- Dim dots for remaining positions ---
        for i, (px, py) in enumerate(self._positions):
            if i == idx:
                continue
            painter.setPen(QPen(QColor(255, 215, 0, 50), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPoint(px, py), 20, 20)
            painter.setFont(QFont("Arial", 9))
            painter.setPen(QColor(255, 215, 0, 80))
            painter.drawText(
                QRect(px - 20, py - 20, 40, 40),
                Qt.AlignCenter,
                str(i + 1)
            )

        painter.end()
