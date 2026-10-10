"""Regression for mixed journal filename formats in the friend's EDMC log."""

import json
from pathlib import Path
import tempfile
import unittest

from carrier_state import CarrierRegistry
from journal_parser import JournalTailer, refresh_from_journal


class JournalOrderingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.registry = CarrierRegistry([{"callsign": "XFH-27F", "CarrierID": 3715921920}])

    def write(self, filename, system):
        path = self.directory / filename
        path.write_text(json.dumps({"event": "CarrierLocation", "CarrierID": 3715921920,
                                    "StarSystem": system}) + "\n", encoding="utf-8")
        return path

    def test_startup_selects_current_modern_file_over_older_legacy_file(self):
        self.write("Journal.261001214723.01.log", "Old system")
        current = self.write("Journal.2026-10-10T073649.01.log", "Current system")
        payloads, _, path, _ = refresh_from_journal(self.registry, self.temp.name)
        self.assertEqual(path, str(current))
        self.assertEqual(payloads, [])
        self.assertEqual(self.registry.get("XFH-27F").current_location, "Current system")

    def test_tailer_leaves_old_legacy_file_for_new_modern_file(self):
        self.write("Journal.261001214723.01.log", "Old system")
        _, _, path, offset = refresh_from_journal(self.registry, self.temp.name)
        tailer = JournalTailer(self.registry, self.temp.name, path, offset)
        current = self.directory / "Journal.2026-10-10T073649.01.log"
        entries = [
            {"event": "Fileheader", "part": 1, "language": "Russian/RU"},
            {"event": "Location", "Docked": False, "StarSystem": "Player system"},
            {"event": "CarrierJumpRequest", "CarrierID": 3715921920, "SystemName": "Destination",
             "DepartureTime": "2026-10-10T08:00:00Z"},
            {"event": "CarrierLocation", "CarrierID": 3715921920, "StarSystem": "Destination",
             "timestamp": "2026-10-10T08:00:00Z"},
        ]
        current.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        payloads = [p for p, _ in tailer.poll() if p]
        self.assertEqual([p["event_type"] for p in payloads], ["jump_started", "carrier_arrival"])
        self.assertEqual(self.registry.get("XFH-27F").current_location, "Destination")
        self.assertEqual(tailer.poll(), [])

    def test_newer_legacy_filename_still_wins_by_date(self):
        self.write("Journal.2026-10-09T120000.01.log", "Older modern system")
        current = self.write("Journal.261010120000.01.log", "Newer legacy system")
        _, _, path, _ = refresh_from_journal(self.registry, self.temp.name)
        self.assertEqual(path, str(current))

    def test_all_intervening_sessions_are_drained_in_date_order(self):
        self.write("Journal.2026-10-08T120000.01.log", "First")
        _, _, path, offset = refresh_from_journal(self.registry, self.temp.name)
        tailer = JournalTailer(self.registry, self.temp.name, path, offset)
        self.write("Journal.261009120000.01.log", "Middle")
        self.write("Journal.2026-10-10T120000.01.log", "Last")
        payloads = [p for p, _ in tailer.poll() if p]
        self.assertEqual([p["current_location"] for p in payloads], ["Middle", "Last"])

    def test_state_debug_representation_omits_webhook_credentials(self):
        webhook = "https://example.invalid/private-webhook-token"
        registry = CarrierRegistry([{"callsign": "XFH-27F", "CarrierID": 3715921920,
                                     "discord_webhook": webhook}])
        self.assertNotIn(webhook, repr(registry))
        self.assertNotIn(webhook, repr(registry.get("XFH-27F")))
        self.assertEqual(registry.to_config_list()[0]["discord_webhook"], webhook)

    def test_legacy_continuation_keeps_pending_schedule(self):
        first = self.directory / "Journal.261010120000.01.log"
        first.write_text("".join(json.dumps(e) + "\n" for e in [
            {"event": "Fileheader", "part": 1},
            {"event": "CarrierJumpRequest", "CarrierID": 3715921920, "SystemName": "Destination"},
            {"event": "Continued", "Part": 2},
        ]), encoding="utf-8")
        _, _, path, offset = refresh_from_journal(self.registry, self.temp.name)
        tailer = JournalTailer(self.registry, self.temp.name, path, offset)
        second = self.directory / "Journal.261010120000.02.log"
        second.write_text("".join(json.dumps(e) + "\n" for e in [
            {"event": "Fileheader", "part": 2},
            {"event": "CarrierLocation", "CarrierID": 3715921920, "StarSystem": "Destination"},
        ]), encoding="utf-8")
        payloads = [p for p, _ in tailer.poll() if p]
        self.assertEqual([p["event_type"] for p in payloads], ["carrier_arrival"])
        self.assertIn("jump_id", payloads[0])

    def test_invalid_journal_names_do_not_override_active_file(self):
        current = self.write("Journal.2026-10-10T120000.01.log", "Current")
        self.write("Journal.backup.01.log", "Invalid")
        self.write("Journal.2026-99-99T120000.01.log", "Invalid date")
        self.write("Journal.2026-10-10T120000.bad.log", "Invalid part")
        _, _, path, _ = refresh_from_journal(self.registry, self.temp.name)
        self.assertEqual(path, str(current))


if __name__ == "__main__":
    unittest.main()
