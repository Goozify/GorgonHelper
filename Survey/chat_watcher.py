import os
import re
import glob
from PyQt5.QtCore import QThread, pyqtSignal


# Matches: [Status] <item> is <dist>m east|west and <dist>m south|north.
COORD_RE = re.compile(
    r"\[Status\] .+ is (\d+)m (east|west) and (\d+)m (south|north)\."
)

# Also capture the item name for storage
FULL_COORD_RE = re.compile(
    r"\[Status\] (.+?) is (\d+)m (east|west) and (\d+)m (south|north)\."
)

# Area change
AREA_RE = re.compile(r"\*+ Entering Area: (.+)")

# Survey loot: "[Status] X collected!" or "X x3 collected!" etc.
COLLECTED_RE = re.compile(r"\[Status\] (.+?)(?:\s+x(\d+))? collected!")

# Item added to inventory: "[Status] X added to inventory." or "X x2 added to inventory."
ADDED_RE = re.compile(r"\[Status\] (.+?)(?:\s+x(\d+))? added to inventory\.")


def find_newest_log(log_dir: str) -> str | None:
    pattern = os.path.join(log_dir, "Chat-*.log")
    files = glob.glob(pattern)
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def parse_status_line(line: str):
    """Parse a chat-log line.
    Returns (item_name, east_meters, south_meters) or None.
    East and south are positive; west and north are negative.
    """
    m = FULL_COORD_RE.search(line)
    if not m:
        return None
    item_name = m.group(1)
    dist1 = int(m.group(2))
    dir1  = m.group(3)
    dist2 = int(m.group(4))
    dir2  = m.group(5)
    east  = dist1 if dir1 == "east"  else -dist1
    south = dist2 if dir2 == "south" else -dist2
    return item_name, east, south


def parse_area_line(line: str) -> str | None:
    m = AREA_RE.search(line)
    return m.group(1) if m else None


class ChatWatcher(QThread):
    """Watches the newest chat log for survey coordinate lines.

    Parameters
    ----------
    log_dir : str
        Directory containing Chat-*.log files.
    skip_existing : bool
        If True (default), seek to the END of the file before watching so
        that old entries are ignored.  Set False only for debugging.
    """

    survey_detected  = pyqtSignal(str, int, int)   # item_name, east, south
    survey_completed = pyqtSignal(str)             # item_name → survey map collected
    loot_received    = pyqtSignal(str, int)        # item_name, quantity → any item collected
    area_changed     = pyqtSignal(str)             # area_name
    error_occurred   = pyqtSignal(str)             # error message

    def __init__(self, log_dir: str, skip_existing: bool = True, parent=None):
        super().__init__(parent)
        self.log_dir = log_dir
        self.skip_existing = skip_existing
        self._running = False

    def run(self):
        self._running = True
        log_path = find_newest_log(self.log_dir)
        if not log_path:
            self.error_occurred.emit(
                f"No chat log files found in:\n{self.log_dir}"
            )
            return

        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                if self.skip_existing:
                    # Jump to end so we only see new lines written after this point
                    f.seek(0, 2)
                else:
                    # Process existing lines (useful for replay/debug)
                    for line in f:
                        self._process_line(line)

                # Tail: read new lines as they appear
                while self._running:
                    line = f.readline()
                    if line:
                        self._process_line(line)
                    else:
                        self.msleep(300)
        except Exception as exc:
            self.error_occurred.emit(str(exc))

    def stop(self):
        self._running = False

    def _process_line(self, line: str):
        line = line.strip()
        if not line:
            return

        area = parse_area_line(line)
        if area:
            self.area_changed.emit(area)
            return

        result = parse_status_line(line)
        if result:
            item_name, east, south = result
            self.survey_detected.emit(item_name, east, south)
            return

        m = COLLECTED_RE.search(line)
        if m:
            name = m.group(1)
            qty  = int(m.group(2)) if m.group(2) else 1
            self.survey_completed.emit(name)
            self.loot_received.emit(name, qty)
            return

        m = ADDED_RE.search(line)
        if m:
            name = m.group(1)
            # Only emit survey_completed — "added to inventory" covers survey map
            # consumption but also every random pickup (foraging, bags, etc.),
            # so we deliberately do NOT emit loot_received here.
            self.survey_completed.emit(name)
