"""Safecracking puzzle solver for Project Gorgon.

Mastermind-variant: 4 slots, up to 12 symbols, repetition allowed.
Feedback: (exact_matches, misplaced_matches)

Symbols are referred to by 1-based index (1..N) matching the display order.
"""
import base64
import io
import logging
from collections import Counter
from itertools import product
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core scoring
# ---------------------------------------------------------------------------

def score_guess(guess: Tuple[int, ...], secret: Tuple[int, ...]) -> Tuple[int, int]:
    """Return (exact, misplaced) feedback for a guess vs. the secret."""
    exact = sum(g == s for g, s in zip(guess, secret))
    total = sum((Counter(guess) & Counter(secret)).values())
    return exact, total - exact


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------

class SafecrackingSolver:
    """Constraint-based solver tracking remaining consistent candidates.

    Strategy
    --------
    Phase 1 — AABB: test symbol pairs (s0,s0,s1,s1), (s2,s2,s3,s3), …
              until the accumulated (exact+misplaced) across AABB guesses
              reaches 4 (all solution symbols identified) or all pairs used.
    Phase 2 — Return the first remaining candidate (fine for the small search
              space: 6^4 = 1 296, 12^4 = 20 736 max).
    """

    SLOTS = 4

    def __init__(self, symbols: List[int]) -> None:
        self.symbols: List[int] = list(symbols)
        self._all: List[Tuple[int, ...]] = list(product(self.symbols, repeat=self.SLOTS))
        self._candidates: List[Tuple[int, ...]] = list(self._all)
        self._history: List[Tuple[List[int], int, int]] = []   # (guess, exact, misplaced)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def candidates_count(self) -> int:
        return len(self._candidates)

    @property
    def history(self) -> List[dict]:
        return [{"guess": list(g), "exact": e, "misplaced": m}
                for g, e, m in self._history]

    @property
    def solved(self) -> bool:
        return len(self._candidates) == 1

    @property
    def impossible(self) -> bool:
        return len(self._candidates) == 0

    @property
    def guess_count(self) -> int:
        return len(self._history)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def suggest(self) -> Optional[List[int]]:
        """Return next recommended guess as a list of symbol indices."""
        if self.impossible:
            return None
        if self.solved:
            return list(self._candidates[0])

        aabb = self._next_aabb()
        if aabb is not None:
            return list(aabb)

        return list(self._candidates[0])

    def record(self, guess: List[int], exact: int, misplaced: int) -> None:
        """Record feedback and prune candidates."""
        t = tuple(guess)
        self._history.append((guess, exact, misplaced))
        self._candidates = [
            c for c in self._candidates
            if score_guess(t, c) == (exact, misplaced)
        ]

    def undo(self) -> bool:
        """Remove the last recorded guess and recompute candidates."""
        if not self._history:
            return False
        self._history.pop()
        self._candidates = list(self._all)
        for guess, exact, misplaced in self._history:
            t = tuple(guess)
            self._candidates = [c for c in self._candidates
                                 if score_guess(t, c) == (exact, misplaced)]
        return True

    def reset(self, symbols: Optional[List[int]] = None) -> None:
        if symbols is not None:
            self.symbols = list(symbols)
            self._all = list(product(self.symbols, repeat=self.SLOTS))
        self._candidates = list(self._all)
        self._history = []

    def to_dict(self) -> dict:
        """Serialise state for broadcast."""
        suggestion = self.suggest()
        return {
            "symbols": self.symbols,
            "candidates": self.candidates_count,
            "suggestion": suggestion,
            "history": self.history,
            "solved": self.solved,
            "impossible": self.impossible,
            "guess_count": self.guess_count,
        }

    # ------------------------------------------------------------------
    # AABB phase helpers
    # ------------------------------------------------------------------

    def _next_aabb(self) -> Optional[Tuple[int, ...]]:
        """Return the next AABB pair to test, or None if AABB phase is done."""
        n = len(self.symbols)
        max_pairs = n // 2

        pairs_tested = 0
        total_hits = 0

        for guess, exact, misplaced in self._history:
            g = tuple(guess)
            if self._is_aabb(g):
                pairs_tested += 1
                total_hits += exact + misplaced
            else:
                return None   # non-AABB guess made — phase over

        if total_hits >= 4 or pairs_tested >= max_pairs:
            return None

        a = self.symbols[pairs_tested * 2]
        b = self.symbols[pairs_tested * 2 + 1]
        return (a, a, b, b)

    @staticmethod
    def _is_aabb(g: Tuple[int, ...]) -> bool:
        return len(g) == 4 and g[0] == g[1] and g[2] == g[3] and g[0] != g[2]


# ---------------------------------------------------------------------------
# Screen capture helpers
# ---------------------------------------------------------------------------

def capture_symbols(x: int, y: int, w: int, h: int,
                    cols: int = 6, rows: int = 2,
                    thumb: int = 52) -> List[str]:
    """Grab the rune-grid screen region and split into thumbnail PNGs (base64).

    Returns a flat list of ``cols * rows`` base64-encoded PNG strings,
    ordered left-to-right, top-to-bottom.
    """
    try:
        from PIL import ImageGrab, Image  # type: ignore
    except ImportError:
        log.error("Pillow not installed — cannot capture symbols")
        return []

    try:
        try:
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        except TypeError:
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h))

        cw = w // cols
        ch = h // rows
        result: List[str] = []

        for r in range(rows):
            for c in range(cols):
                left   = c * cw
                top    = r * ch
                right  = left + cw
                bottom = top  + ch
                cell = img.crop((left, top, right, bottom))
                cell = cell.resize((thumb, thumb), Image.LANCZOS)
                buf = io.BytesIO()
                cell.save(buf, format="PNG")
                result.append(base64.b64encode(buf.getvalue()).decode())

        return result

    except Exception as exc:
        log.warning("Symbol capture failed: %s", exc)
        return []
