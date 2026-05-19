"""
Journal parser – startup refresh **and** real-time polling.

Discovers the newest Journal.*.log in the configured folder, replays it at
startup to seed CarrierState, then provides a ``JournalTailer`` that can be
polled from the main loop to read new lines in real time.

Relevant journal event types:
  - CarrierJumpRequest  → jump_started  (has Body, SystemName as destination)
  - CarrierJump         → jump_completed / location_changed
  - CarrierLocation     → silent position update (let CarrierJump announce)
  - CarrierJumpCancelled → jump_cancelled
  - Location / FSDJump  → may mention StationName matching a carrier callsign
"""

from __future__ import annotations

import glob
import json
import os
from typing import Optional

from carrier_state import CarrierRegistry



# ── helpers ──────────────────────────────────────────────────────────────────

def _newest_journal(journal_dir: str) -> Optional[str]:
    """Return the full path to the newest Journal.*.log file, or None."""
    pattern = os.path.join(journal_dir, "Journal.*.log")
    files = glob.glob(pattern)
    if not files:
        return None
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]


def _extract_carrier_callsign(entry: dict) -> Optional[str]:
    """
    Try to pull a carrier callsign out of a journal entry.

    Fleet-carrier-specific events (CarrierJumpRequest, CarrierJump, etc.)
    do not always include the callsign directly in older journal versions,
    but newer ones include 'StationName' which *is* the callsign.
    """
    for key in ("StationName", "Callsign"):
        val = entry.get(key)
        if val:
            return val.upper()
    return None


def _is_null(val) -> bool:
    """Return True if *val* is None, empty, or the literal string 'Null'."""
    if val is None:
        return True
    if isinstance(val, str) and (not val or val.lower() == "null"):
        return True
    return False


def _extract_system_info(entry: dict) -> dict:
    """
    Pull system-level fields out of a journal entry (CarrierJump, Location,
    FSDJump, etc.) and return them as a flat dict for the payload.
    Values that are None, empty, or the string "Null" are omitted.
    """
    info: dict = {}

    # Timestamp (raw ISO string — the embed builder will format it)
    if "timestamp" in entry and not _is_null(entry["timestamp"]):
        info["timestamp"] = entry["timestamp"]

    # Body the carrier orbits
    if "Body" in entry and not _is_null(entry["Body"]):
        info["body"] = entry["Body"]
    if "BodyType" in entry and not _is_null(entry["BodyType"]):
        info["body_type"] = entry["BodyType"]

    # Security / Government / Allegiance
    if "SystemSecurity_Localised" in entry and not _is_null(entry["SystemSecurity_Localised"]):
        info["security"] = entry["SystemSecurity_Localised"]
    if "SystemGovernment_Localised" in entry and not _is_null(entry["SystemGovernment_Localised"]):
        info["government"] = entry["SystemGovernment_Localised"]
    if "SystemAllegiance" in entry and not _is_null(entry["SystemAllegiance"]):
        info["allegiance"] = entry["SystemAllegiance"]

    # Controlling faction
    sys_faction = entry.get("SystemFaction")
    if isinstance(sys_faction, dict) and not _is_null(sys_faction.get("Name")):
        info["faction"] = sys_faction["Name"]
    elif isinstance(sys_faction, str) and not _is_null(sys_faction):
        info["faction"] = sys_faction

    # Powerplay
    if "ControllingPower" in entry and not _is_null(entry["ControllingPower"]):
        info["controlling_power"] = entry["ControllingPower"]
    if "Powers" in entry and isinstance(entry["Powers"], list):
        powers = [p for p in entry["Powers"] if not _is_null(p)]
        if powers:
            info["powers"] = powers
    if "PowerplayState" in entry and not _is_null(entry["PowerplayState"]):
        info["powerplay_state"] = entry["PowerplayState"]

    return info if info else None


def _process_line(
    line: str,
    registry: CarrierRegistry,
) -> tuple[Optional[dict], Optional[dict]]:
    """
    Parse a single journal line.

    Returns:
        (payload, raw_entry)
        - payload:   dict if a meaningful state change, else None.
        - raw_entry: the parsed JSON dict if carrier-relevant, else None.
    """
    line = line.strip()
    if not line:
        return None, None
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None, None

    event = entry.get("event", "")
    callsign = _extract_carrier_callsign(entry)

    # Some events only have CarrierID (no callsign), so resolve via ID.
    carrier_id = entry.get("CarrierID") or entry.get("MarketID")
    if carrier_id is not None:
        carrier_id = int(carrier_id)

    def _resolve_carrier():
        """Try callsign first, then fall back to CarrierID."""
        if callsign and registry.is_watched(callsign):
            return registry.get(callsign)
        if carrier_id is not None and registry.is_watched_id(carrier_id):
            return registry.get_by_id(carrier_id)
        return None

    # ── CarrierJumpRequest → jump_started ────────────────────────────
    if event == "CarrierJumpRequest":
        carrier = _resolve_carrier()
        if carrier:
            dest = entry.get("SystemName")
            sysinfo = _extract_system_info(entry)
            return carrier.start_jump(destination=dest, system_info=sysinfo), entry
        return None, None

    # ── CarrierJumpCancelled → jump_cancelled ────────────────────────
    if event == "CarrierJumpCancelled":
        carrier = _resolve_carrier()
        if carrier:
            sysinfo = _extract_system_info(entry)
            return carrier.cancel_jump(system_info=sysinfo), entry
        return None, None

    # ── CarrierJump → carrier arrived at new system ──────────────────
    if event == "CarrierJump":
        carrier = _resolve_carrier()
        if carrier:
            system = entry.get("StarSystem")
            if system:
                sysinfo = _extract_system_info(entry)
                return carrier.update_location(system, system_info=sysinfo), entry
        return None, None

    # ── CarrierLocation → silent position update (let CarrierJump announce)
    if event == "CarrierLocation":
        carrier = _resolve_carrier()
        if carrier:
            system = entry.get("StarSystem")
            if system:
                sysinfo = _extract_system_info(entry)
                return carrier.handle_carrier_location(system, system_info=sysinfo), entry
        return None, None

    # ── Location / FSDJump — player may be docked at a carrier ───────
    if event in ("Location", "FSDJump"):
        station = entry.get("StationName", "")
        station_type = entry.get("StationType", "")
        if station_type == "FleetCarrier" and registry.is_watched(station):
            system = entry.get("StarSystem")
            if system:
                sysinfo = _extract_system_info(entry)
                return registry.get(station).update_location(system, system_info=sysinfo), entry
        return None, None

    return None, None


# ── startup refresh ──────────────────────────────────────────────────────────

def refresh_from_journal(registry: CarrierRegistry, journal_dir: str) -> tuple[list[dict], list[dict], Optional[str], int]:
    """
    Open the newest journal file (read-only) and replay carrier-relevant
    events into *registry*.

    Returns:
        payloads     – list of payload dicts that would have been emitted.
        raw_entries  – list of raw journal entry dicts (for logging).
        path         – full path of the journal file that was read (or None).
        offset       – file position after the last line read (byte offset).
    """
    path = _newest_journal(journal_dir)
    if path is None:
        print(f"[journal] No journal files found in {journal_dir}")
        return [], [], None, 0

    print(f"[journal] Reading {os.path.basename(path)}")
    payloads: list[dict] = []
    raw_entries: list[dict] = []

    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            payload, raw = _process_line(line, registry)
            if raw is not None:
                raw_entries.append(raw)
            if payload is not None:
                payloads.append(payload)
        offset = fh.tell()

    return payloads, raw_entries, path, offset


# ── real-time journal tailer (polled from main loop) ─────────────────────────

class JournalTailer:
    """
    Tails the active journal file synchronously.

    Call ``poll()`` from the main loop to read any new lines since the last
    call.  Automatically detects when a newer journal file appears (game
    restart / new session) and switches to it.
    """

    def __init__(
        self,
        registry: CarrierRegistry,
        journal_dir: str,
        start_path: Optional[str] = None,
        start_offset: int = 0,
    ) -> None:
        self._registry = registry
        self._journal_dir = journal_dir
        self._current_path = start_path
        self._offset = start_offset

    def poll(self) -> list[tuple[Optional[dict], Optional[dict]]]:
        """
        Read any new journal lines and return a list of (payload, raw_entry)
        tuples.  Returns an empty list if there is nothing new.
        """
        results: list[tuple[Optional[dict], Optional[dict]]] = []

        try:
            # Check if a newer journal file has appeared.
            newest = _newest_journal(self._journal_dir)
            if newest is None:
                return results

            if newest != self._current_path:
                if self._current_path is not None:
                    print(f"[journal] New journal detected: {os.path.basename(newest)}")
                self._current_path = newest
                self._offset = 0

            # Read any new lines appended since our last offset.
            with open(self._current_path, "r", encoding="utf-8") as fh:
                fh.seek(self._offset)
                new_lines = fh.readlines()
                self._offset = fh.tell()

            for line in new_lines:
                payload, raw = _process_line(line, self._registry)
                if payload is not None or raw is not None:
                    results.append((payload, raw))

        except Exception as exc:
            print(f"[journal] Tailer error: {exc}")

        return results





