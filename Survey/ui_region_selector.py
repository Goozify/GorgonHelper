"""Screen region selector that takes a screenshot as its background.
This approach is reliable on Windows without needing WA_TranslucentBackground."""
from typing import Optional

from PIL import ImageGrab

from PyQt5.QtCore import Qt, QRect, QPoint, pyqtSignal, QTimer
from PyQt5.QtGui import QPainter, QColor, QPen, QFont, QPixmap, QImage
from PyQt5.QtWidgets import QWidget, QApplication


class RegionSelector(QWidget):
    """Fullscreen selector that shows a frozen screenshot as background.
    The user drags a rectangle; on release the screen coordinates are emitted."""

    region_selected = pyqtSignal(int, int, int, int)   # screen x, y, w, h
    cancelled = pyqtSignal()

    def __init__(self, label: str = "Click and drag to select a region. Press Escape to cancel."):
        super().__init__(None)
        self._label = label
        self._start = QPoint()
        self._end = QPoint()
        self._selecting = False

        # Screenshot stored as QPixmap (widget-local resolution)
        self._bg: Optional[QPixmap] = None
        # Scale factors: screenshot physical px / widget logical px
        self._scale_x = 1.0
        self._scale_y = 1.0
        # Top-left of the virtual screen in screen coordinates
        self._offset_x = 0
        self._offset_y = 0

        self.setWindowFlags(
            Qt.Window
            | Qt.WindowStaysOnTopHint
            | Qt.FramelessWindowHint
        )
        # No WA_TranslucentBackground - we paint the screenshot ourselves
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_selection(self):
        """Call this to begin the selection flow (hides the caller first)."""
        QTimer.singleShot(250, self._grab_and_show)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _grab_and_show(self):
        # --- Compute virtual desktop bounds from all screens ---
        screens = QApplication.screens()
        if screens:
            from PyQt5.QtCore import QRect as _QRect
            combined = _QRect()
            for s in screens:
                combined = combined.united(s.geometry())
        else:
            combined = QApplication.primaryScreen().geometry()

        self._offset_x = combined.x()
        self._offset_y = combined.y()
        geo_w = combined.width()
        geo_h = combined.height()

        # --- Take screenshot with PIL ---
        try:
            screenshot = ImageGrab.grab(
                bbox=(combined.x(), combined.y(),
                      combined.x() + geo_w, combined.y() + geo_h),
                all_screens=True,
            )
        except TypeError:
            # Older Pillow without all_screens
            screenshot = ImageGrab.grab()
            geo_w = screenshot.width
            geo_h = screenshot.height

        # Convert PIL → QPixmap
        rgb = screenshot.convert("RGB")
        raw = rgb.tobytes("raw", "RGB")
        qimg = QImage(raw, rgb.width, rgb.height, rgb.width * 3, QImage.Format_RGB888)
        self._bg = QPixmap.fromImage(qimg)

        # Scale factors for DPI-aware displays (physical pixels vs logical pixels)
        self._scale_x = self._bg.width() / geo_w
        self._scale_y = self._bg.height() / geo_h

        self.setGeometry(self._offset_x, self._offset_y, geo_w, geo_h)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def _get_rect(self) -> QRect:
        return QRect(self._start, self._end).normalized()

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)

        # Background: screenshot (fills the whole window)
        if self._bg:
            painter.drawPixmap(0, 0, self.width(), self.height(), self._bg)
        else:
            painter.fillRect(self.rect(), QColor(40, 40, 40))

        # Dim everything
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))

        if self._selecting:
            rect = self._get_rect()

            # Reveal the selected area undimmed
            if self._bg:
                # Map logical rect → source rect in screenshot
                src = QRect(
                    int(rect.x() * self._scale_x),
                    int(rect.y() * self._scale_y),
                    max(1, int(rect.width() * self._scale_x)),
                    max(1, int(rect.height() * self._scale_y)),
                )
                painter.drawPixmap(rect, self._bg, src)

            # Yellow selection border
            painter.setPen(QPen(QColor(255, 215, 0), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect)

            # Size label
            lbl = f"  {rect.width()} × {rect.height()} px  "
            painter.setFont(QFont("Arial", 11, QFont.Bold))
            fm = painter.fontMetrics()
            tr = fm.boundingRect(lbl)
            bg_r = QRect(rect.x(), rect.y() - tr.height() - 10,
                         tr.width() + 12, tr.height() + 8)
            if bg_r.y() < 0:
                bg_r.moveTop(rect.bottom() + 4)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 200))
            painter.drawRect(bg_r)
            painter.setPen(QColor(255, 215, 0))
            painter.drawText(bg_r, Qt.AlignCenter, lbl)

        # Instruction banner (always on top)
        painter.setFont(QFont("Arial", 14, QFont.Bold))
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(self._label) + 48
        th = fm.height() + 18
        instr = QRect((self.width() - tw) // 2, 24, tw, th)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 210))
        painter.drawRoundedRect(instr, 8, 8)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(instr, Qt.AlignCenter, self._label)

        painter.end()

    # ------------------------------------------------------------------
    # Mouse / Keyboard
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._start = event.pos()
            self._end = event.pos()
            self._selecting = True
            self.update()

    def mouseMoveEvent(self, event):
        if self._selecting:
            self._end = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._selecting:
            self._selecting = False
            rect = self._get_rect()
            self.close()
            if rect.width() > 5 and rect.height() > 5:
                # Convert logical widget px → actual screen coordinates
                screen_x = int(rect.x() * self._scale_x) + self._offset_x
                screen_y = int(rect.y() * self._scale_y) + self._offset_y
                screen_w = int(rect.width() * self._scale_x)
                screen_h = int(rect.height() * self._scale_y)
                self.region_selected.emit(screen_x, screen_y, screen_w, screen_h)
            else:
                self.cancelled.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._selecting = False
            self.close()
            self.cancelled.emit()
