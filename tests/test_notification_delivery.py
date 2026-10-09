from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from carrier_state import CarrierRegistry
from event_cache import EventCache
import listener
from journal_parser import JournalTailer


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "events.log")
        self.cache = EventCache(self.path)
        self.payload = {
            "callsign": "CCGL", "event_type": "jump_completed", "current_location": "Destination",
            "jump_id": "request-1", "event_id": "journal:100",
            "discord_webhook": "https://example.invalid/mock",
        }
        for name, value in (("_registry", None), ("_cache", self.cache)):
            patcher = patch.object(listener, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def response(self, status=204, **fields):
        return Mock(status_code=status, headers=fields.get("headers", {}),
                    json=Mock(return_value=fields.get("body", {})))

    def test_success_is_deduplicated_across_restart(self):
        with patch.object(listener.requests, "post", return_value=self.response()) as post:
            listener.announce(self.payload)
            listener._cache = EventCache(self.path)
            listener.announce(self.payload)
        post.assert_called_once()
        self.assertTrue(listener._cache.is_duplicate(self.payload))
        self.assertEqual(listener._cache.due(), [])

    def test_failed_request_is_retried_after_restart_and_backoff(self):
        with patch("event_cache.time.time", return_value=100), \
                patch.object(listener.requests, "post", return_value=self.response(500)) as post:
            listener.announce(self.payload)
            listener._cache = EventCache(self.path)
            listener.flush_pending()
        post.assert_called_once()
        self.assertFalse(listener._cache.is_duplicate(self.payload))
        with patch("event_cache.time.time", return_value=102), \
                patch.object(listener.requests, "post", return_value=self.response()) as post:
            listener.flush_pending()
        post.assert_called_once()
        self.assertTrue(listener._cache.is_duplicate(self.payload))

    def test_timeout_keeps_delivery_pending(self):
        with patch.object(listener.requests, "post", side_effect=requests.Timeout):
            listener.announce(self.payload)
        self.assertFalse(self.cache.is_duplicate(self.payload))
        self.assertEqual(len(EventCache(self.path)._pending), 1)

    def test_rate_limit_blocks_later_messages_on_the_same_webhook(self):
        next_payload = {**self.payload, "jump_id": "request-2", "event_id": "journal:200"}
        self.cache.queue(self.payload)
        self.cache.queue(next_payload)
        with patch("event_cache.time.time", return_value=100), \
                patch.object(listener.requests, "post", return_value=self.response(429, headers={"Retry-After": "10"})) as post:
            listener.flush_pending()
        post.assert_called_once()
        self.assertEqual(self.cache.due(now=109), [])
        with patch("event_cache.time.time", return_value=110), \
                patch.object(listener.requests, "post", return_value=self.response()) as post:
            listener.flush_pending()
        self.assertEqual(post.call_count, 2)

    def test_rate_limit_body_retry_after_is_respected(self):
        with patch("event_cache.time.time", return_value=100), \
                patch.object(listener.requests, "post", return_value=self.response(429, body={"retry_after": 15})):
            listener.announce(self.payload)
        self.assertEqual(self.cache.due(now=114), [])
        self.assertEqual(len(self.cache.due(now=115)), 1)

    def test_cancel_supersedes_failed_unsent_schedule(self):
        scheduled = {**self.payload, "event_type": "jump_started", "destination": "Destination"}
        with patch.object(listener.requests, "post", return_value=self.response(500)):
            listener.announce(scheduled)
        cancelled = {**self.payload, "event_type": "jump_cancelled"}
        with patch.object(listener.requests, "post", return_value=self.response()) as post:
            listener.announce(cancelled)
        post.assert_called_once()
        self.assertIn("cancelled", post.call_args.kwargs["json"]["embeds"][0]["title"])
        self.assertEqual(self.cache._pending, {})

    def test_completion_supersedes_queued_schedule_before_batch_delivery(self):
        scheduled = {**self.payload, "event_type": "jump_started", "destination": "Destination"}
        listener.announce(scheduled, deliver=False)
        listener.announce(self.payload, deliver=False)
        with patch.object(listener.requests, "post", return_value=self.response()) as post:
            listener.flush_pending()
        post.assert_called_once()
        self.assertEqual(self.cache._pending, {})

    def test_alternative_arrival_records_share_a_jump_identity(self):
        self.cache.record_and_log(self.payload)
        later = {**self.payload, "event_id": "journal:300", "system_info": {"timestamp": "2026-10-09T12:16:00Z"}}
        self.assertTrue(self.cache.is_duplicate(later))

    def test_broadcast_supersedes_unsent_schedule_and_survives_restart(self):
        scheduled = {**self.payload, "event_type": "jump_started", "destination": "Destination"}
        broadcast = {**self.payload, "event_type": "carrier_arrival"}
        listener.announce(scheduled, deliver=False)
        listener.announce(broadcast, deliver=False)
        listener._cache = EventCache(self.path)
        with patch.object(listener.requests, "post", return_value=self.response()) as post:
            listener.flush_pending()
            listener.announce(broadcast)
        post.assert_called_once()
        self.assertIn("Carrier Broadcast: New Arrival Destination",
                      post.call_args.kwargs["json"]["embeds"][0]["title"])
        self.assertEqual(listener._cache._pending, {})

    def test_outbox_write_failure_prevents_http_post(self):
        with patch.object(self.cache, "_save_outbox", side_effect=OSError("Disk unavailable")), \
                patch.object(listener.requests, "post") as post:
            with self.assertRaises(OSError):
                listener.announce(self.payload)
        post.assert_not_called()

    def test_acknowledged_delivery_is_not_retried_if_outbox_cleanup_failed(self):
        self.cache.queue(self.payload)
        with patch.object(self.cache, "_save_outbox", side_effect=OSError("Disk unavailable")):
            with self.assertRaises(OSError):
                self.cache.record_and_log(self.payload)
        restarted = EventCache(self.path)
        self.assertTrue(restarted.is_duplicate(self.payload))
        self.assertEqual(restarted.due(), [])

    def test_retry_uses_updated_webhook_and_removal_stops_delivery(self):
        self.cache.queue(self.payload)
        self.cache.defer(self.payload, now=100, delay=300)
        self.cache.update_webhooks([{"callsign": "CCGL", "discord_webhook": "https://example.invalid/new"}])
        row = self.cache.due(now=101)[0]
        self.assertEqual(row["payload"]["discord_webhook"], "https://example.invalid/new")
        self.cache.update_webhooks([])
        self.assertEqual(EventCache(self.path).due(), [])

    def test_observation_gap_invalidates_queued_schedules(self):
        registry = CarrierRegistry([{"callsign": "CCGL"}])
        scheduled = {**self.payload, "event_type": "jump_started", "destination": "Destination"}
        registry.get("CCGL").start_jump("Destination", request_id="request-1")
        self.cache.queue(scheduled)
        registry.clear_pending_jumps()
        self.cache.reconcile_schedules(registry)
        self.assertEqual(EventCache(self.path).due(), [])

    def test_no_historical_webhook_on_startup_and_final_location_is_saved(self):
        directory = Path(self.temp.name) / "journals"
        directory.mkdir()
        path = directory / "Journal.2026-10-09T120000.01.log"
        path.write_text(
            '{"event":"CarrierLocation","CarrierID":3715894016,"StarSystem":"Origin"}\n'
            '{"event":"CarrierJumpRequest","CarrierID":3715894016,"SystemName":"Destination"}\n'
            '{"event":"CarrierLocation","CarrierID":3715894016,"StarSystem":"Destination"}\n',
            encoding="utf-8",
        )
        config = [{"callsign": "CCGL", "CarrierID": 3715894016,
                   "discord_webhook": self.payload["discord_webhook"], "last_known_location": "Destination"}]
        stop = threading.Event()
        stop.set()
        with patch.object(listener, "WATCHED_CARRIERS", config), \
                patch.object(listener, "EventCache", return_value=self.cache), \
                patch.object(listener, "save_carriers") as save, patch.object(listener.requests, "post") as post:
            listener.main_loop(stop, str(directory))
        post.assert_not_called()
        self.assertEqual(save.call_args.args[0][0]["last_known_location"], "Destination")

    def test_scheduled_departure_uses_journal_time_without_fixed_arrival_guess(self):
        payload = {**self.payload, "event_type": "jump_started", "system_info": {
            "timestamp": "2026-10-09T12:00:00Z", "departure_time": "2026-10-09T12:30:00Z",
        }}
        fields = listener._build_embed(payload)["fields"]
        departure = next(field for field in fields if "Scheduled Departure" in field["name"])
        epoch = int(datetime(2026, 10, 9, 12, 30, tzinfo=timezone.utc).timestamp())
        self.assertEqual(departure["value"], f"<t:{epoch}:f>")
        self.assertFalse(any("Estimated Arrival" in field["name"] for field in fields))

    def test_main_loop_batch_sends_cancellation_without_obsolete_schedule(self):
        directory = Path(self.temp.name) / "journals"
        directory.mkdir()
        path = directory / "Journal.2026-10-09T120000.01.log"
        path.write_text('{"event":"CarrierLocation","CarrierID":3715894016,"StarSystem":"Origin"}\n',
                        encoding="utf-8")
        config = [{"callsign": "CCGL", "CarrierID": 3715894016,
                   "discord_webhook": self.payload["discord_webhook"], "last_known_location": "Origin"}]

        def start_tailer(**kwargs):
            tailer = JournalTailer(**kwargs)
            with path.open("a", encoding="utf-8") as fh:
                fh.write('{"event":"CarrierJumpRequest","CarrierID":3715894016,"SystemName":"Destination"}\n'
                         '{"event":"CarrierJumpCancelled","CarrierID":3715894016}\n'
                         '{"event":"CarrierLocation","CarrierID":3715894016,"StarSystem":"Origin"}\n')
            return tailer

        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch.object(listener, "WATCHED_CARRIERS", config), \
                patch.object(listener, "EventCache", return_value=self.cache), \
                patch.object(listener, "JournalTailer", side_effect=start_tailer), \
                patch.object(listener, "save_carriers"), \
                patch.object(listener.requests, "post", return_value=self.response()) as post:
            listener.main_loop(stop, str(directory))
        post.assert_called_once()
        self.assertIn("cancelled", post.call_args.kwargs["json"]["embeds"][0]["title"])
        self.assertEqual(self.cache._pending, {})


if __name__ == "__main__":
    unittest.main()
