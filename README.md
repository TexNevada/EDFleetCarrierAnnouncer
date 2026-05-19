# ED Fleet Carrier Announcer (EDFCA)

EDFCA is an [E:D Market Connector](https://github.com/EDCD/EDMarketConnector) plugin that
watches your Elite Dangerous journal for fleet-carrier events and posts a
rich Discord embed to a webhook whenever a tracked carrier jumps, arrives,
or cancels a jump.

Per-carrier webhooks are supported, so different carriers can announce to
different Discord channels.

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
- Python `requests` library (EDMC's bundled Python on Windows already has
  it; on Linux installs see [Linux setup](#linux-extra-step) below).
- A Discord channel + webhook URL for each carrier you want to announce.

## Installation

### 1. Locate your EDMC plugins folder

1. Open E:D Market Connector.
2. File -> Settings -> Plugins -> **Open Plugins Folder**

### 2. Clone this repo into that folder

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

- An **EDFCA: running** label in the main EDMC window.
- A new **EDFCA** tab in **File → Settings**.

### EDFCA uses EDMC to know where your journal files live

EDFCA reads the journal directory from EDMC's own configuration.

On Linux/Proton this path might not be automatically set. If you haven't set the Journal Path yet: 

**File → Settings → Configuration → "E:D journal file location"**
→ point it at the folder containing `Journal.*.log` files.

For Steam Proton this usually looks like:

```
~/.local/share/Steam/steamapps/compatdata/359320/pfx/drive_c/users/steamuser/Saved Games/Frontier Developments/Elite Dangerous
```

### Add a carrier via the EDFCA settings tab

1. Open **EDMC → File → Settings → EDFCA**.
2. Click **+ Add Carrier**.
3. Fill in:

   | Field           | Required          | Notes                                                                                                       |
   |-----------------|-------------------|-------------------------------------------------------------------------------------------------------------|
   | Callsign        | ✓                 | Your carrier ID, e.g. `N3M-BKZ`. Rows without a callsign are dropped when you click OK.                     |
   | Carrier ID      | Recommended       | `CarrierID` from the journal — needed for events that omit the callsign (some `CarrierJumpCancelled` etc.). |
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

## How it works

On startup EDFCA replays the newest `Journal.*.log` to seed state for each
watched carrier, then tails that file (and any newer journal that appears)
in a background thread. Relevant events — `CarrierJumpRequest`,
`CarrierJump`, `CarrierJumpCancelled`, `CarrierLocation`, plus
`Location`/`FSDJump` while docked at a tracked carrier — are turned into
payloads, deduplicated against `event_cache.json`, and POSTed as Discord
embeds to the per-carrier webhook.

EDMC is purely the host process: EDFCA does **not** read or write EDMC's
own data, send anything to EDDN, or talk to Frontier's servers.

## Files

```
EDFleetCarrierAnnouncer/
├── load.py            # EDMC entry point: lifecycle, settings tab, journal dir
├── listener.py        # main loop: dedup, embed builder, Discord POST
├── journal_parser.py  # newest-journal discovery + real-time tailer
├── carrier_state.py   # per-carrier state machine + registry
├── event_cache.py     # local dedup cache (persists across restarts)
├── fc_config.py       # carriers.json load/save
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
