"""Transparent overlay drawn directly over the in-game map window.

Shows survey location markers and the optimal route trail on top of the
game's own map image.  The player's position is tracked automatically by
periodically capturing the map region and detecting the white arrow.
"""
import ctypes
import time
from typing import List, Optional, Tuple

import numpy as np

from PyQt5.QtCore import Qt, QTimer, QRectF, QPointF, QPoint
from PyQt5.QtGui import QPainter, QColor, QPen, QBrush, QFont
from PyQt5.QtWidgets import QWidget, QApplication

_GWL_EXSTYLE       = -20
_WS_EX_LAYERED     = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020


def _find_red_circle(arr: np.ndarray) -> Optional[Tuple[int, int]]:
    """Detect the game's temporary red survey circle drawn on the map.

    Returns (cx, cy) in image-local pixels if found, else None.

    The game animates the circle from large -> small.  We only want the
    final, smallest size.  `spread` (sum of std-dev on each axis) is
    proportional to the circle radius -- roughly spread ~ R * 1.41.
    Rejecting spread > 25 ignores the larger animation frames while
    keeping the final circle (radius ~10-15 px -> spread ~14-21).
    """
    r = arr[:, :, 0].astype(np.int16)
    g = arr[:, :, 1].astype(np.int16)
    b = arr[:, :, 2].astype(np.int16)

    # Strong red: high R, clearly dominant over G and B.
    # Relaxed slightly for brownish terrain where the circle blends more.
    mask = (r > 140) & (g < 100) & (b < 100) & ((r - g) > 60) & ((r - b) > 60)
    ys, xs = np.where(mask)

    if len(xs) < 5:
        return None

    cx = float(xs.mean())
    cy = float(ys.mean())

    # spread ~ radius * 1.41 for a thin ring
    spread = float(np.std(xs) + np.std(ys))

    # Too small -> noise/dot;  too large -> animated (shrinking) frame
    if spread < 2 or spread > 25:
        return None

    return int(cx), int(cy)


def _apply_click_through(hwnd: int) -> None:
    try:
        user32 = ctypes.windll.user32
        h = int(hwnd)
        style = user32.GetWindowLongW(h, _GWL_EXSTYLE)
        user32.SetWindowLongW(h, _GWL_EXSTYLE,
                              style | _WS_EX_LAYERED | _WS_EX_TRANSPARENT)
        _SWP_NOMOVE = 0x0002
        _SWP_NOSIZE = 0x0001
        _SWP_NOZORDER = 0x0004
        _SWP_FRAMECHANGED = 0x0020
        user32.SetWindowPos(h, 0, 0, 0, 0, 0,
                            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_FRAMECHANGED)
    except Exception:
        pass

try:
    from PIL import ImageGrab
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

from survey_store import SurveyLocation
from player_tracker import find_player_arrow


class GameMapOverlay(QWidget):
    """Transparent, click-through overlay positioned over the in-game map."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowStaysOnTopHint
            | Qt.FramelessWindowHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        # Screen region of the in-game map
        self._map_x = 0
        self._map_y = 0
        self._map_w = 0
        self._map_h = 0

        # Survey data
        self._locations: List[SurveyLocation] = []
        self._route_order: List[int] = []  # indices into _locations

        # Current detected arrow position
        self._arrow_px: Optional[int] = None
        self._arrow_py: Optional[int] = None

        # Red circle pins detected from the game map  [(px, py, timestamp), ...]
        # Used ONLY for setup-mode crosshairs (visual feedback).
        self._circle_pins: List[Tuple[int, int, float]] = []

        # When True we're in setup mode (show crosshairs); False = route mode
        self._setup_active: bool = False
        self._paint_error: Optional[str] = None  # debug: last paint exception
        self._debug_tick = 0          # how many times _update_arrow ran
        self._debug_arrow_ok = 0      # how many times arrow was found
        self._debug_circle_checks = 0 # how many times circle detection ran
        self._debug_last_error: Optional[str] = None

        # Auto-calibrated transformation: pixel = offset + meters * scale
        # Computed from circle pin pixels + survey game coordinates.
        self._cal_scale: float = 0.0
        self._cal_offset_x: float = 0.0
        self._cal_offset_y: float = 0.0

        # Auto-capture timer
        self._timer = QTimer(self)
        self._timer.setInterval(600)
        self._timer.timeout.connect(self._update_arrow)

        # Optional callbacks for server.py to receive detections
        self._position_callback = None   # called with (arrow_px, arrow_py)
        self._pin_callback = None        # called with (cx, cy) when new pin added

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def configure_region(self, x: int, y: int, w: int, h: int):
        """Position the overlay exactly over the in-game map region."""
        self._map_x = x
        self._map_y = y
        self._map_w = w
        self._map_h = h
        self.setGeometry(x, y, w, h)

    def clear_circle_pins(self):
        """Remove all detected red-circle pins (call on new session)."""
        self._circle_pins.clear()
        self._cal_scale = 0.0
        self.update()

    def calibrate(self, locations: List[SurveyLocation]):
        """Auto-compute pixel<->meter transformation.

        Uses bounding-box matching between the detected circle pin pixel
        positions and the survey locations' game coordinates.  No need to
        know which pin corresponds to which location.
        """
        pins = self._circle_pins
        coords = [(l.east_absolute, l.south_absolute)
                   for l in locations if l.east_absolute is not None]

        if len(pins) < 2 or len(coords) < 2:
            return

        # Bounding boxes
        px_xs = [p[0] for p in pins]
        px_ys = [p[1] for p in pins]
        m_es  = [c[0] for c in coords]
        m_ss  = [c[1] for c in coords]

        px_range_x = max(px_xs) - min(px_xs)
        px_range_y = max(px_ys) - min(px_ys)
        m_range_e  = max(m_es) - min(m_es)
        m_range_s  = max(m_ss) - min(m_ss)

        # Compute scale from whichever axes have enough spread
        scales = []
        if m_range_e > 50:
            scales.append(px_range_x / m_range_e)
        if m_range_s > 50:
            scales.append(px_range_y / m_range_s)

        if not scales:
            return

        scale = sum(scales) / len(scales)

        # Offset from centroids
        px_cx = sum(px_xs) / len(px_xs)
        px_cy = sum(px_ys) / len(px_ys)
        m_ce  = sum(m_es)  / len(m_es)
        m_cs  = sum(m_ss)  / len(m_ss)

        self._cal_scale    = scale
        self._cal_offset_x = px_cx - m_ce * scale
        self._cal_offset_y = px_cy - m_cs * scale

    def get_player_pos(self):
        """Return current player (east, south) in game meters, or None."""
        if self._cal_scale <= 0 or self._arrow_px is None:
            return None
        east  = (self._arrow_px - self._cal_offset_x) / self._cal_scale
        south = (self._arrow_py - self._cal_offset_y) / self._cal_scale
        return east, south

    def update_survey_data(self, locations: List[SurveyLocation],
                           route_order: List[int]):
        self._locations = locations
        self._route_order = route_order
        self.repaint()

    def set_position_callback(self, cb):
        """Register callback(arrow_px, arrow_py) called every 600ms when arrow detected."""
        self._position_callback = cb

    def set_pin_callback(self, cb):
        """Register callback(cx, cy) called when a new red circle pin is added."""
        self._pin_callback = cb

    def set_visible(self, visible: bool):
        if visible:
            self._timer.start()
            self.show()
            _apply_click_through(self.winId())
        else:
            self._timer.stop()
            self.hide()

    # ------------------------------------------------------------------
    # Player tracking
    # ------------------------------------------------------------------

    def _update_arrow(self):
        self._debug_tick += 1
        if not _PIL_OK or self._map_w <= 0 or self._map_h <= 0:
            self._debug_last_error = f"skip: PIL={_PIL_OK} w={self._map_w} h={self._map_h}"
            return
        try:
            img = ImageGrab.grab(
                bbox=(self._map_x, self._map_y,
                      self._map_x + self._map_w,
                      self._map_y + self._map_h)
            )
            arr = np.array(img.convert("RGB"))

            result = find_player_arrow(arr)
            if result:
                self._arrow_px, self._arrow_py = result
                self._debug_arrow_ok += 1

            # Detect the game's temporary red survey circle (only during setup)
            if self._setup_active:
                self._debug_circle_checks += 1
                # Diagnostic: report actual max pixel values every 60 ticks
                if self._debug_circle_checks % 60 == 1:
                    r = arr[:, :, 0]
                    g = arr[:, :, 1]
                    b = arr[:, :, 2]
                    reddish = (r.astype(int) - g.astype(int) > 40)
                    self._debug_last_error = (
                        f"maxR={r.max()} maxG={g.max()} maxB={b.max()} "
                        f"redPx={(reddish).sum()} imgShape={arr.shape}"
                    )
                circle = _find_red_circle(arr)
                if circle:
                    cx, cy = circle
                    now = time.time()
                    is_dup = any(
                        abs(p[0] - cx) < 15 and abs(p[1] - cy) < 15
                        for p in self._circle_pins
                    )
                    if not is_dup:
                        self._circle_pins.append((cx, cy, now))
                        if self._pin_callback:
                            self._pin_callback(cx, cy)

            if result and self._position_callback:
                self._position_callback(self._arrow_px, self._arrow_py)

            self.update()

        except Exception as e:
            self._debug_last_error = str(e)

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def _loc_to_pixel(self, loc: SurveyLocation) -> Optional[Tuple[float, float]]:
        """Get overlay pixel position for a location.

        Prefers stored pixel_x/pixel_y (assigned directly from circle pin
        detection during setup) over computed positions from calibration.
        """
        # Direct pixel position from circle pin detection (most accurate)
        if loc.pixel_x is not None and loc.pixel_y is not None:
            return (loc.pixel_x, loc.pixel_y)
        # Fallback: compute from calibration
        if self._cal_scale <= 0 or loc.east_absolute is None:
            return None
        x = self._cal_offset_x + loc.east_absolute * self._cal_scale
        y = self._cal_offset_y + loc.south_absolute * self._cal_scale
        return (x, y)

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        try:
            self._do_paint(painter)
        except Exception as e:
            # Paint errors are silently swallowed by Qt — store for debugging
            self._paint_error = str(e)
        finally:
            painter.end()

    def _do_paint(self, painter: QPainter):
        if self._setup_active:
            # --- SETUP MODE: draw crosshairs at detected red-circle positions ---
            for cpx, cpy, _t in self._circle_pins:
                pt = QPointF(cpx, cpy)
                painter.setPen(QPen(QColor(255, 230, 0, 200), 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(pt, 8, 8)
                painter.drawLine(QPointF(cpx - 12, cpy), QPointF(cpx + 12, cpy))
                painter.drawLine(QPointF(cpx, cpy - 12), QPointF(cpx, cpy + 12))
        else:
            # --- ROUTE MODE: draw numbered circles from location coordinates ---
            loc_pixels = []
            for loc in self._locations:
                loc_pixels.append(self._loc_to_pixel(loc))

            # Unvisited location indices in route order
            active_route = [r for r in self._route_order
                            if r < len(self._locations)
                            and not self._locations[r].visited
                            and loc_pixels[r] is not None]

            # --- Route lines between stops ---
            if active_route:
                pen = QPen(QColor(70, 160, 255, 210), 3)
                pen.setStyle(Qt.DashLine)
                painter.setPen(pen)

                if self._arrow_px is not None:
                    fp = loc_pixels[active_route[0]]
                    painter.drawLine(
                        QPointF(self._arrow_px, self._arrow_py),
                        QPointF(fp[0], fp[1]),
                    )

                for i in range(len(active_route) - 1):
                    pa = loc_pixels[active_route[i]]
                    pb = loc_pixels[active_route[i + 1]]
                    painter.drawLine(
                        QPointF(pa[0], pa[1]),
                        QPointF(pb[0], pb[1]),
                    )

            # Build permanent route number lookup: route_order index -> route number
            route_num_by_idx = {}
            for route_pos, loc_idx in enumerate(self._route_order):
                route_num_by_idx[loc_idx] = route_pos + 1

            # --- Draw location markers ---
            for idx, loc in enumerate(self._locations):
                px_pos = loc_pixels[idx]
                if px_pos is None:
                    continue
                px, py = px_pos
                pt = QPointF(px, py)

                num = route_num_by_idx.get(idx, "?")

                if loc.visited:
                    r = 10
                    painter.setPen(QPen(QColor(60, 200, 60, 150), 2))
                    painter.setBrush(QBrush(QColor(60, 200, 60, 80)))
                    painter.drawEllipse(pt, r, r)
                    painter.setPen(QColor(30, 130, 30, 180))
                    painter.setFont(QFont("Arial", 8, QFont.Bold))
                    painter.drawText(
                        QRectF(px - r, py - r, r * 2, r * 2),
                        Qt.AlignCenter, str(num),
                    )
                else:
                    r = 12
                    painter.setPen(QPen(QColor(0, 0, 0, 200), 2))
                    painter.setBrush(QBrush(QColor(255, 180, 0, 220)))
                    painter.drawEllipse(pt, r, r)
                    painter.setPen(QColor(0, 0, 0))
                    painter.setFont(QFont("Arial", 9, QFont.Bold))
                    painter.drawText(
                        QRectF(px - r, py - r, r * 2, r * 2),
                        Qt.AlignCenter, str(num),
                    )

        # --- Player dot ---
        if self._arrow_px is not None:
            pp = QPointF(self._arrow_px, self._arrow_py)
            painter.setPen(QPen(QColor(0, 0, 0, 180), 2))
            painter.setBrush(QBrush(QColor(0, 200, 255, 200)))
            painter.drawEllipse(pp, 6, 6)
