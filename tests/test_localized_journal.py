"""Carrier sequences from the Russian journal supplied on 2026-10-10.

The reduced fixture keeps only parser-relevant fields and event boundaries.
Commander, financial, inventory, and chat data are excluded. Original lines:
1, 4, 6, 12, 15, 31, 2462, 2617, 2623, 2627, 2634, 2700, 2702, 2704,
2707, 2722, 2793, 2816, 2821, 2880, 2887, 2912, 2961, 2972, 3229, 3314, 3389.
"""

import json
from pathlib import Path
import tempfile
import unittest

from carrier_state import CarrierRegistry
from journal_parser import JournalTailer, refresh_from_journal
from listener import _build_embed


FIXTURE = Path(__file__).parent / "fixtures" / "russian_carrier_session.jsonl"
FLEET = {"callsign": "XFH-27F", "CarrierID": 3715921920}
SQUADRON = {"callsign": "CCGL", "CarrierID": 3715894016}


class LocalizedJournalTests(unittest.TestCase):
    def setUp(self):
        self.entries = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]

    def read_live(self, config, entries=None):
        entries = self.entries if entries is None else entries
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Journal.2026-10-10T043644.01.log"
            # Native Unicode and Windows CRLF exercise the actual byte reader.
            lines = [(json.dumps(e, ensure_ascii=False) + "\r\n").encode("utf-8") for e in entries]
            path.write_bytes(b"".join(lines[:5]))
            registry = CarrierRegistry(config)
            startup, _, start_path, offset = refresh_from_journal(registry, directory)
            self.assertEqual(startup, [])
            tailer = JournalTailer(registry, directory, start_path, offset)
            self.assertEqual(tailer.poll(), [])
            payloads = []
            for line in lines[5:]:
                with path.open("ab") as stream:
                    stream.write(line)
                payloads.extend(p for p, _ in tailer.poll() if p is not None)
            self.assertEqual(tailer.poll(), [])
            return payloads, registry

    def test_all_four_live_jumps_are_detected_with_numeric_identity(self):
        payloads, registry = self.read_live([FLEET])
        self.assertEqual([p["event_type"] for p in payloads], [
            "jump_started", "jump_completed", "location_changed", "jump_started",
            "carrier_arrival", "jump_started", "jump_completed", "jump_started", "carrier_arrival",
        ])
        arrivals = [p for p in payloads if p["event_type"] in ("jump_completed", "carrier_arrival")]
        self.assertEqual([p["current_location"] for p in arrivals], [
            "HR 5518", "Praea Euq PY-Y b1-0", "HR 5307", "Skeggiko O",
        ])
        self.assertFalse(registry.get("XFH-27F").jumping)

    def test_russian_display_values_render_in_completion_embeds(self):
        payloads, _ = self.read_live([FLEET])
        completions = [p for p in payloads if p["event_type"] == "jump_completed"]
        for payload, government in zip(completions, ("Диктатура", "Демократия")):
            with self.subTest(system=payload["current_location"]):
                embed = _build_embed(payload)
                fields = {field["name"]: field["value"] for field in embed["fields"]}
                self.assertIn("Carrier Jump complete", embed["title"])
                self.assertEqual(fields["🛂 Government"], government)
                self.assertEqual(fields["🪪 System Security"], "Высок. ур. безопасности")
                self.assertEqual(fields["🪐 Body"].split("  (")[0], payload["system_info"]["body"])

    def test_missing_carrier_id_reproduces_missing_schedule_and_broadcast(self):
        payloads, _ = self.read_live([{"callsign": "XFH-27F"}])
        self.assertEqual([p["event_type"] for p in payloads], ["location_changed", "location_changed"])
        self.assertEqual([p["current_location"] for p in payloads], ["HR 5518", "HR 5307"])

    def test_squadron_identity_does_not_receive_other_carriers_jumps(self):
        payloads, _ = self.read_live([SQUADRON])
        self.assertEqual([p["event_type"] for p in payloads], ["location_changed"])
        self.assertEqual(payloads[0]["current_location"], "Eta Cassiopeiae")

    def test_absent_destination_visits_leave_offboard_system_details_unavailable(self):
        payloads, _ = self.read_live([FLEET])
        broadcasts = [p for p in payloads if p["event_type"] == "carrier_arrival"]
        self.assertEqual(len(broadcasts), 2)
        for payload in broadcasts:
            self.assertEqual(set(payload["system_info"]), {"timestamp"})
            self.assertIn("Carrier Broadcast: New Arrival Destination", _build_embed(payload)["title"])

    def test_display_language_does_not_change_detected_movements(self):
        translated = json.loads(json.dumps(self.entries))
        for entry in translated:
            if "language" in entry:
                entry["language"] = "English/UK"
            if "SystemGovernment_Localised" in entry:
                entry["SystemGovernment_Localised"] = "English government display"
            if "SystemSecurity_Localised" in entry:
                entry["SystemSecurity_Localised"] = "English security display"
        russian, _ = self.read_live([FLEET])
        english, _ = self.read_live([FLEET], translated)
        def movements(payloads):
            return [(p["event_type"], p["current_location"], p.get("destination")) for p in payloads]
        self.assertEqual(movements(russian), movements(english))


if __name__ == "__main__":
    unittest.main()
