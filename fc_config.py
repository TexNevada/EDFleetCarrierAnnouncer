"""
Configuration for the Fleet Carrier Announcer.

Carrier definitions live in ``carriers.json`` next to this file.
Edit that file to add/remove carriers – each entry needs a callsign and a
human-readable name.
"""

import json
import os

from _logger import logger

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CARRIERS_FILE = os.path.join(_CONFIG_DIR, "carriers.json")


def _load_carriers() -> list[dict]:
    """Load the carrier list from carriers.json."""
    if not os.path.exists(CARRIERS_FILE):
        logger.warning("%s not found – no carriers will be tracked", CARRIERS_FILE)
        return []
    with open(CARRIERS_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("carriers", [])


def save_carriers(carriers: list[dict]) -> None:
    """Write the carrier list back to carriers.json (preserves last_known_location)."""
    data = {"carriers": carriers}
    with open(CARRIERS_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4)
        fh.write("\n")


def reload_carriers() -> list[dict]:
    """Reload carriers from disk and update the module-level list in place."""
    global WATCHED_CARRIERS
    WATCHED_CARRIERS = _load_carriers()
    return WATCHED_CARRIERS


# Loaded once at import time.
WATCHED_CARRIERS: list[dict] = _load_carriers()
