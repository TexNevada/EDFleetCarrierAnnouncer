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

    # ── state transitions ────────────────────────────────────────────────

    def update_location(self, system: str, system_info: Optional[dict] = None) -> Optional[dict]:
        """
        Called when we learn the carrier is *in* a system (not jumping).
        Returns a payload if state changed, otherwise None.
        """
        if self.jumping:
            # Jump just finished – carrier is now at the new system.
            self.jumping = False
            self.current_location = system
            self.destination = None
            return _make_payload(
                self.callsign, "jump_completed", self.current_location,
                name=self.name,
                discord_webhook=self.discord_webhook,
                logo_url=self.logo_url,
                system_info=system_info,
            )

        if system == self.current_location:
            # Duplicate – already know it's here.
            return None

        # Genuine location change (first sighting or moved without us
        # seeing a jump-start, e.g. journal catch-up).
        self.current_location = system
        return _make_payload(
            self.callsign, "location_changed", self.current_location,
            name=self.name,
            discord_webhook=self.discord_webhook,
            logo_url=self.logo_url,
            system_info=system_info,
        )

    def start_jump(self, destination: Optional[str] = None, system_info: Optional[dict] = None) -> Optional[dict]:
        """
        Called when the carrier begins a jump.
        Returns a payload.
        """
        if self.jumping and destination == self.destination:
            # Duplicate jump-start for the same destination.
            return None

        self.jumping = True
        self.destination = destination
        return _make_payload(
            self.callsign,
            "jump_started",
            self.current_location,
            destination=self.destination,
            name=self.name,
            discord_webhook=self.discord_webhook,
            logo_url=self.logo_url,
            system_info=system_info,
        )

    def cancel_jump(self, system_info: Optional[dict] = None) -> Optional[dict]:
        """
        Called when a pending carrier jump is cancelled.
        Returns a payload if the carrier was actually jumping, otherwise None.
        """
        if not self.jumping:
            return None

        self.jumping = False
        dest = self.destination
        self.destination = None
        return _make_payload(
            self.callsign,
            "jump_cancelled",
            self.current_location,
            destination=dest,
            name=self.name,
            discord_webhook=self.discord_webhook,
            logo_url=self.logo_url,
            system_info=system_info,
        )

    def set_location(self, system: str) -> None:
        """Silently update the carrier's known location without emitting
        a payload or consuming jump state.  Used when we want to track
        position but let a later event (CarrierJump) do the announcement."""
        self.current_location = system

    def handle_carrier_location(
        self, system: str, system_info: Optional[dict] = None,
    ) -> Optional[dict]:
        """
        Handle a ``CarrierLocation`` journal event.

        CarrierLocation fires at departure time (~1 min before CarrierJump)
        and has NO rich system data.  We must NOT consume the jumping state
        here — CarrierJump will follow with all the rich fields.

        Special cases while jumping:
        * Destination is the same system (intra-system body change) →
          assume the carrier made it; silently update, let CarrierJump
          announce.
        * Destination is a *different* system but carrier is still in the
          *original* system → rare edge case: unexpected cancellation.
        * Destination is a different system and carrier is now in a third
          system → silently update (let CarrierJump clarify).
        """
        if not self.jumping:
            # Not jumping — normal location tracking.
            return self.update_location(system, system_info=system_info)

        if self.destination == system:
            # Same-system jump (body change) — assume success.
            self.set_location(system)
            return None

        if system == self.current_location:
            # Carrier was supposed to leave but is still here.
            self.jumping = False
            dest = self.destination
            self.destination = None
            return _make_payload(
                self.callsign,
                "jump_unexpected_cancel",
                self.current_location,
                destination=dest,
                name=self.name,
                discord_webhook=self.discord_webhook,
                logo_url=self.logo_url,
                system_info=system_info,
            )

        # Carrier is somewhere unexpected — silently update.
        self.set_location(system)
        return None


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

    def __repr__(self) -> str:
        return f"CarrierRegistry({list(self._carriers.values())})"








