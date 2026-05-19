# ED Fleet Carrier Announcer

Tracks Elite Dangerous fleet carriers by callsign and emits payload objects
when their state meaningfully changes:

| `event_type`       | Meaning                                  |
|--------------------|------------------------------------------|
| `location_changed` | Carrier appeared in a new system         |
| `jump_started`     | Carrier jump is in progress              |
| `jump_completed`   | Carrier jump finished, location updated  |

Duplicate reports (carrier already known to be in that system) are silently
ignored.

## Data sources

The plugin reads carrier events from **two sources simultaneously**:

1. **Journal files** – the newest `Journal.*.log` is read at startup to seed
   carrier state, then tailed in real time in a background thread.  When a
   newer journal file appears (game restart) the watcher switches to it
   automatically.
2. **EDDN firehose** – the main thread listens to `tcp://eddn.edcd.io:9500`
   for live updates from all commanders.

Both sources feed through the same `announce()` function and the same
carrier state machine, so duplicates are suppressed regardless of origin.

## Setup

```
pip install -r requirements.txt
```

## Configuration

Edit `config.py`:

- **`WATCHED_CARRIERS`** – list of carrier callsigns to track (e.g. `"N3M-BKZ"`).
- **`JOURNAL_DIR`** – path to the Elite Dangerous journal folder.

## Running

```
python listener.py
```

## Payload shape

```json
{
  "callsign": "N3M-BKZ",
  "event_type": "jump_started",
  "current_location": "Sol",
  "destination": "Colonia"
}
```

`destination` is only present for `jump_started` events.


