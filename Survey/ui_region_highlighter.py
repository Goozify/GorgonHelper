"""Transparent overlay that highlights a screen region with a gold border.

Used to preview / confirm inventory-slot and map-capture regions while
the user is editing the pixel-coordinate inputs in the browser.
"""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPainter, QColor, QPen
from PyQt5.QtWidgets import QWidget


_MARGIN = 4   # px of padding outside the region rect


class RegionHighlighter(QWidget):
    """Always-on-top transparent border drawn around a screen region.

    Mouse events pass through (WA_TransparentForMouseEvents) so the user
    can keep clicking in the game while the highlight is visible.
    The widget does NOT steal focus (WA_ShowWithoutActivating).
    """

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowStaysOnTopHint
            | Qt.FramelessWindowHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_region(self, x: int, y: int, w: int, h: int):
        """Position and show the highlight around (x, y, w, h) in screen coords."""
        if w <= 0 or h <= 0:
            return
        self.setGeometry(x - _MARGIN, y - _MARGIN, w + _MARGIN * 2, h + _MARGIN * 2)
        self.show()
        self.raise_()

    def hide_region(self):
        self.hide()

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        r = self.rect().adjusted(_MARGIN - 1, _MARGIN - 1, -(_MARGIN - 1), -(_MARGIN - 1))

        # Subtle fill so the region is visible even on dark backgrounds
        painter.fillRect(r, QColor(255, 215, 0, 28))

        # Gold border — two passes for a glow effect
        for width, alpha in ((5, 60), (2, 220)):
            painter.setPen(QPen(QColor(255, 215, 0, alpha), width))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(r)

        painter.end()
