"""
Journal parser – startup refresh **and** real-time polling.

Discovers the newest Journal.*.log in the configured folder, replays it at
startup to seed CarrierState, then provides a ``JournalTailer`` that can be
polled from the main loop to read new lines in real time.

Relevant journal event types:
  - CarrierJumpRequest  → jump_started  (has Body, SystemName as destination)
  - CarrierJump         → jump_completed / location_changed
  - CarrierLocation     → startup location / live carrier_arrival broadcast
  - CarrierJumpCancelled → jump_cancelled
  - Location / FSDJump  → may mention StationName matching a carrier callsign
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
from typing import Optional

from _logger import logger
from carrier_state import CarrierRegistry, CarrierState



# ── helpers ──────────────────────────────────────────────────────────────────

def _newest_journal(journal_dir: str) -> Optional[str]:
    """Return the full path to the newest Journal.*.log file, or None."""
    pattern = os.path.join(journal_dir, "Journal.*.log")
    files = glob.glob(pattern)
    if not files:
        return None
    # Journal names encode session time and part; touching a copied old file
    # must not make it the active journal.
    files.sort(reverse=True)
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


def _extract_system_info(entry: dict) -> Optional[dict]:
    """
    Pull system-level fields out of a journal entry (CarrierJump, Location,
    FSDJump, etc.) and return them as a flat dict for the payload.
    Values that are None, empty, or the string "Null" are omitted.
    """
    info: dict = {}

    # Timestamp (raw ISO string — the embed builder will format it)
    if "timestamp" in entry and not _is_null(entry["timestamp"]):
        info["timestamp"] = entry["timestamp"]
    if not _is_null(entry.get("DepartureTime")):
        info["departure_time"] = entry["DepartureTime"]

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


def _parse_entry(line: str) -> Optional[dict]:
    """Ignore blank, incomplete, or non-object journal lines."""
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    return entry if isinstance(entry, dict) else None


class _JournalHistory:
    """Latest FSD visits and player docking state in the current journal."""

    def __init__(self) -> None:
        self._by_address: dict[int, dict] = {}
        self._by_name: dict[str, dict] = {}
        self._station_id: Optional[int] = None
        self._station_callsign: Optional[str] = None
        # Missing session context cannot establish that a report is live.
        # LoadGame precedes startup CarrierLocation; Location (or subsequent
        # gameplay) establishes that the player is already in the game.
        self.initializing = True
        self.startup_locations: set[str] = set()
        self.first_startup_location = False
        self.continued_part: Optional[int] = None
        self.fid: Optional[str] = None

    def observe(self, entry: dict) -> bool:
        """Update context and report boundaries that invalidate pending jumps."""
        event = entry.get("event")
        boundary = False
        self.first_startup_location = False
        if event in ("FileHeader", "Fileheader") and entry.get("part", 1) == 1:
            self.__init__()
            boundary = True
        elif event in ("Commander", "LoadGame", "Shutdown", "Resurrect"):
            fid = entry.get("FID")
            if fid is not None and self.fid is not None and fid != self.fid:
                self._by_address.clear()
                self._by_name.clear()
            if fid is not None:
                self.fid = fid
            self._station_id = None
            self._station_callsign = None
            self.initializing = True
            self.startup_locations.clear()
            boundary = True
        elif event == "Continued":
            self.continued_part = entry.get("Part")
        elif event in ("Location", "FSDJump", "Docked", "Undocked", "CarrierJump",
                       "CarrierJumpRequest", "Embark", "Disembark"):
            self.initializing = False
            boundary = event == "Location"
        if event == "CarrierLocation" and self.initializing:
            identity = entry.get("CarrierID") or entry.get("MarketID") or _extract_carrier_callsign(entry)
            if identity is not None:
                key = str(identity)
                self.first_startup_location = key not in self.startup_locations
                self.startup_locations.add(key)
        if event == "FSDJump":
            address = entry.get("SystemAddress")
            name = entry.get("StarSystem")
            if address is not None:
                self._by_address[address] = entry
            if not _is_null(name):
                self._by_name[name.casefold()] = entry

        if event in ("Undocked", "FSDJump"):
            self._station_id = None
            self._station_callsign = None
        elif event in ("Docked", "Location", "CarrierJump"):
            aboard = (event == "Docked" or entry.get("Docked", False)
                      or (entry.get("OnFoot") and entry.get("StationType") == "FleetCarrier"))
            self._station_id = entry.get("MarketID") if aboard else None
            self._station_callsign = _extract_carrier_callsign(entry) if aboard else None
        elif event in ("Embark", "Disembark") and entry.get("OnStation"):
            # Being on foot inside a carrier also counts as being aboard.
            self._station_id = entry.get("MarketID")
            self._station_callsign = _extract_carrier_callsign(entry)
        return boundary

    def player_aboard(self, carrier: CarrierState) -> bool:
        if self._station_id is not None and carrier.carrier_id is not None:
            return self._station_id == carrier.carrier_id
        return self._station_callsign == carrier.callsign

    def find_system(self, entry: dict) -> Optional[dict]:
        """Prefer the unique system address; use names if an address is absent."""
        address = entry.get("SystemAddress")
        if address is not None and address in self._by_address:
            return self._by_address[address]
        name = entry.get("StarSystem")
        previous = self._by_name.get(name.casefold()) if not _is_null(name) else None
        if previous is not None:
            previous_address = previous.get("SystemAddress")
            if address is None or previous_address is None or address == previous_address:
                return previous
        return None


def _process_line(
    line: str,
    registry: CarrierRegistry,
    history: Optional[_JournalHistory] = None,
    source_id: Optional[str] = None,
) -> tuple[Optional[dict], Optional[dict]]:
    """
    Parse a single journal line.

    Returns:
        (payload, raw_entry)
        - payload:   dict if a meaningful state change, else None.
        - raw_entry: the parsed JSON dict if carrier-relevant, else None.
    """
    entry = _parse_entry(line)
    if entry is None:
        return None, None

    if history is not None:
        if history.observe(entry):
            registry.clear_pending_jumps()

    payload, raw = _process_entry(entry, registry, history, source_id)
    if payload is not None and source_id is not None:
        payload["event_id"] = source_id
    return payload, raw


def _process_entry(
    entry: dict, registry: CarrierRegistry, history: Optional[_JournalHistory], source_id: Optional[str],
) -> tuple[Optional[dict], Optional[dict]]:

    event = entry.get("event", "")
    callsign = _extract_carrier_callsign(entry)

    # Some events only have CarrierID (no callsign), so resolve via ID.
    carrier_id = entry.get("CarrierID") or entry.get("MarketID")
    if carrier_id is not None:
        try:
            carrier_id = int(carrier_id)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid carrier/market identity in %s", event)
            return None, None

    def _resolve_carrier():
        """A configured numeric identity takes precedence over station text."""
        if carrier_id is not None and registry.is_watched_id(carrier_id):
            return registry.get_by_id(carrier_id)
        if callsign and registry.is_watched(callsign):
            carrier = registry.get(callsign)
            if carrier_id is None or carrier.carrier_id is None:
                return carrier
        return None

    # ── CarrierJumpRequest → jump_started ────────────────────────────
    if event == "CarrierJumpRequest":
        carrier = _resolve_carrier()
        if carrier:
            dest = entry.get("SystemName")
            sysinfo = _extract_system_info(entry)
            request_id = source_id or hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
            return carrier.start_jump(
                destination=dest, system_info=sysinfo, system_address=entry.get("SystemAddress"),
                body_id=entry.get("BodyID"), departure_time=entry.get("DepartureTime"), request_id=request_id,
            ), entry
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
                return carrier.update_location(
                    system, system_info=sysinfo, system_address=entry.get("SystemAddress"),
                    body_id=entry.get("BodyID"), arrival=True,
                ), entry
        return None, None

    # ── CarrierLocation → arrival, enriched from an earlier FSD visit ──
    if event == "CarrierLocation":
        carrier = _resolve_carrier()
        if carrier:
            previous = history.find_system(entry) if history is not None else None
            system = entry.get("StarSystem")
            if _is_null(system) and previous is not None:
                system = previous.get("StarSystem")
            if not _is_null(system):
                sysinfo = (_extract_system_info(previous) or {}) if previous is not None else {}
                # The FSD timestamp and arrival star belong to the player's
                # visit, not to this carrier arrival or its orbit body.
                for key in ("timestamp", "body", "body_type"):
                    sysinfo.pop(key, None)
                sysinfo.update(_extract_system_info(entry) or {})
                aboard = history.player_aboard(carrier) if history is not None else False
                if history is None or history.initializing:
                    carrier.clear_pending_jump()
                    return carrier.update_location(
                        system, system_info=sysinfo or None, system_address=entry.get("SystemAddress"),
                        body_id=entry.get("BodyID"),
                        report_unchanged=history is not None and history.first_startup_location,
                    ), entry
                payload = carrier.handle_carrier_location(
                    system, system_info=sysinfo or None, player_aboard=aboard,
                    system_address=entry.get("SystemAddress"), body_id=entry.get("BodyID"),
                )
                if payload is not None:
                    # CarrierLocation reports a destination shortly before
                    # arrival; reserve the completion label for CarrierJump.
                    payload["event_type"] = "carrier_arrival"
                return payload, entry
        return None, None

    # ── Location / FSDJump — player may be docked at a carrier ───────
    if event in ("Location", "FSDJump"):
        carrier = _resolve_carrier()
        if entry.get("StationType") == "FleetCarrier" and carrier:
            system = entry.get("StarSystem")
            if system:
                sysinfo = _extract_system_info(entry)
                carrier.clear_pending_jump()
                return carrier.update_location(
                    system, system_info=sysinfo, system_address=entry.get("SystemAddress"),
                    body_id=entry.get("BodyID"),
                ), entry
        return None, None

    return None, None


# ── startup refresh ──────────────────────────────────────────────────────────

def _session_key(path: str) -> str:
    return os.path.basename(path).rsplit(".", 2)[0]


def _part(path: str) -> int:
    return int(os.path.basename(path).rsplit(".", 2)[1])


def _session_parts(path: str, journal_dir: str) -> list[str]:
    """Return available preceding parts of this session, in journal order."""
    return sorted(p for p in glob.glob(os.path.join(journal_dir, "Journal.*.log"))
                  if _session_key(p) == _session_key(path) and p <= path)


def _complete_lines(path: str, offset: int = 0, limit: Optional[int] = None):
    """Read complete UTF-8 records; leave an unfinished EOF line unconsumed."""
    lines = []
    with open(path, "rb") as fh:
        fh.seek(offset)
        while limit is None or fh.tell() < limit:
            start = fh.tell()
            line = fh.readline()
            if not line or not line.endswith(b"\n") or (limit is not None and fh.tell() > limit):
                fh.seek(start)
                break
            try:
                lines.append((start, line.decode("utf-8")))
            except UnicodeDecodeError:
                logger.warning("Invalid UTF-8 record in %s at %d", path, start)
        return lines, fh.tell()


def refresh_from_journal(
    registry: CarrierRegistry, journal_dir: str, *, collect_payloads: bool = False,
) -> tuple[list[dict], list[dict], Optional[str], int]:
    """
    Open the newest journal file (read-only) and replay carrier-relevant
    events into *registry*, including available earlier parts of its session.
    Startup is silent; collect_payloads is for diagnostics/tests only.

    Returns:
        payloads     – empty unless diagnostic collection is requested.
        raw_entries  – list of raw journal entry dicts (for logging).
        path         – full path of the journal file that was read (or None).
        offset       – file position after the last line read (byte offset).
    """
    path = _newest_journal(journal_dir)
    if path is None:
        logger.warning("No journal files found in %s", journal_dir)
        return [], [], None, 0

    logger.info("Reading %s", os.path.basename(path))
    payloads: list[dict] = []
    raw_entries: list[dict] = []
    history = _JournalHistory()

    registry.clear_pending_jumps()
    previous_path = None
    for part_path in _session_parts(path, journal_dir):
        if previous_path is not None and not _continues(previous_path, part_path, history):
            history = _JournalHistory()
            history.initializing = True
            registry.clear_pending_jumps()
        lines, offset = _complete_lines(part_path)
        for position, line in lines:
            payload, raw = _process_line(line, registry, history, f"{os.path.basename(part_path)}:{position}")
            if raw is not None:
                raw_entries.append(raw)
            if collect_payloads and payload is not None:
                payloads.append(payload)
        previous_path = part_path

    return payloads, raw_entries, path, offset


def _continues(previous: str, current: str, history: _JournalHistory) -> bool:
    return (_session_key(previous) == _session_key(current)
            and _part(current) == _part(previous) + 1
            and history.continued_part == _part(current))


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
        self._history = _JournalHistory()
        self._history_ready = False

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

            if self._current_path is None:
                self._current_path = _session_parts(newest, self._journal_dir)[0]

            if not self._history_ready:
                previous_path = None
                for path in _session_parts(self._current_path, self._journal_dir):
                    if previous_path is not None and not _continues(previous_path, path, self._history):
                        self._history = _JournalHistory()
                        self._history.initializing = True
                    limit = self._offset if path == self._current_path else None
                    lines, _ = _complete_lines(path, limit=limit)
                    for _, line in lines:
                        entry = _parse_entry(line)
                        if entry is not None:
                            self._history.observe(entry)
                    previous_path = path
                self._history_ready = True

            # Drain the old file and every intervening part before switching.
            paths = sorted(p for p in glob.glob(os.path.join(self._journal_dir, "Journal.*.log"))
                           if self._current_path <= p <= newest)
            for path in paths:
                if path != self._current_path:
                    continuation = _continues(self._current_path, path, self._history)
                    if continuation and self._offset < os.path.getsize(self._current_path):
                        # The preceding part still has an unfinished record.
                        break
                    if not continuation:
                        self._registry.clear_pending_jumps()
                        self._history = _JournalHistory()
                        self._history.initializing = True
                    self._current_path = path
                    self._offset = 0

                truncated = os.path.getsize(path) < self._offset
                if truncated:
                    self._offset = 0
                    self._registry.clear_pending_jumps()
                    self._history = _JournalHistory()
                    self._history.initializing = True
                lines, next_offset = _complete_lines(path, self._offset)
                for position, line in lines:
                    payload, raw = _process_line(
                        line, self._registry, self._history, f"{os.path.basename(path)}:{position}",
                    )
                    if payload is not None or raw is not None:
                        results.append((None if truncated else payload, raw))
                self._offset = next_offset

        except Exception:
            logger.exception("Tailer error")

        return results
