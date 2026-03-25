import os
from typing import List, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QToolBar, QAction,
    QStatusBar, QLabel, QMessageBox, QDialog,
    QFormLayout, QSpinBox, QDialogButtonBox, QPushButton, QGroupBox,
    QLineEdit, QDoubleSpinBox,
)

from config import Config
from chat_watcher import ChatWatcher
from survey_store import SurveyStore, SurveyLocation
from route_solver import nearest_neighbor_route
from ui_inventory_overlay import InventoryOverlay
from ui_game_map_overlay import GameMapOverlay
from ui_region_selector import RegionSelector
from inventory_click_watcher import InventoryClickWatcher


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PG Survey Helper")
        self.setMinimumSize(420, 500)

        self.config = Config.load()
        self.store = SurveyStore()
        self.store.clear_all()  # fresh start each session
        self.chat_watcher: Optional[ChatWatcher] = None
        self.inv_overlay = InventoryOverlay()
        self.map_overlay = GameMapOverlay()

        self._surveying = False
        self._setup_complete = False  # True after setup stops (chat watcher in validation mode)
        self._current_scan_slot = 0
        self._route_id_order: List[int] = []   # location IDs in route order
        self._route_mapped: List[int] = []    # route indices into all_locs for map overlay
        self._current_slot_labels: dict = {}   # slot_index → route_number_str
        self._pending_visit_loc = None          # location awaiting chat confirmation

        # Inventory double-click watcher (global mouse hook)
        self.click_watcher = InventoryClickWatcher(self)
        self.click_watcher.double_clicked_slot.connect(self._on_inv_double_click)

        self._setup_ui()
        self._setup_toolbar()
        self._refresh_locations()

        if self.config.map_capture.w > 0:
            self.map_overlay.configure_region(
                self.config.map_capture.x, self.config.map_capture.y,
                self.config.map_capture.w, self.config.map_capture.h,
            )

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)

        layout.addWidget(QLabel("Survey Locations:"))
        self.location_list = QListWidget()
        self.location_list.itemDoubleClicked.connect(self._on_location_double_click)
        layout.addWidget(self.location_list)

        self.route_label = QLabel("")
        layout.addWidget(self.route_label)

        btn_row = QHBoxLayout()
        btn_clear = QPushButton("Clear All")
        btn_clear.clicked.connect(self._clear_locations)
        btn_row.addWidget(btn_clear)
        btn_mark = QPushButton("Mark Visited")
        btn_mark.clicked.connect(self._mark_selected_visited)
        btn_row.addWidget(btn_mark)
        layout.addLayout(btn_row)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready — open Settings to configure inventory and map regions.")

    def _setup_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        self.act_survey = QAction("▶  Setup Surveying", self)
        self.act_survey.setCheckable(True)
        self.act_survey.setToolTip(
            "Start: watch chat log for new survey coordinates and highlight inventory slots.\n"
            "Stop: auto-calculate the optimal route and show it on the in-game map overlay."
        )
        self.act_survey.toggled.connect(self._toggle_surveying)
        tb.addAction(self.act_survey)

        tb.addSeparator()

        act_route = QAction("Recalculate Route", self)
        act_route.triggered.connect(self._calculate_route)
        tb.addAction(act_route)

        tb.addSeparator()


        act_settings = QAction("Settings", self)
        act_settings.triggered.connect(self._show_settings)
        tb.addAction(act_settings)

    # ------------------------------------------------------------------
    # Refresh display
    # ------------------------------------------------------------------

    def _refresh_locations(self):
        area = self.config.active_area
        locations = self.store.get_all(area)

        # Build permanent route number mapping (never renumbers)
        route_num_by_id = {}
        for i, loc_id in enumerate(self._route_id_order):
            route_num_by_id[loc_id] = i + 1

        self.location_list.clear()
        for scan_idx, loc in enumerate(locations):
            prefix = "[✓]" if loc.visited else "[ ]"
            dir_ew = "E" if (loc.east_absolute or 0) >= 0 else "W"
            dir_ns = "S" if (loc.south_absolute or 0) >= 0 else "N"

            num = route_num_by_id.get(loc.id, scan_idx + 1)

            text = (
                f"{prefix} #{num} {loc.item_name}  "
                f"({abs(loc.east_absolute or 0):.0f}{dir_ew}, "
                f"{abs(loc.south_absolute or 0):.0f}{dir_ns})"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, loc.id)
            if loc.visited:
                item.setForeground(Qt.darkGreen)
            self.location_list.addItem(item)

        # Only reset route_order on the overlay if we don't already have a route
        if not self._route_id_order:
            self.map_overlay.update_survey_data(locations, [])

    # ------------------------------------------------------------------
    # Setup Surveying  (combined watch + scan)
    # ------------------------------------------------------------------

    def _toggle_surveying(self, enabled: bool):
        self._surveying = enabled

        if enabled:
            self.act_survey.setText("⏹  Stop Setup")
            self._setup_complete = False

            # Fresh session: stop watcher and clear previous state
            self.click_watcher.stop()
            if self.chat_watcher:
                self.chat_watcher.stop()
                self.chat_watcher.wait(2000)
                self.chat_watcher = None

            self.store.clear_area(self.config.active_area)
            self._route_id_order = []
            self._route_mapped = []
            self._current_scan_slot = 0
            self._current_slot_labels = {}
            self._pending_visit_loc = None
            self.route_label.setText("")
            self.map_overlay.clear_circle_pins()
            self.map_overlay._setup_active = True
            self.inv_overlay.set_slot_labels({})
            self._refresh_locations()

            inv = self.config.inventory
            self.inv_overlay.configure(
                inv.screen_x, inv.screen_y,
                inv.slot_width, inv.slot_height,
                inv.grid_cols, inv.grid_rows, inv.slot_gap,
                inv.padding_left, inv.padding_top,
            )
            self.inv_overlay.set_current_slot(0)
            self.inv_overlay.set_overlay_visible(True)

            if self.config.map_capture.w > 0:
                self.map_overlay.configure_region(
                    self.config.map_capture.x, self.config.map_capture.y,
                    self.config.map_capture.w, self.config.map_capture.h,
                )
            self.map_overlay.set_visible(True)

            self.chat_watcher = ChatWatcher(
                self.config.chat_log_dir, skip_existing=True
            )
            self.chat_watcher.survey_detected.connect(self._on_survey_detected)
            self.chat_watcher.survey_completed.connect(self._on_survey_completed)
            self.chat_watcher.area_changed.connect(self._on_area_detected)
            self.chat_watcher.error_occurred.connect(self._on_watch_error)
            self.chat_watcher.start()

            self.status_bar.showMessage(
                "Setup active — double-click each survey map to reveal its location."
            )

        else:
            self.act_survey.setText("▶  Setup Surveying")
            self._setup_complete = True

            # Keep chat watcher running (used to validate survey completion)

            self.inv_overlay.set_overlay_visible(False)

            self.map_overlay._setup_active = False

            # Assign circle pin pixel positions directly to locations by index.
            # During setup, pins and locations are added in the same order.
            all_locs = self.store.get_all(self.config.active_area)
            pins = self.map_overlay._circle_pins
            matched = 0
            for i, loc in enumerate(all_locs):
                if i < len(pins):
                    loc.pixel_x = float(pins[i][0])
                    loc.pixel_y = float(pins[i][1])
                    matched += 1
            self.store.save()

            # Calibrate pixel<->meter for player position tracking
            self.map_overlay.calibrate(all_locs)
            self._calculate_route()
            self.map_overlay.update()

            mo = self.map_overlay
            self.status_bar.showMessage(
                f"{len(all_locs)} surveys, {len(pins)} pins | "
                f"ticks={mo._debug_tick} arrows={mo._debug_arrow_ok} "
                f"circles_checked={mo._debug_circle_checks} "
                f"err={mo._debug_last_error}"
            )

    # ------------------------------------------------------------------
    # Survey detection callback
    # ------------------------------------------------------------------

    def _on_survey_detected(self, item_name: str, east: int, south: int):
        if self._setup_complete:
            # Coordinate hint = survey was used but player was too far.
            # Override any slot-based pending with coordinate-matched location
            # (coordinates are more reliable than inventory slot tracking).
            east_abs = self.config.player_east + east
            south_abs = self.config.player_south + south
            area = self.config.active_area
            best_loc = None
            best_dist = float("inf")
            for loc in self.store.get_unvisited(area):
                if loc.east_absolute is None:
                    continue
                dx = loc.east_absolute - east_abs
                dy = loc.south_absolute - south_abs
                d = (dx * dx + dy * dy) ** 0.5
                if d < best_dist:
                    best_dist = d
                    best_loc = loc
            if best_loc and best_dist < 50:
                self._pending_visit_loc = best_loc
                QTimer.singleShot(10000, self._timeout_pending_visit)
            return

        area = self.config.active_area
        east_abs = self.config.player_east + east
        south_abs = self.config.player_south + south

        loc = self.store.add(
            area=area,
            item_name=item_name,
            east_rel=east,
            south_rel=south,
            player_east=self.config.player_east,
            player_south=self.config.player_south,
            inventory_slot=self._current_scan_slot,
        )

        if loc:
            self._current_scan_slot += 1
            self.inv_overlay.set_current_slot(self._current_scan_slot)

            self.status_bar.showMessage(
                f"Detected: {item_name}  ({east:+d}E, {south:+d}S)  "
                f"[slot {self._current_scan_slot}]"
            )
        else:
            self.status_bar.showMessage(
                f"Duplicate skipped: {east:+d}E, {south:+d}S"
            )

        self._refresh_locations()


    def _on_survey_completed(self, item_name: str):
        """Chat log says loot was collected — confirm the pending visit.

        The double-click already identified which location is being surveyed
        (slot-based). Trust that — proximity matching caused wrong visits when
        two surveys were close together. Just confirm the pending location.
        """
        if not self._setup_complete:
            return

        if self._pending_visit_loc is None:
            return

        loc = self._pending_visit_loc
        self._pending_visit_loc = None
        self._mark_location_visited(loc)

    # ------------------------------------------------------------------
    # Inventory double-click
    # ------------------------------------------------------------------

    def _on_inv_double_click(self, slot_index: int):
        """Called when a labeled inventory slot is double-clicked.

        Sets pending visit by slot (fallback for when no coordinate hint fires,
        i.e. successful survey use). If a coordinate hint fires afterwards,
        it will override this with a coordinate-matched location.
        """
        if self._pending_visit_loc is not None:
            return

        for loc_id in self._route_id_order:
            loc = self.store.get_by_id(loc_id)
            if loc and not loc.visited and loc.inventory_slot == slot_index:
                self._pending_visit_loc = loc
                label = self._current_slot_labels.get(slot_index, '?')
                self.status_bar.showMessage(
                    f"Surveying #{label}... waiting for chat confirmation"
                )
                QTimer.singleShot(10000, self._timeout_pending_visit)
                return

    def _timeout_pending_visit(self):
        """Cancel pending visit if chat never confirmed it."""
        if self._pending_visit_loc is not None:
            self._pending_visit_loc = None
            self.status_bar.showMessage(
                "Survey timed out — no confirmation from chat log"
            )

    def _mark_location_visited(self, loc):
        """Mark a location visited and update map + inventory overlays.

        Numbers are permanent — #3 stays #3 even after #1 and #2 are done.
        The game shifts all items left to fill the consumed slot, so every
        item at a higher slot index moves down by 1.
        """
        self.store.mark_visited(loc.id)

        consumed_slot = loc.inventory_slot

        # --- Inventory shift: game shifts all higher items left by 1 ---
        if consumed_slot is not None:
            remaining = self.store.get_unvisited(self.config.active_area)
            for ul in remaining:
                if ul.inventory_slot is not None and ul.inventory_slot > consumed_slot:
                    self.store.update_slot(ul.id, ul.inventory_slot - 1)

        # --- Rebuild inventory labels (keep original numbers, update slots) ---
        self._rebuild_slot_labels()

        # --- Refresh map overlay with updated visited flags ---
        all_locs = self.store.get_all(self.config.active_area)
        self.map_overlay.update_survey_data(all_locs, self._route_mapped)

        remaining_count = len(self.store.get_unvisited(self.config.active_area))
        if remaining_count == 0:
            self.inv_overlay.set_overlay_visible(False)
            self.status_bar.showMessage("All survey locations visited!")
        else:
            self.status_bar.showMessage(
                f"Visited: {loc.item_name} — {remaining_count} remaining"
            )
        self._refresh_locations()

    def _rebuild_slot_labels(self):
        """Rebuild inventory slot labels from current route state.

        Numbers are permanent — each location keeps its original route number.
        Only the slot positions change due to inventory shifting.
        """
        labels = {}
        first_unvisited_slot = None
        for route_num_0, loc_id in enumerate(self._route_id_order):
            loc = self.store.get_by_id(loc_id)
            if loc and not loc.visited and loc.inventory_slot is not None:
                route_num = route_num_0 + 1
                labels[loc.inventory_slot] = str(route_num)
                if first_unvisited_slot is None:
                    first_unvisited_slot = loc.inventory_slot
        self._current_slot_labels = labels
        self.inv_overlay.set_slot_labels(labels, first_unvisited_slot)
        self.inv_overlay.repaint()
        self.click_watcher.set_active_slots(set(labels.keys()))

    def _on_area_detected(self, area: str):
        self.config.active_area = area
        self.status_bar.showMessage(f"Area detected: {area}")

    def _on_watch_error(self, msg: str):
        self.status_bar.showMessage(f"Chat watcher error: {msg}")

    # ------------------------------------------------------------------
    # Route
    # ------------------------------------------------------------------

    def _calculate_route(self):
        area = self.config.active_area
        unvisited = self.store.get_unvisited(area)
        if not unvisited:
            self.status_bar.showMessage("No unvisited locations — nothing to route.")
            return

        points = []
        valid_locs = []
        for loc in unvisited:
            if loc.east_absolute is not None:
                points.append((loc.east_absolute, loc.south_absolute))
                valid_locs.append(loc)

        if not points:
            self.status_bar.showMessage("No locations with coordinates yet.")
            return

        start = (self.config.player_east, self.config.player_south)
        route_indices = nearest_neighbor_route(points, start)

        # Store route as ordered list of location IDs (permanent numbering)
        self._route_id_order = [valid_locs[ri].id for ri in route_indices]

        all_locs = self.store.get_all(area)

        # Map route to indices in all_locs for the overlay
        self._route_mapped = []
        for ri in route_indices:
            target_id = valid_locs[ri].id
            for i, loc in enumerate(all_locs):
                if loc.id == target_id:
                    self._route_mapped.append(i)
                    break

        self.map_overlay.update_survey_data(all_locs, self._route_mapped)

        total_dist = 0.0
        prev = start
        for ri in route_indices:
            p = points[ri]
            total_dist += ((p[0] - prev[0]) ** 2 + (p[1] - prev[1]) ** 2) ** 0.5
            prev = p

        self.route_label.setText(
            f"Route: {len(route_indices)} stops  ~{total_dist:.0f} m total"
        )
        self.status_bar.showMessage(
            f"Route: {len(route_indices)} stops, ~{total_dist:.0f} m"
        )

        # Build inventory slot → route number mapping and show overlay
        slot_labels = {}
        first_slot = None
        for route_num, ri in enumerate(route_indices, start=1):
            loc = valid_locs[ri]
            slot = loc.inventory_slot
            if slot is not None:
                slot_labels[slot] = str(route_num)
                if first_slot is None:
                    first_slot = slot
        self._current_slot_labels = slot_labels
        self.inv_overlay.set_slot_labels(slot_labels, first_slot)
        inv = self.config.inventory
        self.inv_overlay.configure(
            inv.screen_x, inv.screen_y,
            inv.slot_width, inv.slot_height,
            inv.grid_cols, inv.grid_rows, inv.slot_gap,
            inv.padding_left, inv.padding_top,
        )
        self.inv_overlay.set_overlay_visible(True)

        # Start inventory double-click watcher
        self.click_watcher.configure(
            inv.screen_x, inv.screen_y,
            inv.slot_width, inv.slot_height,
            inv.grid_cols, inv.grid_rows, inv.slot_gap,
        )
        self.click_watcher.set_active_slots(set(slot_labels.keys()))
        self.click_watcher.start()

        self._refresh_locations()


    # ------------------------------------------------------------------
    # Location list actions
    # ------------------------------------------------------------------

    def _on_location_double_click(self, item: QListWidgetItem):
        loc_id = item.data(Qt.UserRole)
        loc = self.store.get_by_id(loc_id)
        if loc:
            self._mark_location_visited(loc)

    def _mark_selected_visited(self):
        item = self.location_list.currentItem()
        if item:
            loc = self.store.get_by_id(item.data(Qt.UserRole))
            if loc:
                self._mark_location_visited(loc)

    def _clear_locations(self):
        reply = QMessageBox.question(
            self, "Clear",
            f"Clear all survey locations?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.store.clear_area(self.config.active_area)
            self._route_id_order = []
            self._route_mapped = []
            self.route_label.setText("")
            self.map_overlay.clear_circle_pins()
            self.map_overlay.update_survey_data([], [])
            self._refresh_locations()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _show_settings(self):
        dlg = SettingsDialog(self.config, self)
        dlg.setModal(False)
        dlg.accepted.connect(self._on_settings_accepted)
        dlg.show()
        self._settings_dlg = dlg

    def _on_settings_accepted(self):
        self.config.save()
        if self.config.map_capture.w > 0:
            self.map_overlay.configure_region(
                self.config.map_capture.x, self.config.map_capture.y,
                self.config.map_capture.w, self.config.map_capture.h,
            )
        self.status_bar.showMessage("Settings saved.")
        self._settings_dlg = None

    # ------------------------------------------------------------------
    # Window close
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self.click_watcher.stop()
        if self.chat_watcher:
            self.chat_watcher.stop()
            self.chat_watcher.wait(2000)
        self.inv_overlay.close()
        self.map_overlay.close()
        self.config.save()
        super().closeEvent(event)


# ======================================================================
# Slot-gap auto-detection
# ======================================================================

def _detect_slot_gap(slot_x: int, slot_y: int,
                     slot_w: int, slot_h: int,
                     max_gap: int = 30) -> int:
    try:
        from PIL import ImageGrab
        import numpy as np

        mid_y = slot_y + slot_h // 2
        strip = ImageGrab.grab(
            bbox=(slot_x + slot_w, mid_y - 2,
                  slot_x + slot_w + max_gap + 4, mid_y + 3)
        )
        arr = np.array(strip.convert("L"), dtype=float)
        col_brightness = arr.mean(axis=0)

        ref = float(np.array(
            ImageGrab.grab(bbox=(slot_x + slot_w - 3, mid_y - 2,
                                 slot_x + slot_w,     mid_y + 3))
            .convert("L")
        ).mean())

        threshold = max(30.0, ref * 0.40)

        for i, b in enumerate(col_brightness):
            if b >= threshold:
                return max(1, i)

    except Exception:
        pass

    return 4


# ======================================================================
# Settings dialog
# ======================================================================

class SettingsDialog(QDialog):
    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        self._selector = None
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)

        # --- Inventory: first-slot selection ---
        inv_grp = QGroupBox("Inventory — First Slot")
        inv_layout = QVBoxLayout(inv_grp)

        inv_layout.addWidget(QLabel(
            "Drag over exactly ONE slot (the top-left slot of your inventory).\n"
            "Slot size and position are read directly from your selection."
        ))

        slot_info = (
            f"Slot: ({config.inventory.screen_x}, {config.inventory.screen_y})  "
            f"Size: {config.inventory.slot_width} × {config.inventory.slot_height} px"
            if config.inventory.slot_width > 0
            else "Not set — click the button below"
        )
        self.inv_slot_label = QLabel(slot_info)
        inv_layout.addWidget(self.inv_slot_label)

        btn_inv = QPushButton("Select First Inventory Slot on Screen")
        btn_inv.clicked.connect(self._select_inventory_region)
        inv_layout.addWidget(btn_inv)

        layout.addWidget(inv_grp)

        # --- Inventory grid ---
        grid_grp = QGroupBox("Inventory Grid")
        grid_layout = QFormLayout(grid_grp)

        self.inv_cols = QSpinBox()
        self.inv_cols.setRange(1, 20)
        self.inv_cols.setValue(config.inventory.grid_cols)
        grid_layout.addRow("Columns:", self.inv_cols)

        self.inv_rows = QSpinBox()
        self.inv_rows.setRange(1, 20)
        self.inv_rows.setValue(config.inventory.grid_rows)
        grid_layout.addRow("Rows:", self.inv_rows)

        self.inv_gap = QSpinBox()
        self.inv_gap.setRange(0, 30)
        self.inv_gap.setValue(config.inventory.slot_gap)
        self.inv_gap.setToolTip(
            "Pixels between slots. Auto-detected when you select a slot,\n"
            "but can be adjusted manually if the alignment is slightly off."
        )
        gap_row = QHBoxLayout()
        gap_row.addWidget(self.inv_gap)
        btn_detect_gap = QPushButton("Re-detect gap")
        btn_detect_gap.setFixedWidth(110)
        btn_detect_gap.clicked.connect(self._detect_gap)
        gap_row.addWidget(btn_detect_gap)
        grid_layout.addRow("Slot Gap (px):", gap_row)

        layout.addWidget(grid_grp)

        # --- In-game map region ---
        map_grp = QGroupBox("In-Game Map Region")
        map_layout = QVBoxLayout(map_grp)

        self.map_region_label = QLabel(self._fmt_map_rect(
            config.map_capture.x, config.map_capture.y,
            config.map_capture.w, config.map_capture.h,
        ))
        map_layout.addWidget(self.map_region_label)

        btn_map = QPushButton("Select Map Region on Screen")
        btn_map.clicked.connect(self._select_map_region)
        map_layout.addWidget(btn_map)

        layout.addWidget(map_grp)

        # --- Chat log ---
        chat_grp = QGroupBox("Chat Log")
        chat_layout = QFormLayout(chat_grp)
        self.chat_dir = QLineEdit(config.chat_log_dir)
        chat_layout.addRow("Log Directory:", self.chat_dir)
        layout.addWidget(chat_grp)

        # --- Buttons ---
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_map_rect(x, y, w, h):
        if w == 0 and h == 0:
            return "Not set — click the button below to select on screen"
        return f"Position: ({x}, {y})   Size: {w} × {h} px"

    # ------------------------------------------------------------------
    # Region selection
    # ------------------------------------------------------------------

    def _select_inventory_region(self):
        self.hide()
        self._selector = RegionSelector(
            "Drag over exactly ONE inventory slot (top-left slot). Press Escape to cancel."
        )
        self._selector.region_selected.connect(self._on_inv_region)
        self._selector.cancelled.connect(self._on_selection_cancelled)
        self._selector.start_selection()

    def _on_inv_region(self, x, y, w, h):
        self._selector = None
        self.config.inventory.screen_x    = x
        self.config.inventory.screen_y    = y
        self.config.inventory.slot_width  = max(8, w)
        self.config.inventory.slot_height = max(8, h)
        self.config.inventory.padding_left = 0
        self.config.inventory.padding_top  = 0

        self.inv_slot_label.setText(f"Slot: ({x}, {y})   Size: {w} × {h} px")

        gap = _detect_slot_gap(x, y, w, h)
        self.config.inventory.slot_gap = gap
        self.inv_gap.setValue(gap)

        self.show()

    def _detect_gap(self):
        inv = self.config.inventory
        if inv.slot_width <= 0:
            return
        gap = _detect_slot_gap(
            inv.screen_x, inv.screen_y,
            inv.slot_width, inv.slot_height,
        )
        self.config.inventory.slot_gap = gap
        self.inv_gap.setValue(gap)

    def _select_map_region(self):
        self.hide()
        self._selector = RegionSelector(
            "Drag over the IN-GAME MAP window. Press Escape to cancel."
        )
        self._selector.region_selected.connect(self._on_map_region)
        self._selector.cancelled.connect(self._on_selection_cancelled)
        self._selector.start_selection()

    def _on_map_region(self, x, y, w, h):
        self._selector = None
        self.config.map_capture.x = x
        self.config.map_capture.y = y
        self.config.map_capture.w = w
        self.config.map_capture.h = h
        self.map_region_label.setText(self._fmt_map_rect(x, y, w, h))
        self.show()

    def _on_selection_cancelled(self):
        self._selector = None
        self.show()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save_and_accept(self):
        self.config.inventory.grid_cols = self.inv_cols.value()
        self.config.inventory.grid_rows = self.inv_rows.value()
        self.config.inventory.slot_gap  = self.inv_gap.value()
        self.config.chat_log_dir        = self.chat_dir.text()
        self.accept()
