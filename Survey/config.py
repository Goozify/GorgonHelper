import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

DEFAULT_CHAT_LOG_DIR = r"C:\Users\Janne\AppData\LocalLow\Elder Game\Project Gorgon\ChatLogs"


@dataclass
class ScreenRect:
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0


@dataclass
class InventorySettings:
    screen_x: int = 0
    screen_y: int = 0
    slot_width: int = 48
    slot_height: int = 48
    grid_cols: int = 10
    grid_rows: int = 5
    slot_gap: int = 2
    # Pixel offset from the selected region's top-left corner to the first slot.
    # Use these to compensate for the title bar / border of the inventory window.
    padding_left: int = 0
    padding_top: int = 0


@dataclass
class Config:
    inventory: InventorySettings = field(default_factory=InventorySettings)
    map_capture: ScreenRect = field(default_factory=ScreenRect)
    chat_log_dir: str = DEFAULT_CHAT_LOG_DIR
    active_area: str = "AreaSerbule"
    overlay_mode: bool = False
    player_east: float = 0.0
    player_south: float = 0.0
    auto_use_hotkey_vk: int = 0x75   # F6 by default
    single_use_hotkey_vk: int = 0x76  # F7 by default

    def save(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(self._to_dict(), f, indent=2)

    @classmethod
    def load(cls) -> "Config":
        if not os.path.exists(CONFIG_PATH):
            return cls()
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
            return cls._from_dict(data)
        except (json.JSONDecodeError, KeyError):
            return cls()

    def _to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def _from_dict(cls, data: dict) -> "Config":
        cfg = cls()
        if "inventory" in data:
            import dataclasses
            known = {f.name for f in dataclasses.fields(InventorySettings)}
            cfg.inventory = InventorySettings(
                **{k: v for k, v in data["inventory"].items() if k in known}
            )
        if "map_capture" in data:
            cfg.map_capture = ScreenRect(**data["map_capture"])
        cfg.chat_log_dir = data.get("chat_log_dir", DEFAULT_CHAT_LOG_DIR)
        cfg.active_area = data.get("active_area", "AreaSerbule")
        cfg.overlay_mode = data.get("overlay_mode", False)
        cfg.player_east = data.get("player_east", 0.0)
        cfg.player_south = data.get("player_south", 0.0)
        cfg.auto_use_hotkey_vk = data.get("auto_use_hotkey_vk", 0x75)
        cfg.single_use_hotkey_vk = data.get("single_use_hotkey_vk", 0x76)
        return cfg
