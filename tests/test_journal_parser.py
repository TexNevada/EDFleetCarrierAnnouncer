import json
import os
from pathlib import Path
import tempfile
import unittest

from carrier_state import CarrierRegistry
from journal_parser import JournalTailer, refresh_from_journal


CARRIER_ID = 3715894016
SYSTEM = "Core Sys Sector FH-M a7-1"
ADDRESS = 22957136956264


def visit(**overrides):
    entry = {
        "event": "FSDJump", "timestamp": "2026-10-09T11:49:28Z",
        "StarSystem": SYSTEM, "SystemAddress": ADDRESS,
        "Body": SYSTEM + " A", "BodyType": "Star",
        "SystemAllegiance": "Federation",
        "SystemGovernment_Localised": "Corporate",
        "SystemSecurity_Localised": "High Security",
        "SystemFaction": {"Name": "Djabushis Systems"},
        "ControllingPower": "Jerome Archer",
        "Powers": ["Aisling Duval", "Li Yong-Rui", "Yuri Grom", "Jerome Archer"],
        "PowerplayState": "Exploited",
    }
    entry.update(overrides)
    return entry


def request(system=SYSTEM):
    return {
        "event": "CarrierJumpRequest", "timestamp": "2026-10-09T11:52:49Z",
        "CarrierID": CARRIER_ID, "SystemName": system,
        "Body": SYSTEM + " B 4", "SystemAddress": ADDRESS,
    }


def arrival(**overrides):
    entry = {
        "event": "CarrierLocation", "timestamp": "2026-10-09T12:08:10Z",
        "CarrierID": CARRIER_ID, "StarSystem": SYSTEM,
        "SystemAddress": ADDRESS, "BodyID": 6,
    }
    entry.update(overrides)
    return entry


class JournalArrivalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "Journal.2026-10-09T133657.01.log"
        self.registry = CarrierRegistry([{
            "callsign": "CCGL", "CarrierID": CARRIER_ID,
            "last_known_location": "Merope",
        }])

    def append(self, *entries, path=None):
        with (path or self.path).open("a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")

    def replay(self, *entries):
        self.append(*entries)
        return refresh_from_journal(self.registry, self.temp.name, collect_payloads=True)[0]

    def test_offboard_arrival_uses_system_fields_and_its_own_timestamp(self):
        payloads = self.replay(visit(), request(), arrival())
        self.assertEqual([p["event_type"] for p in payloads], ["jump_started", "carrier_arrival"])
        completed = payloads[-1]
        self.assertEqual(completed["current_location"], SYSTEM)
        info = completed["system_info"]
        self.assertEqual(info["timestamp"], "2026-10-09T12:08:10Z")
        self.assertEqual(info["faction"], "Djabushis Systems")
        self.assertEqual(info["allegiance"], "Federation")
        self.assertEqual(info["government"], "Corporate")
        self.assertEqual(info["security"], "High Security")
        self.assertEqual(info["controlling_power"], "Jerome Archer")
        self.assertEqual(info["powers"], visit()["Powers"])
        self.assertEqual(info["powerplay_state"], "Exploited")
        self.assertNotIn("body", info)
        self.assertNotIn("body_type", info)
        self.assertFalse(self.registry.get("CCGL").jumping)
        self.assertIsNone(self.registry.get("CCGL").destination)

    def test_address_only_arrival_recovers_system_name(self):
        info = arrival()
        del info["StarSystem"]
        payload = self.replay(visit(), request(), info)[-1]
        self.assertEqual(payload["event_type"], "carrier_arrival")
        self.assertEqual(payload["current_location"], SYSTEM)

    def test_name_only_arrival_uses_case_insensitive_match(self):
        info = arrival(StarSystem=SYSTEM.lower())
        del info["SystemAddress"]
        self.assertEqual(self.replay(visit(), request(SYSTEM.lower()), info)[-1]["system_info"]["faction"], "Djabushis Systems")

    def test_name_fallback_when_visit_has_no_address(self):
        info = visit()
        del info["SystemAddress"]
        self.assertEqual(self.replay(info, request(), arrival())[-1]["system_info"]["security"], "High Security")

    def test_address_match_takes_precedence_over_name(self):
        payload = self.replay(visit(StarSystem="Earlier name"), request(), arrival())[-1]
        self.assertEqual(payload["system_info"]["faction"], "Djabushis Systems")

    def test_conflicting_addresses_do_not_match_by_name(self):
        info = self.replay(visit(SystemAddress=1), request(), arrival())[-1]["system_info"]
        self.assertEqual(info, {"timestamp": "2026-10-09T12:08:10Z"})

    def test_latest_prior_matching_visit_wins_and_future_visit_is_ignored(self):
        payload = self.replay(
            visit(SystemSecurity_Localised="Low Security"),
            visit(), visit(StarSystem="Other system", SystemAddress=1),
            request(), arrival(), visit(SystemSecurity_Localised="Anarchy"),
        )[-1]
        self.assertEqual(payload["system_info"]["security"], "High Security")

    def test_missing_visit_still_completes_and_later_arrivals_are_duplicates(self):
        payloads = self.replay(request(), arrival(), arrival(), arrival(event="CarrierJump"))
        self.assertEqual([p["event_type"] for p in payloads], ["jump_started", "carrier_arrival"])

    def test_arrival_fields_override_visit_fields(self):
        info = self.replay(visit(), request(), arrival(
            SystemSecurity_Localised="Medium Security", Body=SYSTEM + " B 4", BodyType="Planet",
        ))[-1]["system_info"]
        self.assertEqual(info["security"], "Medium Security")
        self.assertEqual(info["body"], SYSTEM + " B 4")
        self.assertEqual(info["body_type"], "Planet")

    def test_same_system_scheduled_jump_completes(self):
        self.registry.get("CCGL").current_location = SYSTEM
        self.assertEqual(self.replay(visit(), request(), arrival())[-1]["event_type"], "carrier_arrival")

    def test_aboard_waits_for_rich_carrier_jump(self):
        for boarding in (
            {"event": "Docked", "MarketID": CARRIER_ID, "StationName": "CCGL"},
            {"event": "Location", "Docked": True, "MarketID": CARRIER_ID},
            {"event": "Disembark", "OnStation": True, "MarketID": CARRIER_ID},
            {"event": "Docked", "StationName": "CCGL"},
        ):
            with self.subTest(boarding=boarding):
                self.path.write_text("", encoding="utf-8")
                carrier = self.registry.get("CCGL")
                carrier.current_location = "Merope"
                self.append(boarding, request())
                payloads, _, path, offset = refresh_from_journal(self.registry, self.temp.name, collect_payloads=True)
                self.assertEqual(len(payloads), 1)
                tailer = JournalTailer(self.registry, self.temp.name, path, offset)
                self.append(arrival())
                self.assertIsNone(tailer.poll()[0][0])
                self.assertTrue(carrier.jumping)
                self.append(arrival(event="CarrierJump", Body=SYSTEM + " B 4", BodyType="Planet"))
                completed = tailer.poll()[0][0]
                self.assertEqual(completed["event_type"], "jump_completed")
                self.assertEqual(completed["system_info"]["body"], SYSTEM + " B 4")

    def test_cancelled_jump_does_not_complete_on_duplicate_origin_location(self):
        payloads = self.replay(
            request(), {"event": "CarrierJumpCancelled", "CarrierID": CARRIER_ID},
            arrival(StarSystem="Merope", SystemAddress=224644818084),
        )
        self.assertEqual([p["event_type"] for p in payloads], ["jump_started", "jump_cancelled"])

    def test_null_system_fields_are_omitted(self):
        info = self.replay(visit(
            SystemAllegiance="", SystemGovernment_Localised="Null",
            SystemSecurity_Localised=None, SystemFaction={"Name": "null"},
            ControllingPower="Null", Powers=[None, "", "Null"], PowerplayState="",
        ), request(), arrival())[-1]["system_info"]
        self.assertEqual(info, {"timestamp": "2026-10-09T12:08:10Z"})

    def test_undocked_player_and_player_on_other_station_complete_offboard(self):
        for last_event in (
            {"event": "Undocked"}, visit(),
            {"event": "Docked", "MarketID": 99, "StationName": "Other carrier"},
            {"event": "Location", "Docked": False},
        ):
            with self.subTest(last_event=last_event):
                self.path.write_text("", encoding="utf-8")
                payloads = self.replay(
                    {"event": "Docked", "MarketID": CARRIER_ID},
                    last_event, request(), arrival(),
                )
                self.assertEqual(payloads[-1]["event_type"], "carrier_arrival")

    def test_startup_history_is_available_to_live_tailer_without_replaying_state(self):
        self.append(visit(), request())
        _, _, path, offset = refresh_from_journal(self.registry, self.temp.name)
        tailer = JournalTailer(self.registry, self.temp.name, path, offset)
        self.assertEqual(tailer.poll(), [])
        self.append(arrival())
        payload, raw = tailer.poll()[0]
        self.assertEqual(payload["event_type"], "carrier_arrival")
        self.assertEqual(payload["system_info"]["faction"], "Djabushis Systems")
        self.assertEqual(raw, arrival())
        self.assertEqual(tailer.poll(), [])

    def test_live_history_persists_across_polls(self):
        tailer = JournalTailer(self.registry, self.temp.name)
        self.append(visit())
        self.assertEqual(tailer.poll(), [])
        self.append(request())
        self.assertEqual(tailer.poll()[0][0]["event_type"], "jump_started")
        self.append(arrival())
        self.assertEqual(tailer.poll()[0][0]["system_info"]["security"], "High Security")

    def test_journal_rotation_does_not_reuse_old_visits_or_docking_state(self):
        self.append(visit(), {"event": "Docked", "MarketID": CARRIER_ID})
        tailer = JournalTailer(self.registry, self.temp.name)
        tailer.poll()
        new_path = Path(self.temp.name) / "Journal.2026-10-09T140000.01.log"
        self.append(request(), arrival(), path=new_path)
        new_time = self.path.stat().st_mtime + 1
        os.utime(new_path, (new_time, new_time))
        payload = tailer.poll()[-1][0]
        self.assertEqual(payload["event_type"], "carrier_arrival")
        self.assertEqual(payload["system_info"], {"timestamp": "2026-10-09T12:08:10Z"})


if __name__ == "__main__":
    unittest.main()
