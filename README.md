# Project Gorgon Item Helper

A browser-based inventory and character tool for [Project Gorgon](https://www.projectgorgon.com/).
Tracks your storage vaults, work orders, recipes, favors, quests, and more — across all your characters.

---

## Requirements

- **Python 3.8+** — to run the local server ([download](https://www.python.org/downloads/))
- **Chrome or Edge** — required for the File System Access API (live folder watching)

---

## Setup

### 1. Get the files

Clone the repository or download it as a ZIP and extract it anywhere you like:

```
git clone https://github.com/Goozify/GorgonHelper.git
```

### 2. Start the server

Open a terminal in the folder and run:

```
py start_server.py
```

You should see:
```
GorgonHelper serving on http://localhost:3000
```

> Leave this terminal open while using the tool.

**Alternative — no server needed:** You can also open `GorgonHelper.html` directly in Chrome or Edge (double-click the file). Folder watching and live updates still work. The server is only needed for the in-app update feature.

### 3. Open in Chrome or Edge

Navigate to **http://localhost:3000/GorgonHelper.html** in Chrome or Edge.

### 4. Watch your Reports folder

The game writes character data to:
```
C:\Users\<you>\AppData\LocalLow\Elder Game\Project Gorgon\Reports\
```

In the tool, go to **Settings** → click **📂 Watch Folder** → select that `Reports` folder.
The tool will automatically load your character files and keep them up to date.

> **On subsequent visits Chrome may show a permission popup** asking to allow access to the folders you previously selected (`Player.log` and `Reports`). Click **Allow on every visit** to avoid being asked again each time.

The tool can also monitor `player.log` in real time to track NPC vendor prices, favor changes, and gardening plots. This file is located one folder up from `Reports`:
```
C:\Users\<you>\AppData\LocalLow\Elder Game\Project Gorgon\player.log
```
In **Settings** → **Player Log**, click **📂 Watch player.log** and select that file.

### 5. Sync game data

Still in **Settings**, under **Game Data Files**, click **Sync All** to download the latest item, recipe, and NPC data from the Project Gorgon CDN.

> This only needs to be done once, and again after game updates.

You can also download item icons in **Settings** → **Icons** → click **Download All Icons**. This grabs icon images from the CDN and saves them to an `icons/` folder inside your watched folder. Icons are optional but improve the display in several tabs.

---

## Exporting character data from the game

The tool reads `.json` files exported by the game itself.

In-game, open **Settings** → **V.I.P.** → **Special Reports**, then use:
- **Export Storage as JSON** — exports your vault and inventory contents
- **Export Character as JSON** — exports your character's skills, favor levels, and active quests

Both files are written to your `Reports` folder and picked up automatically as long as the folder is being watched. Re-export whenever you want to refresh the data.

---

## Features

| Tab | What it does |
|---|---|
| **Storage** | Search all items across vaults and characters. Filter by vault, show consolidation candidates. |
| **Work Orders** | Track work order progress with ingredient availability. Shows available work orders. |
| **Recipes** | Browse known and locked recipes with skill level, NPC trainer info, and ingredient sources. |
| **Vaults** | See vault slot usage per NPC, with favor level progress. |
| **Favors** | Track favor levels with all NPCs across characters. |
| **Quests** | Active quest objectives with item counts and vault locations. |
| **Maps** | Interactive area maps with NPC and landmark positions. |
| **Tools** | Gardening tracker, Safecracking helper, Survey tool. |

---

## Survey tool setup

The Survey tool runs a local Python process that watches your chat log, highlights inventory slots, and overlays the optimal route on your in-game map.

### 1. Install Python dependencies

Open a terminal inside the `Survey/` folder and run:

```
pip install -r requirements.txt
```

This installs: **PyQt5**, **opencv-python**, **Pillow**, **numpy**, **websockets**, and **qasync**.

### 2. Register the launcher (one time)

Still in the `Survey/` folder:

```
py setup_survey.py
```

This registers the `gorgon-survey://` custom URL protocol in the Windows registry (no admin rights needed), so the **▶ Start Survey Helper** button in the app can launch `main.pyw` directly. To undo: `py setup_survey.py remove`

### 3. Start the tool

Click **▶ Start Survey Helper** in **Tools → Survey**, or run manually:

```
cd Survey
py main.pyw
```

The process starts both the WebSocket server (port 8765) and the HTTP file server (port 3000) — no need to run `start_server.py` separately.

---

## Survey tool tips

The Survey tool (Tools → Survey) uses screen capture to track survey map locations and guide you through optimal routes.

For best results:

- **Inventory** — move the survey maps into an empty inventory bag before starting. Other items in the same bag can confuse slot detection.
- **In-game map** — open **Filters** and hide all other markers so only the survey pins are visible. Make sure the map is fully visible within the game window.
- **Map player indicator** — in **Settings → GUI → Other Window Settings**, set **Map Player Indicator Style** to **Square**. This gives the most reliable player position recognition.

**Setup screen** — inventory bag highlighted on the left, map with the red detection circle on the right:

![Survey setup](screenshots/survey-setup.png)

**Active route** — numbered survey locations with the optimised route overlay on the map:

![Survey route overlay](screenshots/survey-route-overlay.png)

---

## Updating

The tool can update itself. In **Settings** → **Version** → click **Check for updates**.
If an update is available, a button will appear to pull it automatically via `git`.

Or manually:
```
git pull origin master
```

---

## Notes

- The tool works fully offline after the initial game data sync — no account or login needed.
- All data is stored locally in your browser (IndexedDB) and in the `Reports` folder.
- Chrome/Edge are required for folder watching. The page can be opened as a file directly, but live updates won't work without the server.
