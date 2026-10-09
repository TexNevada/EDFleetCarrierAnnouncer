"""Regression coverage for the resolved journal/webhook review findings.

See docs/JOURNAL_BEHAVIOR_REVIEW.md. All Discord calls are mocked.
"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from carrier_state import CarrierRegistry
from event_cache import EventCache
from journal_parser import JournalTailer, _JournalHistory, _process_line, refresh_from_journal
import listener


CARRIER_ID = 3715894016


def event(name, **fields):
    return {"event": name, "timestamp": "2026-10-09T12:00:00Z", **fields}


class JournalReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "Journal.2026-10-09T120000.01.log"
        self.registry = CarrierRegistry([{
            "callsign": "CCGL", "CarrierID": CARRIER_ID, "last_known_location": "Origin",
        }])
        self.history = _JournalHistory()

    def process(self, name, **fields):
        return _process_line(json.dumps(event(name, **fields)), self.registry, self.history)[0]

    def append(self, *entries, path=None):
        with (path or self.path).open("a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")

    def schedule(self, destination="Destination"):
        return event("CarrierJumpRequest", CarrierID=CARRIER_ID, SystemName=destination,
                     DepartureTime="2026-10-09T12:15:00Z")

    def snapshot(self, system="Origin", **fields):
        return event("CarrierLocation", CarrierID=CARRIER_ID, StarSystem=system, **fields)

    def rotate(self, tailer, *entries, continuation=False):
        name = "Journal.2026-10-09T120000.02.log" if continuation else "Journal.2026-10-09T120500.01.log"
        path = Path(self.temp.name) / name
        self.append(*entries, path=path)
        newer = self.path.stat().st_mtime + 1
        os.utime(path, (newer, newer))
        return [payload for payload, _ in tailer.poll() if payload is not None]

    def test_live_explicit_cancel_never_announces_completion(self):
        self.process("CarrierJumpRequest", CarrierID=CARRIER_ID, SystemName="Destination")
        cancelled = self.process("CarrierJumpCancelled", CarrierID=CARRIER_ID)
        duplicate = self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Origin")
        self.assertEqual(cancelled["event_type"], "jump_cancelled")
        self.assertIsNone(duplicate)
        self.assertFalse(self.registry.get("CCGL").jumping)

    def test_offline_move_on_fresh_start_reports_location_without_inventing_a_jump(self):
        moved = self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Destination")
        docked = self.process("Location", Docked=True, MarketID=CARRIER_ID,
                              StationName="CCGL", StationType="FleetCarrier", StarSystem="Destination")
        self.assertEqual(moved["event_type"], "location_changed")
        self.assertIsNone(docked)

    def test_live_offboard_completion_followed_by_carrier_jump_emits_once(self):
        self.process("CarrierJumpRequest", CarrierID=CARRIER_ID, SystemName="Destination")
        completed = self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Destination")
        duplicate = self.process("CarrierJump", MarketID=CARRIER_ID, StarSystem="Destination")
        self.assertEqual(completed["event_type"], "carrier_arrival")
        self.assertIsNone(duplicate)

    def test_login_location_at_origin_does_not_complete_pending_jump(self):
        self.process("CarrierJumpRequest", CarrierID=CARRIER_ID, SystemName="Destination")
        payload = self.process("Location", Docked=True, StationName="CCGL",
                               StationType="FleetCarrier", MarketID=CARRIER_ID, StarSystem="Origin")
        self.assertIsNone(payload, "A startup/respawn snapshot currently announces jump_completed at Origin")

    def test_relogin_before_departure_does_not_invent_cancellation(self):
        self.append(self.schedule(), event("Shutdown"))
        tailer = JournalTailer(self.registry, self.temp.name)
        tailer.poll()
        payloads = self.rotate(tailer, event("FileHeader", part=1), event("LoadGame"), self.snapshot())
        self.assertEqual([p["event_type"] for p in payloads], ["location_changed"])
        self.assertFalse(self.registry.get("CCGL").jumping)

    def test_same_system_relogin_snapshot_does_not_prove_offline_jump_succeeded(self):
        self.append(self.schedule("Origin"), event("Shutdown"))
        tailer = JournalTailer(self.registry, self.temp.name)
        tailer.poll()
        # An offline cancellation would be absent from this player's journal.
        payloads = self.rotate(tailer, event("FileHeader", part=1), event("LoadGame"), self.snapshot())
        self.assertEqual([p["event_type"] for p in payloads], ["location_changed"])
        self.assertFalse(self.registry.get("CCGL").jumping)

    def test_startup_does_not_publish_already_cancelled_schedule(self):
        self.append(self.schedule(), event("CarrierJumpCancelled", CarrierID=CARRIER_ID))
        # main_loop unconditionally passes every replay payload to announce().
        stop = Mock()
        stop.is_set.return_value = True
        config = [{"callsign": "CCGL", "CarrierID": CARRIER_ID, "last_known_location": "Origin"}]
        with patch.object(listener, "WATCHED_CARRIERS", config), \
                patch.object(listener, "EventCache"), \
                patch.object(listener, "save_carriers"), \
                patch.object(listener, "announce") as announce, \
                patch.object(listener, "_registry", None), \
                patch.object(listener, "_cache", None):
            listener.main_loop(stop, self.temp.name)
        published = [call.args[0]["event_type"] for call in announce.call_args_list]
        self.assertNotIn("jump_started", published, "Startup currently publishes an already-cancelled schedule")

    def test_restart_does_not_publish_old_origin_as_a_new_location(self):
        self.append(self.snapshot(), self.schedule(), self.snapshot("Destination"))
        refresh_from_journal(self.registry, self.temp.name)
        # Simulate reloading the final persisted location on an EDMC restart.
        restarted = CarrierRegistry([{
            "callsign": "CCGL", "CarrierID": CARRIER_ID, "last_known_location": "Destination",
        }])
        payloads = refresh_from_journal(restarted, self.temp.name)[0]
        old_locations = [p for p in payloads if p["event_type"] == "location_changed"]
        self.assertEqual(old_locations, [], "Replay currently publishes Origin with its old timestamp")

    def test_cancel_split_across_writes_is_not_lost(self):
        self.append(self.schedule("Origin"))
        tailer = JournalTailer(self.registry, self.temp.name)
        tailer.poll()
        cancel = json.dumps(event("CarrierJumpCancelled", CarrierID=CARRIER_ID)) + "\n"
        midpoint = len(cancel) // 2
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(cancel[:midpoint])
        tailer.poll()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(cancel[midpoint:])
        self.append(self.snapshot())
        payloads = [p for p, _ in tailer.poll() if p]
        self.assertEqual([p["event_type"] for p in payloads], ["jump_cancelled"],
                         "Partial cancellation is discarded; the snapshot currently announces completion")

    def test_continuation_part_keeps_player_aboard_until_carrier_jump(self):
        self.append(event("Docked", MarketID=CARRIER_ID, StationName="CCGL"),
                    self.schedule(), event("Continued", Part=2))
        tailer = JournalTailer(self.registry, self.temp.name)
        tailer.poll()
        payloads = self.rotate(tailer, event("FileHeader", part=2),
                               self.snapshot("Destination", timestamp="2026-10-09T12:15:00Z"),
                               continuation=True)
        self.assertEqual(payloads, [], "Part rotation resets docking context and announces too early")
        self.assertTrue(self.registry.get("CCGL").jumping)
        self.append(event("CarrierJump", MarketID=CARRIER_ID, StarSystem="Destination"),
                    path=Path(self.temp.name) / "Journal.2026-10-09T120000.02.log")
        completed = [p for p, _ in tailer.poll() if p]
        self.assertEqual([p["event_type"] for p in completed], ["jump_completed"])

    def test_old_announced_event_remains_deduplicated_after_cache_ttl(self):
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        payload = {"callsign": "CCGL", "event_type": "jump_started", "current_location": "Origin",
                   "destination": "Destination", "system_info": {"timestamp": old}}
        path = Path(self.temp.name) / "event_log.log"
        cache = EventCache(str(path))
        cache.record_and_log(payload)
        restarted = EventCache(str(path))
        self.assertTrue(restarted.is_duplicate(payload), "TTL removes dedup evidence while journal replay has no age limit")

    def test_failed_webhook_is_not_marked_successfully_announced(self):
        payload = {"callsign": "CCGL", "event_type": "jump_completed", "current_location": "Destination",
                   "discord_webhook": "https://example.invalid/review-only"}
        cache = EventCache(str(Path(self.temp.name) / "event_log.log"))
        with patch.object(listener.requests, "post", return_value=Mock(status_code=500, text="Failure")) as post, \
                patch.object(listener, "_registry", None), patch.object(listener, "_cache", cache):
            listener.announce(payload)
        post.assert_called_once()
        self.assertFalse(cache.is_duplicate(payload), "HTTP failure is currently persisted as already announced")

    def test_live_origin_report_never_proves_cancellation(self):
        self.process("CarrierJumpRequest", CarrierID=CARRIER_ID, SystemName="Destination",
                     DepartureTime="2026-10-09T12:15:00Z")
        payload = self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Origin",
                               timestamp="2026-10-09T12:30:00Z")
        self.assertIsNone(payload)
        self.assertTrue(self.registry.get("CCGL").jumping)

    def test_destination_report_before_departure_does_not_complete(self):
        self.process("CarrierJumpRequest", CarrierID=CARRIER_ID, SystemName="Destination",
                     DepartureTime="2026-10-09T12:15:00Z")
        payload = self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Destination",
                               timestamp="2026-10-09T12:14:59Z")
        self.assertIsNone(payload)
        self.assertTrue(self.registry.get("CCGL").jumping)

    def test_conflicting_destination_address_cannot_complete_by_name(self):
        self.process("CarrierJumpRequest", CarrierID=CARRIER_ID, SystemName="Destination", SystemAddress=100)
        payload = self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Destination", SystemAddress=200)
        self.assertEqual(payload["event_type"], "carrier_arrival")
        self.assertNotIn("jump_id", payload)
        self.assertFalse(self.registry.get("CCGL").jumping)

    def test_intra_system_jump_requires_body_match(self):
        carrier = self.registry.get("CCGL")
        carrier.set_location("Origin", 100, 1)
        self.process("CarrierJumpRequest", CarrierID=CARRIER_ID, SystemName="Origin", SystemAddress=100,
                     BodyID=2, DepartureTime="2026-10-09T12:15:00Z")
        for fields in ({}, {"BodyID": 1}):
            self.assertIsNone(self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Origin",
                                           SystemAddress=100, timestamp="2026-10-09T12:16:00Z", **fields))
            self.assertTrue(carrier.jumping)
        completed = self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Origin", SystemAddress=100,
                                 BodyID=2, timestamp="2026-10-09T12:16:00Z")
        self.assertEqual(completed["event_type"], "carrier_arrival")

    def test_startup_keeps_partial_cancel_for_live_reader(self):
        self.append(self.schedule("Origin"))
        cancel = json.dumps(event("CarrierJumpCancelled", CarrierID=CARRIER_ID)) + "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(cancel[:25])
        payloads, _, path, offset = refresh_from_journal(self.registry, self.temp.name)
        self.assertEqual(payloads, [])
        self.assertLess(offset, self.path.stat().st_size)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(cancel[25:])
        self.append(self.snapshot(timestamp="2026-10-09T12:16:00Z"))
        tailer = JournalTailer(self.registry, self.temp.name, path, offset)
        payloads = [p for p, _ in tailer.poll() if p]
        self.assertEqual([p["event_type"] for p in payloads], ["jump_cancelled"])

    def test_reader_drains_cancel_from_old_part_before_switching(self):
        self.append(self.schedule("Origin"))
        tailer = JournalTailer(self.registry, self.temp.name)
        tailer.poll()
        self.append(event("CarrierJumpCancelled", CarrierID=CARRIER_ID), event("Continued", Part=2))
        payloads = self.rotate(tailer, event("FileHeader", part=2),
                               self.snapshot(timestamp="2026-10-09T12:16:00Z"), continuation=True)
        self.assertEqual([p["event_type"] for p in payloads], ["jump_cancelled"])

    def test_startup_and_warm_tailer_restore_visits_from_preceding_parts(self):
        self.append(event("FileHeader", part=1),
                    event("FSDJump", StarSystem="Destination", SystemAddress=100,
                          SystemSecurity_Localised="High Security"),
                    self.schedule(), event("Continued", Part=2))
        part2 = Path(self.temp.name) / "Journal.2026-10-09T120000.02.log"
        self.append(event("FileHeader", part=2), path=part2)
        _, _, path, offset = refresh_from_journal(self.registry, self.temp.name)
        tailer = JournalTailer(self.registry, self.temp.name, path, offset)
        self.append(self.snapshot("Destination", SystemAddress=100, timestamp="2026-10-09T12:15:00Z"), path=part2)
        completed = [p for p, _ in tailer.poll() if p][0]
        self.assertEqual(completed["event_type"], "carrier_arrival")
        self.assertEqual(completed["system_info"]["security"], "High Security")

    def test_partial_utf8_record_is_decoded_only_after_newline(self):
        self.append(self.schedule())
        tailer = JournalTailer(self.registry, self.temp.name)
        tailer.poll()
        line = json.dumps(event("CarrierJumpCancelled", CarrierID=CARRIER_ID, Body="日本"),
                          ensure_ascii=False).encode("utf-8") + b"\n"
        cut = line.index("日本".encode("utf-8")) + 1
        with self.path.open("ab") as fh:
            fh.write(line[:cut])
        self.assertEqual(tailer.poll(), [])
        with self.path.open("ab") as fh:
            fh.write(line[cut:])
        self.assertEqual(tailer.poll()[0][0]["event_type"], "jump_cancelled")

    def test_touching_old_journal_does_not_change_active_session(self):
        self.append(self.snapshot())
        older = Path(self.temp.name) / "Journal.2026-10-08T120000.01.log"
        self.append(self.snapshot("Old system"), path=older)
        future = self.path.stat().st_mtime + 100
        os.utime(older, (future, future))
        _, _, path, _ = refresh_from_journal(self.registry, self.temp.name)
        self.assertEqual(path, str(self.path))
        self.assertEqual(self.registry.get("CCGL").current_location, "Origin")

    def test_conflicting_market_id_cannot_update_watched_callsign(self):
        payload = self.process("CarrierJump", StationName="CCGL", MarketID=999, StarSystem="Destination")
        self.assertIsNone(payload)
        self.assertEqual(self.registry.get("CCGL").current_location, "Origin")

    def test_same_file_load_boundary_does_not_credit_an_old_request(self):
        self.process("CarrierJumpRequest", CarrierID=CARRIER_ID, SystemName="Destination")
        self.process("LoadGame", FID="F1")
        payload = self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Destination")
        self.assertEqual(payload["event_type"], "location_changed")
        self.assertFalse(self.registry.get("CCGL").jumping)

    def test_load_game_without_location_is_still_a_startup_snapshot(self):
        self.process("LoadGame", FID="F1")
        self.process("SquadronStartup")
        located = self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Origin")
        self.assertEqual(located["event_type"], "location_changed")
        self.assertNotIn("jump_id", located)
        self.assertIsNone(self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Origin"))
        self.process("Location", StarSystem="Player system", Docked=False)
        broadcast = self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Destination")
        self.assertEqual(broadcast["event_type"], "carrier_arrival")

    def test_active_player_receives_remote_arrival_without_a_request(self):
        self.process("Location", Docked=False, StarSystem="Player system")
        self.process("FSDJump", StarSystem="Destination", SystemAddress=200,
                     SystemSecurity_Localised="High Security", Body="Destination A", BodyType="Star")
        broadcast = self.process("CarrierLocation", CarrierID=CARRIER_ID, SystemAddress=200)
        self.assertEqual(broadcast["event_type"], "carrier_arrival")
        self.assertEqual(broadcast["current_location"], "Destination")
        self.assertEqual(broadcast["system_info"]["security"], "High Security")
        self.assertNotIn("body", broadcast["system_info"])
        self.assertNotIn("jump_id", broadcast)
        self.assertIsNone(self.process("CarrierLocation", CarrierID=CARRIER_ID, SystemAddress=200))

    def test_unknown_session_context_uses_located_once(self):
        self.process("Music")
        located = self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Origin")
        self.assertEqual(located["event_type"], "location_changed")
        self.assertIsNone(self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Origin"))
        self.assertTrue(self.history.initializing)

    def test_relogin_resets_gameplay_and_startup_notification_context(self):
        for boundary in ("LoadGame", "Commander", "Shutdown", "Resurrect", "Fileheader"):
            with self.subTest(boundary=boundary):
                self.process("LoadGame")
                self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Origin")
                self.process("Location", Docked=False)
                self.process("CarrierJumpRequest", CarrierID=CARRIER_ID, SystemName="Destination")
                self.process(boundary, part=1)
                located = self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Origin")
                self.assertEqual(located["event_type"], "location_changed")
                self.assertNotIn("jump_id", located)
                self.assertFalse(self.registry.get("CCGL").jumping)
                self.assertIsNone(self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Origin"))

    def test_live_remote_arrival_keeps_active_context_across_continuation(self):
        self.append(event("Fileheader", part=1), event("LoadGame"),
                    event("Location", Docked=False), event("Continued", Part=2))
        tailer = JournalTailer(self.registry, self.temp.name)
        tailer.poll()
        payloads = self.rotate(tailer, event("Fileheader", part=2),
                               self.snapshot("Destination"), continuation=True)
        self.assertEqual([p["event_type"] for p in payloads], ["carrier_arrival"])

    def test_warm_tailer_does_not_repeat_consumed_startup_snapshot(self):
        self.append(event("Fileheader", part=1), event("LoadGame"), self.snapshot())
        _, _, path, offset = refresh_from_journal(self.registry, self.temp.name)
        tailer = JournalTailer(self.registry, self.temp.name, path, offset)
        self.append(self.snapshot())
        self.assertEqual([p for p, _ in tailer.poll() if p], [])

    def test_broadcast_embed_matches_completion_fields_and_style(self):
        payload = {"callsign": "CCGL", "name": "Carrier", "current_location": "Destination",
                   "logo_url": "https://example.invalid/logo.png",
                   "system_info": {"timestamp": "2026-10-09T12:15:00Z", "body": "Destination B 4",
                                   "security": "High Security", "faction": "Faction",
                                   "government": "Corporate", "allegiance": "Federation",
                                   "controlling_power": "Jerome Archer", "powers": ["Aisling Duval"],
                                   "powerplay_state": "Exploited"}}
        complete = listener._build_embed({**payload, "event_type": "jump_completed"})
        broadcast = listener._build_embed({**payload, "event_type": "carrier_arrival"})
        self.assertIn("Carrier Broadcast: New Arrival Destination", broadcast["title"])
        self.assertEqual({k: v for k, v in broadcast.items() if k != "title"},
                         {k: v for k, v in complete.items() if k != "title"})

    def test_login_snapshot_embed_uses_located_label(self):
        self.process("LoadGame")
        payload = self.process("CarrierLocation", CarrierID=CARRIER_ID, StarSystem="Origin")
        self.assertIn("Carrier has been located", listener._build_embed(payload)["title"])


if __name__ == "__main__":
    unittest.main()
