import json
import math
import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SURVEYS_PATH = os.path.join(DATA_DIR, "surveys.json")

DEDUP_THRESHOLD_M = 20  # meters - skip if new location is within this distance


@dataclass
class SurveyLocation:
    id: int = 0
    area: str = ""
    item_name: str = ""
    east_relative: int = 0
    south_relative: int = 0
    east_absolute: Optional[float] = None
    south_absolute: Optional[float] = None
    pixel_x: Optional[float] = None
    pixel_y: Optional[float] = None
    visited: bool = False
    inventory_slot: Optional[int] = None
    timestamp: str = ""


class SurveyStore:
    def __init__(self):
        self.locations: List[SurveyLocation] = []
        self._next_id = 1
        self._load()

    def _load(self):
        if not os.path.exists(SURVEYS_PATH):
            return
        try:
            with open(SURVEYS_PATH) as f:
                data = json.load(f)
            for item in data.get("locations", []):
                loc = SurveyLocation(**item)
                self.locations.append(loc)
                if loc.id >= self._next_id:
                    self._next_id = loc.id + 1
        except (json.JSONDecodeError, TypeError):
            pass

    def save(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        data = {"locations": [asdict(loc) for loc in self.locations]}
        with open(SURVEYS_PATH, "w") as f:
            json.dump(data, f, indent=2)

    def add(self, area: str, item_name: str, east_rel: int, south_rel: int,
            player_east: float = 0, player_south: float = 0,
            inventory_slot: Optional[int] = None) -> Optional[SurveyLocation]:
        """Add a survey location. Returns the location if added, None if duplicate."""
        east_abs = player_east + east_rel
        south_abs = player_south + south_rel

        # Dedup check against absolute coords in same area
        for existing in self.locations:
            if existing.area != area or existing.east_absolute is None:
                continue
            dx = existing.east_absolute - east_abs
            dy = existing.south_absolute - south_abs
            if math.sqrt(dx * dx + dy * dy) < DEDUP_THRESHOLD_M:
                return None

        from datetime import datetime
        loc = SurveyLocation(
            id=self._next_id,
            area=area,
            item_name=item_name,
            east_relative=east_rel,
            south_relative=south_rel,
            east_absolute=east_abs,
            south_absolute=south_abs,
            inventory_slot=inventory_slot,
            timestamp=datetime.now().isoformat(),
        )
        self._next_id += 1
        self.locations.append(loc)
        self.save()
        return loc

    def mark_visited(self, loc_id: int):
        for loc in self.locations:
            if loc.id == loc_id:
                loc.visited = True
                self.save()
                return

    def get_unvisited(self, area: str) -> List[SurveyLocation]:
        return [l for l in self.locations if l.area == area and not l.visited]

    def get_all(self, area: str) -> List[SurveyLocation]:
        return [l for l in self.locations if l.area == area]

    def get_by_id(self, loc_id: int) -> Optional[SurveyLocation]:
        for loc in self.locations:
            if loc.id == loc_id:
                return loc
        return None

    def update_slot(self, loc_id: int, new_slot: int):
        """Update inventory_slot for a location (after game shifts items)."""
        for loc in self.locations:
            if loc.id == loc_id:
                loc.inventory_slot = new_slot
                self.save()
                return

    def clear_area(self, area: str):
        self.locations = [l for l in self.locations if l.area != area]
        self.save()

    def clear_all(self):
        self.locations.clear()
        self._next_id = 1
        self.save()
