# ED Fleet Carrier Announcer (EDFCA)

EDFCA is an [E:D Market Connector](https://github.com/EDCD/EDMarketConnector) plugin that
watches your Elite Dangerous journal for fleet-carrier events and posts a
rich Discord embed to a webhook whenever a tracked carrier jumps, arrives,
or cancels a jump.

Per-carrier webhooks are supported, so different carriers can announce to
different Discord channels.

## Contents

- [Events announced](#events-announced)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
  - [1. Locate your EDMC plugins folder](#1-locate-your-edmc-plugins-folder)
  - [2. Put this repo in that folder](#2-put-this-repo-in-that-folder)
  - [2.5 Linux extra step](#25-linux-extra-step)
  - [3. Restart EDMC](#3-restart-edmc)
  - [EDFCA uses EDMC to know where your journal files live](#edfca-uses-edmc-to-know-where-your-journal-files-live)
  - [Finding your Carrier ID](#finding-your-carrier-id)
  - [Add a carrier via the EDFCA settings tab](#add-a-carrier-via-the-edfca-settings-tab)
  - [Or edit `carriers.json` directly](#or-edit-carriersjson-directly)
- [Versioning and updates](#versioning-and-updates)
- [How it works](#how-it-works)
- [Files](#files)
- [Troubleshooting](#troubleshooting)

## Events announced

| `event_type`              | Meaning                                              |
|---------------------------|------------------------------------------------------|
| `jump_started`            | 🚀 Carrier jump scheduled (with destination + ETA)   |
| `jump_completed`          | ✅ Carrier arrived at the destination system         |
| `jump_cancelled`          | ❌ Carrier jump cancelled by the owner               |
| `jump_unexpected_cancel`  | ⚠️ Jump cancelled without an explicit cancel event   |
| `location_changed`        | 📍 Carrier located in a new system (silent recovery) |

Duplicate events are suppressed via a local event cache, so restarting EDMC
will not re-post events that were already announced.

## Prerequisites

- [Elite Dangerous](https://www.elitedangerous.com/) (PC — journal files
  must be written to disk; this includes the Linux + Proton/Wine setup).
- [EDMarketConnector](https://github.com/EDCD/EDMarketConnector/releases)
  installed and running.
- **Linux users:** Python `requests` library (EDMC's bundled Python on Windows already has
  it; on Linux installs see [Linux extra step](#25-linux-extra-step) below).
- A Discord channel + webhook URL for each carrier you want to announce.

## Installation

### 1. Locate your EDMC plugins folder

1. Open E:D Market Connector.
2. File -> Settings -> Plugins -> **Open Plugins Folder**

### 2. Put this repo in that folder

Either download a zip and extract it:

1. Open the [repository](https://github.com/TexNevada/EDFleetCarrierAnnouncer)
   (switch to the `dev` branch first if you want the bleeding edge).
2. **Code → Download ZIP**, then extract it into the plugins folder.
3. Rename the extracted folder from `EDFleetCarrierAnnouncer-main` to
   `EDFleetCarrierAnnouncer`.

…or clone it, which is what you want on `dev` because it enables update
checks (see [Versioning and updates](#versioning-and-updates)):

```bash
cd /path/to/EDMarketConnector/plugins
git clone https://github.com/TexNevada/EDFleetCarrierAnnouncer.git
```

The folder name (`EDFleetCarrierAnnouncer`) is the plugin name EDMC will
display.

### 2.5 Linux extra step

EDMC isn't officially supported on Linux, but a community port exists.
If `requests` isn't already installed in the same Python environment EDMC
uses, install it:

```bash
pip install -r EDFleetCarrierAnnouncer/requirements.txt
```

### 3. Restart EDMC

EDMC loads plugins at startup. After restarting you should see:

- An **EDFCA: Running - v1.0.0** label in the main EDMC window.
- A new **EDFCA** tab in **File → Settings**.

### EDFCA uses EDMC to know where your journal files live

EDFCA reads the journal directory from EDMC's own configuration.

On Linux/Proton this path might not be automatically set. If you haven't set the Journal Path yet: 

**File → Settings → Configuration → "E:D journal file location"**
→ point it at the folder containing `Journal.*.log` files.

On Windows the default is:

```
%USERPROFILE%\Saved Games\Frontier Developments\Elite Dangerous
```

On Linux the journal lives inside the Proton prefix (app ID `359320`).
Default locations, depending on how Steam is installed:

| Steam install    | Path                                                                                                                                |
|------------------|-------------------------------------------------------------------------------------------------------------------------------------|
| Standard Steam   | `~/.local/share/Steam/steamapps/compatdata/359320/pfx/drive_c/users/steamuser/Saved Games/Frontier Developments/Elite Dangerous/`      |
| Symlink variant  | `~/.steam/steam/steamapps/compatdata/359320/pfx/drive_c/users/steamuser/Saved Games/Frontier Developments/Elite Dangerous/`            |
| Flatpak Steam    | `~/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/compatdata/359320/pfx/drive_c/users/steamuser/Saved Games/Frontier Developments/Elite Dangerous/` |

If you installed Elite Dangerous to an additional Steam library folder (a
second drive, for example), `compatdata` lives under that library instead —
the tail of the path (`compatdata/359320/pfx/...`) is the same, only the
Steam root differs. Non-Steam Wine/Lutris setups put it under whatever
prefix you configured.

To locate it if none of the above match:

```bash
find / -type d -name "Elite Dangerous" -path "*Frontier Developments*" 2>/dev/null
```

### Finding your Carrier ID

Your `CarrierID` is not shown anywhere in the game UI — it lives in the
journal files Elite Dangerous writes to your own PC, in the same folder you
pointed EDMC at above (`Journal.*.log`).

The game writes a `CarrierStats` event every time you open the carrier
management screen, so the quickest way to generate a fresh one is: log in,
open **Carrier Management**, then look at the newest `Journal.*.log` file.
Each journal line is a single JSON object; the one you want looks like this
(trimmed):

```json
{ "timestamp":"2026-09-06T15:49:35Z", "event":"CarrierStats", "CarrierID":3711951104, "CarrierType":"FleetCarrier", "Callsign":"N3M-BKZ", "Name":"THE HYPERION", ... }
```

Here the Carrier ID is `3711951104` and the callsign is `N3M-BKZ` — both of
which go into the EDFCA settings tab.

To find it without scrolling through the file:

```bash
# Linux / macOS
grep -h CarrierStats "$JOURNAL_DIR"/Journal.*.log | tail -1
```

```powershell
# Windows (PowerShell) — default journal folder
Select-String -Path "$env:USERPROFILE\Saved Games\Frontier Developments\Elite Dangerous\Journal.*.log" -Pattern CarrierStats | Select-Object -Last 1
```

`CarrierID` also appears in the `CarrierJumpRequest`, `CarrierJump`,
`CarrierJumpCancelled` and `CarrierLocation` events, so any of those lines
will do if you don't have a `CarrierStats` line handy.

### Add a carrier via the EDFCA settings tab

1. Open **EDMC → File → Settings → EDFCA**.
2. Click **+ Add Carrier**.
3. Fill in:

   | Field           | Required          | Notes                                                                                                       |
   |-----------------|-------------------|-------------------------------------------------------------------------------------------------------------|
   | Callsign        | ✓                 | Your carrier ID, e.g. `N3M-BKZ`. Rows without a callsign are dropped when you click OK.                     |
   | Carrier ID      | Recommended       | `CarrierID` from the journal (see [Finding your Carrier ID](#finding-your-carrier-id)) — needed for events that omit the callsign (some `CarrierJumpCancelled` etc.). |
   | Name            | Optional          | Free-text display name.                                                                                     |
   | Discord Webhook | For announcements | Full webhook URL. If blank, the carrier is tracked silently — nothing is posted to Discord.                 |
   | Logo URL        | Optional          | Image URL — shown as the embed thumbnail.                                                                   |

4. Click **OK**. `carriers.json` is rewritten and the live plugin reloads it.

### Or edit `carriers.json` directly

```json
{
  "carriers": [
    {
      "callsign": "N3M-BKZ",
      "CarrierID": 3700000000,
      "name": "Wandering Albatross",
      "discord_webhook": "https://discord.com/api/webhooks/.../...",
      "logo_url": "https://example.com/logo.png"
    }
  ]
}
```

The plugin writes a `last_known_location` field back to this file as it
sees your carrier in different systems — leave it alone, the plugin
manages it.

## Versioning and updates

The main-window label shows the running version, e.g.
**EDFCA: Running - v1.0.0**. There are two channels:

| Branch | Version | Updates are checked against |
|--------|---------|-----------------------------|
| `main` | `v1.0.0` | the latest [release](https://github.com/TexNevada/EDFleetCarrierAnnouncer/releases) |
| `dev`  | `v1.0.0-dev` | the tip of the `dev` branch |

The check runs once per EDMC start. A second line appears under the status
label only when there is something to say:

- **⬆ Update available: …** — click it to open the release or commit list.
- **🚫 Update check unavailable** — nothing could be compared. The EDMC log
  says why. The usual reason is a `dev` build installed from a zip: without a
  `.git` directory there is no commit id to compare against the branch, so
  **`dev` users should install with `git clone`**. Zip installs of `main` are
  checked normally.

Nothing else is shown when you are up to date. To upgrade, `git pull` a clone,
or re-download and replace the folder (keep your `carriers.json`).

## How it works

On startup EDFCA replays the newest `Journal.*.log` to seed state for each
watched carrier, then tails that file (and any newer journal that appears)
in a background thread. Relevant events — `CarrierJumpRequest`,
`CarrierJump`, `CarrierJumpCancelled`, `CarrierLocation`, plus
`Location`/`FSDJump` while docked at a tracked carrier — are turned into
payloads, deduplicated against `event_cache.json`, and POSTed as Discord
embeds to the per-carrier webhook.

EDMC is purely the host process: EDFCA does **not** read or write EDMC's
own data, send anything to EDDN, or talk to Frontier's servers. The only
outbound traffic is your Discord webhooks plus one unauthenticated GitHub
API request per start for the update check.

## Files

```
EDFleetCarrierAnnouncer/
├── load.py            # EDMC entry point: lifecycle, settings tab, journal dir
├── listener.py        # main loop: dedup, embed builder, Discord POST
├── journal_parser.py  # newest-journal discovery + real-time tailer
├── carrier_state.py   # per-carrier state machine + registry
├── event_cache.py     # local dedup cache (persists across restarts)
├── fc_config.py       # carriers.json load/save
├── version.py         # the running version (differs between main and dev)
├── updater.py         # update check against GitHub
├── carriers.json      # YOUR carriers (gitignored — do not commit)
└── requirements.txt
```


`destination` is only present for `jump_started`. `system_info` fields are
optional and only included when the underlying journal event carries them.

## Troubleshooting

- **Plugin doesn't appear in EDMC** — confirm the folder is directly inside
  EDMC's plugins folder (not nested) and that EDMC was restarted.
- **No events announced** — check EDMC's log (**File → Settings → Plugins →
  Open Log Folder**) for `EDFCA` entries; verify the journal path under
  EDMC's Configuration tab points at a folder that actually contains
  `Journal.*.log` files.
- **Webhook posts fail** — the log will show the Discord HTTP status. 401
  means the webhook URL is wrong or revoked; 404 means it was deleted.
- **Duplicate posts after restart** — delete `event_cache.json` if you've
  intentionally cleared state and want a fresh replay.
