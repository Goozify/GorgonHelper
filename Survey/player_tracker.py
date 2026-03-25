"""Detects the player's position (bright white arrow) on an in-game map screenshot."""
from typing import Optional, Tuple

import numpy as np

try:
    import cv2
    _CV2_OK = True
except ImportError:
    _CV2_OK = False


def find_player_arrow(img_rgb: np.ndarray) -> Optional[Tuple[int, int]]:
    """Locate the player arrow (bright white shape) in an RGB screenshot.

    The in-game map arrow is the brightest white element on screen.
    Returns (x, y) in image-local pixel coordinates, or None if not found.
    """
    if not _CV2_OK or img_rgb is None or img_rgb.size == 0:
        return None

    # --- Build a mask of near-white pixels ---
    # All three channels must be very bright (≥230)
    r = img_rgb[:, :, 0].astype(np.uint16)
    g = img_rgb[:, :, 1].astype(np.uint16)
    b = img_rgb[:, :, 2].astype(np.uint16)
    mask = ((r >= 230) & (g >= 230) & (b >= 230)).astype(np.uint8) * 255

    # Remove single-pixel noise
    kernel = np.ones((2, 2), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    # Connected components
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )
    if num_labels <= 1:
        return None

    best_centroid = None
    best_score = -1

    for i in range(1, num_labels):
        area = int(stats[i, cv2.CC_STAT_AREA])
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])

        # The arrow is small (roughly 10–80 px²), roughly square bounding box
        if area < 15 or area > 500:
            continue
        if w > 70 or h > 70:
            continue
        # Prefer larger blobs (more arrow-like) over tiny noise
        if area > best_score:
            best_score = area
            best_centroid = centroids[i]

    if best_centroid is None:
        return None

    return (int(best_centroid[0]), int(best_centroid[1]))
