"""
Fleet Carrier Announcer – journal real-time tail.

1. On startup: refreshes carrier state from the newest journal file.
2. Continuously tails the journal file in real time (and switches to
   newer files when they appear).
Events are emitted through the announce() sink → Discord webhook.
"""

import time
from datetime import datetime
from typing import Optional

import requests

from carrier_state import CarrierRegistry
from fc_config import WATCHED_CARRIERS, save_carriers
from event_cache import EventCache
from journal_parser import JournalTailer, refresh_from_journal


# ── helpers ──────────────────────────────────────────────────────────────────

def _has_value(val) -> bool:
    """Return True if *val* is a real value — filters out None, empty
    strings, and the literal string ``"Null"`` (case-insensitive) that
    Elite Dangerous sometimes returns."""
    if val is None:
        return False
    if isinstance(val, str) and (not val or val.lower() == "null"):
        return False
    if isinstance(val, list) and not val:
        return False
    return True


# ── announce helper ──────────────────────────────────────────────────────────

_EVENT_DESCRIPTIONS = {
    "location_changed":        "📍 Carrier has been located",
    "jump_started":            "🚀 Carrier jump scheduled",
    "jump_cancelled":          "❌ Carrier jump cancelled",
    "jump_unexpected_cancel":  "⚠️ Carrier jump unexpectedly cancelled",
    "jump_completed":          "✅ Carrier Jump complete",
}

_EVENT_COLORS = {
    "location_changed":        0x3498DB,   # blue
    "jump_started":            0xF1C40F,   # yellow
    "jump_cancelled":          0xE74C3C,   # red
    "jump_unexpected_cancel":  0xE67E22,   # orange
    "jump_completed":          0x2ECC71,   # green
}

# Will be set in main() so announce() can persist state.
_registry: Optional[CarrierRegistry] = None
_cache: Optional[EventCache] = None


def _to_discord_timestamp(iso_str: str) -> str:
    """Convert an ISO-8601 timestamp like '2026-03-11T15:07:11Z' to Discord
    format ``<t:EPOCH:f>``."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        epoch = int(dt.timestamp())
        return f"<t:{epoch}:f>"
    except (ValueError, OSError):
        return iso_str


def _build_embed(payload: dict) -> dict:
    """Build a Discord embed dict from a carrier payload.

    Kept intentionally simple and flat so fields are easy to reorder,
    rename, or remove.
    """
    event_type = payload["event_type"]
    title = _EVENT_DESCRIPTIONS.get(event_type, event_type)
    info = payload.get("system_info") or {}

    # ── fields ───────────────────────────────────────────────────────
    fields = []

    if _has_value(payload.get("current_location")):
        fields.append({"name": "📍 Carrier Location", "value": payload["current_location"], "inline": False})

    if _has_value(payload.get("destination")):
        fields.append({"name": "🚀 Destination", "value": payload["destination"], "inline": True})

    if _has_value(info.get("body")):
        body_val = info["body"]
        if _has_value(info.get("body_type")):
            body_val += f"  ({info['body_type']})"
        fields.append({"name": "🪐 Body", "value": body_val, "inline": True})

    if _has_value(info.get("allegiance")):
        fields.append({"name": "🛃 Superpower", "value": info["allegiance"], "inline": True})

    if _has_value(info.get("government")):
        fields.append({"name": "🛂 Government", "value": info["government"], "inline": True})

    if _has_value(info.get("security")):
        fields.append({"name": "🪪 System Security", "value": info["security"], "inline": True})

    if _has_value(info.get("faction")):
        fields.append({"name": "🔰 Local Faction", "value": info["faction"], "inline": True})

    if _has_value(info.get("controlling_power")):
        fields.append({"name": "❇️ Controlling Power", "value": info["controlling_power"], "inline": False})

    if _has_value(info.get("powers")):
        powers = [p for p in info["powers"] if _has_value(p)]
        if powers:
            fields.append({"name": "⚔️ Rival powers fighting for control", "value": ", ".join(powers), "inline": False})

    if _has_value(info.get("powerplay_state")):
        fields.append({"name": "Powerplay State", "value": info["powerplay_state"], "inline": True})

    if _has_value(info.get("timestamp")):
        fields.append({"name": "Log time", "value": _to_discord_timestamp(info["timestamp"]), "inline": False})

    # Estimated arrival: timestamp + 16 minutes (only for scheduled jumps)
    if event_type == "jump_started" and _has_value(info.get("timestamp")):
        try:
            dt = datetime.fromisoformat(info["timestamp"].replace("Z", "+00:00"))
            arrival_epoch = int(dt.timestamp()) + 960  # 16 minutes
            fields.append({"name": "⏱️ Estimated Arrival Time", "value": f"<t:{arrival_epoch}:f>", "inline": False})
        except (ValueError, OSError):
            pass

    # ── embed ────────────────────────────────────────────────────────
    embed: dict = {
        "title": title,
        "color": _EVENT_COLORS.get(event_type, 0x95A5A6),
        "fields": fields,
    }

    logo_url = payload.get("logo_url")
    if logo_url:
        embed["thumbnail"] = {"url": logo_url}

    return embed


def announce(payload: dict) -> None:
    """
    Send a payload to the output sink.
    1. Check the local cache — skip if this event was already announced.
    2. Print to stdout for logging.
    3. POST a rich embed to the carrier's Discord webhook.
    4. Persist the updated location to carriers.json.
    5. Record the event in the cache so it won't be re-posted on restart.
    """
    # ── dedup check ──────────────────────────────────────────────────
    if _cache is not None and _cache.is_duplicate(payload):
        print(f"[cache] Skipping duplicate: {payload.get('callsign')} "
              f"{payload.get('event_type')} @ {payload.get('current_location')}")
        return

    print(payload)

    # ── persist location to carriers.json ────────────────────────────
    if _registry is not None and payload.get("current_location"):
        save_carriers(_registry.to_config_list())

    # ── send Discord webhook ─────────────────────────────────────────
    webhook_url = payload.get("discord_webhook")
    if webhook_url:
        embed = _build_embed(payload)
        body = {"embeds": [embed]}

        try:
            resp = requests.post(webhook_url, json=body, timeout=10)
            if resp.status_code >= 400:
                print(f"[discord] Webhook error {resp.status_code}: {resp.text}")
        except requests.RequestException as exc:
            print(f"[discord] Webhook request failed: {exc}")

    # ── record in cache ──────────────────────────────────────────────
    if _cache is not None:
        _cache.record_and_log(payload)


# ── main ─────────────────────────────────────────────────────────────────────

def main_loop(stop_event=None, journal_dir: Optional[str] = None) -> None:
    """
    Core announcer loop.

    Parameters
    ----------
    stop_event : threading.Event, optional
        When set, the loop exits gracefully.  If *None* the loop runs
        until the process is killed (standalone mode).
    journal_dir : str
        Path to the Elite Dangerous journal folder.  Required — the EDMC
        plugin entry point (load.py) sources this from EDMC's config.
    """
    if not journal_dir:
        raise ValueError("main_loop requires a journal_dir (provided by EDMC via load.py)")

    global _registry, _cache

    # 1. Build the registry of carriers we care about.
    registry = CarrierRegistry(WATCHED_CARRIERS)
    _registry = registry
    print(f"[startup] Tracking carriers: {registry.callsigns}")

    # 2. Load the event cache (prevents re-posting on restart).
    cache = EventCache()
    _cache = cache

    # 3. Seed state from the newest journal file (read-only).
    journal_payloads, journal_raws, journal_path, journal_offset = refresh_from_journal(registry, journal_dir)
    for raw in journal_raws:
        cache.log_raw(raw)
    for p in journal_payloads:
        announce(p)

    print(f"[startup] Carrier states after journal refresh: {registry}")

    # 4. Set up the journal tailer (picks up where startup left off).
    tailer = JournalTailer(
        registry=registry,
        journal_dir=journal_dir,
        start_path=journal_path,
        start_offset=journal_offset,
    )
    print("[journal] Real-time tailer ready — polling for carrier events …")

    # 5. Poll the journal file for new lines.
    while not (stop_event and stop_event.is_set()):
        for payload, raw in tailer.poll():
            if raw is not None and _cache is not None:
                _cache.log_raw(raw)
            if payload is not None:
                announce(payload)

        time.sleep(1)

    print("[shutdown] Fleet Carrier Announcer stopped.")


def main() -> None:
    """Standalone entry point — runs until interrupted."""
    main_loop()


if __name__ == "__main__":
    main()
