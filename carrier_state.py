"""
Carrier state tracking and payload generation.

Maintains per-callsign state in memory and emits payload dicts only when
the carrier's situation meaningfully changes:
  - location_changed  – carrier arrived in a new system
  - jump_started      – carrier jump is in progress
  - jump_completed    – carrier jump finished (location updated)
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


# ── payload helpers ──────────────────────────────────────────────────────────

def _make_payload(
    callsign: str,
    event_type: str,
    current_location: Optional[str],
    destination: Optional[str] = None,
    name: Optional[str] = None,
    discord_webhook: Optional[str] = None,
    logo_url: Optional[str] = None,
    system_info: Optional[dict] = None,
    jump_id: Optional[str] = None,
) -> dict:
    """Build a normalised payload dict."""
    payload: dict = {
        "callsign": callsign,
        "name": name,
        "event_type": event_type,
        "current_location": current_location,
    }
    if destination is not None:
        payload["destination"] = destination
    if discord_webhook is not None:
        payload["discord_webhook"] = discord_webhook
    if logo_url is not None:
        payload["logo_url"] = logo_url
    if system_info is not None:
        payload["system_info"] = system_info
    if jump_id is not None:
        payload["jump_id"] = jump_id
    return payload


# ── per-carrier state ────────────────────────────────────────────────────────

@dataclass
class CarrierState:
    callsign: str
    name: Optional[str] = None
    carrier_id: Optional[int] = None
    discord_webhook: Optional[str] = None
    logo_url: Optional[str] = None
    current_location: Optional[str] = None
    jumping: bool = False
    destination: Optional[str] = None
    current_system_address: Optional[int] = None
    current_body_id: Optional[int] = None
    destination_address: Optional[int] = None
    destination_body_id: Optional[int] = None
    departure_time: Optional[str] = None
    jump_id: Optional[str] = None

    # ── state transitions ────────────────────────────────────────────────

    def clear_pending_jump(self) -> None:
        """Forget an unresolved schedule without claiming success or cancellation."""
        self.jumping = False
        self.destination = None
        self.destination_address = None
        self.destination_body_id = None
        self.departure_time = None
        self.jump_id = None

    def matches_destination(self, system: str, address: Optional[int], body_id: Optional[int]) -> bool:
        if self.destination_address is not None and address is not None:
            matches = self.destination_address == address
        else:
            matches = bool(self.destination and self.destination.casefold() == system.casefold())
        if not matches:
            return False
        if self.destination_body_id is not None and body_id is not None:
            return self.destination_body_id == body_id
        # A system alone cannot confirm an intra-system body move.
        if self.current_system_address is not None and address is not None:
            same_system = self.current_system_address == address
        else:
            same_system = self.current_location and self.current_location.casefold() == system.casefold()
        return not (same_system and self.destination_body_id is not None and body_id is None)

    def departure_reached(self, timestamp: Optional[str]) -> bool:
        if not self.departure_time:
            return True
        try:
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00")) >= datetime.fromisoformat(
                self.departure_time.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            return False

    def update_location(
        self, system: str, system_info: Optional[dict] = None, *,
        system_address: Optional[int] = None, body_id: Optional[int] = None,
        arrival: bool = False, report_unchanged: bool = False,
    ) -> Optional[dict]:
        """Reconcile a position; only a matching observed arrival completes a jump."""
        completed = arrival and self.jumping and self.matches_destination(system, system_address, body_id)
        jump_id = self.jump_id if completed else None
        changed = self.current_location is None or self.current_location.casefold() != system.casefold()
        changed = changed or (system_address is not None and self.current_system_address is not None
                              and system_address != self.current_system_address)
        changed = changed or (body_id is not None and self.current_body_id is not None
                              and body_id != self.current_body_id)
        if completed or (arrival and changed):
            self.clear_pending_jump()
        self.set_location(system, system_address, body_id)
        if not completed and not changed and not report_unchanged:
            return None
        return _make_payload(
            self.callsign, "jump_completed" if completed else "location_changed", self.current_location,
            name=self.name,
            discord_webhook=self.discord_webhook,
            logo_url=self.logo_url,
            system_info=system_info,
            jump_id=jump_id,
        )

    def start_jump(
        self, destination: Optional[str] = None, system_info: Optional[dict] = None, *,
        system_address: Optional[int] = None, body_id: Optional[int] = None,
        departure_time: Optional[str] = None, request_id: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Called when the carrier begins a jump.
        Returns a payload.
        """
        if self.jumping and request_id == self.jump_id and destination == self.destination:
            # Duplicate jump-start for the same destination.
            return None

        self.jumping = True
        self.destination = destination
        self.destination_address = system_address
        self.destination_body_id = body_id
        self.departure_time = departure_time
        self.jump_id = request_id
        return _make_payload(
            self.callsign,
            "jump_started",
            self.current_location,
            destination=self.destination,
            name=self.name,
            discord_webhook=self.discord_webhook,
            logo_url=self.logo_url,
            system_info=system_info,
            jump_id=self.jump_id,
        )

    def cancel_jump(self, system_info: Optional[dict] = None) -> Optional[dict]:
        """
        Called when a pending carrier jump is cancelled.
        Returns a payload if the carrier was actually jumping, otherwise None.
        """
        if not self.jumping:
            return None

        dest = self.destination
        jump_id = self.jump_id
        self.clear_pending_jump()
        return _make_payload(
            self.callsign,
            "jump_cancelled",
            self.current_location,
            destination=dest,
            name=self.name,
            discord_webhook=self.discord_webhook,
            logo_url=self.logo_url,
            system_info=system_info,
            jump_id=jump_id,
        )

    def set_location(self, system: str, system_address: Optional[int] = None, body_id: Optional[int] = None) -> None:
        """Silently update the carrier's known location without emitting
        a payload or consuming jump state.  Used when we want to track
        position but let a later event (CarrierJump) do the announcement."""
        if (self.current_location is None or self.current_location.casefold() != system.casefold()
                or (system_address is not None and self.current_system_address is not None
                    and system_address != self.current_system_address)):
            self.current_system_address = None
            self.current_body_id = None
        self.current_location = system
        if system_address is not None:
            self.current_system_address = system_address
        if body_id is not None:
            self.current_body_id = body_id

    def handle_carrier_location(
        self, system: str, system_info: Optional[dict] = None,
        player_aboard: bool = False,
        *, system_address: Optional[int] = None, body_id: Optional[int] = None,
    ) -> Optional[dict]:
        """
        Handle a ``CarrierLocation`` journal event.

        Startup snapshots are reconciled by the parser separately. A live
        report can complete a matching schedule after its departure time.
        Remaining at the origin never proves cancellation.
        """
        if not self.jumping:
            # Not jumping — normal location tracking.
            return self.update_location(system, system_info=system_info,
                                        system_address=system_address, body_id=body_id)

        if self.matches_destination(system, system_address, body_id):
            if not self.departure_reached((system_info or {}).get("timestamp")):
                return None
            if not player_aboard:
                return self.update_location(system, system_info=system_info,
                                            system_address=system_address, body_id=body_id, arrival=True)
            # Includes intra-system body changes while the player is aboard.
            self.set_location(system, system_address, body_id)
            return None

        return self.update_location(system, system_info=system_info,
                                    system_address=system_address, body_id=body_id, arrival=True)


# ── registry of all tracked carriers ────────────────────────────────────────

class CarrierRegistry:
    """Holds CarrierState objects keyed by callsign, with CarrierID index."""

    def __init__(self, carriers: list[dict]) -> None:
        """
        Parameters
        ----------
        carriers : list of dicts from carriers.json.
        """
        self._carriers: dict[str, CarrierState] = {}
        self._id_to_callsign: dict[int, str] = {}
        self._raw_config: list[dict] = carriers  # keep original for saving
        for entry in carriers:
            cs = entry["callsign"].upper()
            name = entry.get("name")
            webhook = entry.get("discord_webhook")
            logo_url = entry.get("logo_url")
            last_loc = entry.get("last_known_location")
            # CarrierID may be stored as string or int in JSON
            raw_id = entry.get("CarrierID") or entry.get("carrier_id")
            carrier_id = int(raw_id) if raw_id is not None else None
            self._carriers[cs] = CarrierState(
                callsign=cs, name=name, carrier_id=carrier_id,
                discord_webhook=webhook, logo_url=logo_url,
                current_location=last_loc,
            )
            if carrier_id is not None:
                self._id_to_callsign[carrier_id] = cs

    @property
    def callsigns(self) -> list[str]:
        return list(self._carriers.keys())

    @property
    def carrier_ids(self) -> list[int]:
        return list(self._id_to_callsign.keys())

    def get(self, callsign: str) -> Optional[CarrierState]:
        return self._carriers.get(callsign.upper())

    def get_by_id(self, carrier_id: int) -> Optional[CarrierState]:
        """Look up a carrier by its numeric CarrierID."""
        cs = self._id_to_callsign.get(carrier_id)
        if cs is not None:
            return self._carriers.get(cs)
        return None

    def is_watched(self, callsign: str) -> bool:
        return callsign.upper() in self._carriers

    def is_watched_id(self, carrier_id: int) -> bool:
        return carrier_id in self._id_to_callsign

    def clear_pending_jumps(self) -> None:
        for carrier in self._carriers.values():
            carrier.clear_pending_jump()

    def to_config_list(self) -> list[dict]:
        """Return a list of dicts suitable for writing back to carriers.json."""
        result: list[dict] = []
        for raw in self._raw_config:
            cs = raw["callsign"].upper()
            entry = dict(raw)  # preserve all original keys
            carrier = self._carriers.get(cs)
            if carrier and carrier.current_location:
                entry["last_known_location"] = carrier.current_location
            result.append(entry)
        return result

    def replace_contents(self, carriers: list[dict]) -> None:
        """
        Rebuild the registry from a fresh carriers list — used when the
        EDFCA settings tab is saved so changes apply without restarting EDMC.

        In-memory jump state (``jumping``, ``destination``, and a
        ``current_location`` that's newer than the saved one) is preserved
        for callsigns that survive the edit.
        """
        new_carriers: dict[str, CarrierState] = {}
        new_id_map: dict[int, str] = {}
        for entry in carriers:
            cs = entry["callsign"].upper()
            raw_id = entry.get("CarrierID") or entry.get("carrier_id")
            carrier_id = int(raw_id) if raw_id is not None else None
            state = CarrierState(
                callsign=cs,
                name=entry.get("name"),
                carrier_id=carrier_id,
                discord_webhook=entry.get("discord_webhook"),
                logo_url=entry.get("logo_url"),
                current_location=entry.get("last_known_location"),
            )
            old = self._carriers.get(cs)
            if old is not None:
                state.jumping = old.jumping
                state.destination = old.destination
                state.destination_address = old.destination_address
                state.destination_body_id = old.destination_body_id
                state.departure_time = old.departure_time
                state.jump_id = old.jump_id
                state.current_system_address = old.current_system_address
                state.current_body_id = old.current_body_id
                if old.carrier_id != carrier_id:
                    state.clear_pending_jump()
                if old.current_location:
                    state.current_location = old.current_location
            new_carriers[cs] = state
            if carrier_id is not None:
                new_id_map[carrier_id] = cs

        # Reference reassignment is atomic in CPython — the listener thread
        # may observe a brief mismatch between the two maps, but won't crash.
        self._raw_config = carriers
        self._carriers = new_carriers
        self._id_to_callsign = new_id_map

    def __repr__(self) -> str:
        return f"CarrierRegistry({list(self._carriers.values())})"



