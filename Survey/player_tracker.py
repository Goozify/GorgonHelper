"""Detects the player's position arrow on an in-game map screenshot.

The game renders the player arrow as a solid CYAN / turquoise triangle.
Depending on the map the arrow may also have a dark drop-shadow beneath it.
Two detection strategies are combined:

  1. Cyan-hue + saturation  (primary — works at any zoom level)
     The arrow is the only vivid cyan/turquoise object on the map.  Detected
     by a tight hue band (actual ~150-200°) plus a saturation floor.
     Hue band is deliberately narrow to exclude the blue overlay route-lines
     (actual ~210°) and the orange/green location markers (~40° / ~120°).
     Minimum area is very low (10 px²) because the arrow may be tiny when the
     map is zoomed far out, but the hue filter is selective enough to tolerate
     it.

  2. Local range in grayscale  (secondary — catches shadowed arrows)
     Computes local_max − local_min over a ~13 px neighbourhood.  The dark
     drop-shadow adjacent to the map background creates a sharp edge that
     scores ~150-200; uniform snow ~10-30.  Minimum area is slightly relaxed
     here too to handle small zoomed-out arrows.

Both methods share size/shape/proximity filters.  Scores are
area × mean_channel_value so they are directly comparable.  The
higher-scoring result is returned.

Public API
----------
find_player_arrow(img_rgb, exclude_positions=None) -> Optional[(x, y)]
"""
from typing import List, Optional, Tuple

import numpy as np

try:
    import cv2
    _CV2_OK = True
except ImportError:
    _CV2_OK = False


# ── Shared size / shape constants ─────────────────────────────────────────────

_MAX_AREA   = 800     # px² — upper bound (arrow at any zoom ≪ 800)
_MAX_DIM    = 90      # largest bounding-box side, px
_MIN_FILL   = 0.10    # area / bbox area — allows partial detections at small size
_MAX_ASPECT = 3.5     # max(bw,bh) / min(bw,bh) — rejects elongated text / lines

# Proximity exclusion: skip candidates centred within this radius of a known
# overlay-marker position.  Must exceed marker_radius(12) + half_kernel(7).
_EXCLUDE_RADIUS    = 32
_EXCLUDE_RADIUS_SQ = _EXCLUDE_RADIUS * _EXCLUDE_RADIUS

# ── Cyan / saturation detector constants ─────────────────────────────────────

# Hue range for the cyan arrow (OpenCV 0-180 scale, where each unit = 2°).
#   Arrow:       actual ~170-200° → OpenCV  85-100
#   Route lines: actual ~210°     → OpenCV ~105   ← kept OUT by _CYAN_H_HI=103
#   Orange pins: actual ~40°      → OpenCV  ~20   ← well outside
#   Green pins:  actual ~120°     → OpenCV  ~60   ← well outside
_CYAN_H_LO = 75
_CYAN_H_HI = 103

# Minimum saturation (0-255).  Arrow ≈ 190-255; terrain < 40; blue route ≈ 155.
_CYAN_S_MIN = 80

# Minimum value — exclude very dark saturated pixels (navy ocean, dark forest).
_CYAN_V_MIN = 60

# Lower minimum area because the hue gate is already very selective:
# even a 5-pixel-wide arrow passes S / V / H checks, so we trust the match.
_CYAN_MIN_AREA = 10

# ── Local-range detector constants ───────────────────────────────────────────

# Kernel size for local-range computation (must be odd).
_RANGE_KERNEL = 13

# Pixels with local_max − local_min above this are high-contrast edges.
# Arrow shadow: ~150-200.  Snow: ~10-30.
_RANGE_THRESH = 115

# Relaxed from 30 → 18 so a small zoomed-out shadowed arrow still passes.
_RANGE_MIN_AREA = 18


# ── Shared helpers ────────────────────────────────────────────────────────────

def _passes_shape(area: int, bw: int, bh: int, min_area: int) -> bool:
    if area < min_area or area > _MAX_AREA:
        return False
    if bw > _MAX_DIM or bh > _MAX_DIM:
        return False
    if bw * bh > 0 and (area / (bw * bh)) < _MIN_FILL:
        return False
    if max(bw, bh) / max(min(bw, bh), 1) > _MAX_ASPECT:
        return False
    return True


def _excluded(cx: int, cy: int, positions) -> bool:
    if not positions:
        return False
    for ex, ey in positions:
        dx, dy = cx - ex, cy - ey
        if dx * dx + dy * dy < _EXCLUDE_RADIUS_SQ:
            return True
    return False


def _best_blob(
    mask: np.ndarray,
    score_img: np.ndarray,
    exclude_positions,
    min_area: int,
) -> Tuple[Optional[Tuple[int, int]], float]:
    """Return ((cx, cy), score) of the best candidate, or (None, -1)."""
    n, _lbl, stats, centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )
    best_xy    = None
    best_score = -1.0

    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        bw   = int(stats[i, cv2.CC_STAT_WIDTH])
        bh   = int(stats[i, cv2.CC_STAT_HEIGHT])

        if not _passes_shape(area, bw, bh, min_area):
            continue

        cx = int(centroids[i][0])
        cy = int(centroids[i][1])

        if _excluded(cx, cy, exclude_positions):
            continue

        x0 = int(stats[i, cv2.CC_STAT_LEFT])
        y0 = int(stats[i, cv2.CC_STAT_TOP])
        roi   = score_img[y0:y0 + bh, x0:x0 + bw]
        score = area * float(roi.mean())

        if score > best_score:
            best_score = score
            best_xy    = (cx, cy)

    return best_xy, best_score


# ── Detection method 1: cyan hue + saturation ─────────────────────────────────

def _detect_cyan(
    img_rgb: np.ndarray,
    exclude_positions,
) -> Tuple[Optional[Tuple[int, int]], float]:
    """Find the cyan arrow by hue/saturation — works at any zoom level."""
    hsv = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]

    mask = (
        (h >= _CYAN_H_LO) & (h <= _CYAN_H_HI) &
        (s >= _CYAN_S_MIN) &
        (v >= _CYAN_V_MIN)
    ).astype(np.uint8) * 255

    # Remove isolated single-pixel noise; close tiny gaps in the arrow fill.
    open_k  = np.ones((2, 2), np.uint8)
    close_k = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  open_k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_k)

    # Score by mean saturation within each blob bbox
    return _best_blob(mask, s, exclude_positions, _CYAN_MIN_AREA)


# ── Detection method 2: grayscale local range ─────────────────────────────────

def _detect_local_range(
    img_rgb: np.ndarray,
    exclude_positions,
) -> Tuple[Optional[Tuple[int, int]], float]:
    """Find the arrow's drop-shadow / outline via local contrast."""
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)

    k = _RANGE_KERNEL
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    local_max = cv2.dilate(gray, kernel)
    local_min = cv2.erode(gray, kernel)
    local_range = np.clip(
        local_max.astype(np.int16) - local_min.astype(np.int16),
        0, 255,
    ).astype(np.uint8)

    _, mask = cv2.threshold(local_range, _RANGE_THRESH, 255, cv2.THRESH_BINARY)
    close_k = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_k)

    return _best_blob(mask, local_range, exclude_positions, _RANGE_MIN_AREA)


# ── Public API ────────────────────────────────────────────────────────────────

def find_player_arrow(
    img_rgb: np.ndarray,
    exclude_positions: Optional[List[Tuple[int, int]]] = None,
) -> Optional[Tuple[int, int]]:
    """Locate the player arrow in an RGB map screenshot.

    Parameters
    ----------
    img_rgb
        H × W × 3 uint8 RGB array (the captured map region).
    exclude_positions
        Optional list of (x, y) pixel positions to skip during detection.
        Pass known overlay-marker positions so their coloured circles are
        not mistaken for the player arrow.

    Returns
    -------
    (x, y) in image-local pixel coordinates, or None if not found.
    """
    if not _CV2_OK or img_rgb is None or img_rgb.size == 0:
        return None

    # Cyan detection is very selective (narrow hue band + saturation floor).
    # When it fires, the probability of a false positive is very low, so we
    # prefer it unconditionally over the less-selective local-range detector.
    # Only fall back to local range when the cyan method finds nothing at all
    # (e.g. a non-cyan map where the arrow has a prominent dark shadow).
    xy_cyan, _score_cyan = _detect_cyan(img_rgb, exclude_positions)
    if xy_cyan:
        return xy_cyan

    xy_range, _score_range = _detect_local_range(img_rgb, exclude_positions)
    return xy_range
