"""Verify plugin records are sanitized before propagating to EDMC handlers."""

import io
import logging
import unittest
from unittest.mock import patch

from _logger import logger, _WebhookRedactionFilter


WEBHOOK = "https://discord.com/api/webhooks/123456789/test_secret-token"


class LoggingRedactionTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        handler = logging.StreamHandler(self.output)
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        # A parent handler exercises the propagation used by EDMC log files.
        parent = logging.Logger("test-edmc", level=logging.DEBUG)
        parent.addHandler(handler)
        for name, value in (("parent", parent), ("handlers", []), ("propagate", True),
                            ("disabled", False)):
            patcher = patch.object(logger, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, previous_level)

    def assert_redacted(self):
        text = self.output.getvalue()
        self.assertNotIn(WEBHOOK, text)
        self.assertNotIn("test_secret-token", text)
        self.assertNotIn("123456789", text)
        self.assertIn("[REDACTED DISCORD WEBHOOK]", text)
        return text

    def test_literal_and_interpolated_messages_are_redacted(self):
        logger.debug("Webhook: " + WEBHOOK)
        logger.warning("Delivery to %s returned HTTP %d", WEBHOOK, 429)
        logger.error("Route %(route)s failed", {"route": WEBHOOK})
        text = self.assert_redacted()
        self.assertIn("HTTP 429", text)
        self.assertEqual(text.count("[REDACTED DISCORD WEBHOOK]"), 3)

    def test_nested_payload_logging_does_not_change_delivery_configuration(self):
        payload = {"callsign": "XFH-27F", "nested": {"discord_webhook": WEBHOOK}}
        logger.debug("Full payload: %s", payload)
        text = self.assert_redacted()
        self.assertIn("XFH-27F", text)
        self.assertEqual(payload["nested"]["discord_webhook"], WEBHOOK)

    def test_chained_exceptions_retain_traceback_without_webhook(self):
        try:
            try:
                raise ValueError("Invalid route " + WEBHOOK)
            except ValueError as exc:
                raise RuntimeError("Delivery failed for " + WEBHOOK) from exc
        except RuntimeError:
            logger.exception("Worker failed")
        text = self.assert_redacted()
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn("ValueError", text)
        self.assertIn("RuntimeError", text)
        self.assertIn("Worker failed", text)

    def test_legacy_versioned_alternate_hosts_and_relative_routes(self):
        for route in (
            WEBHOOK,
            "https://discordapp.com/api/webhooks/123456789/test_secret-token?wait=true",
            "https://canary.discord.com/api/v10/webhooks/123456789/test_secret-token",
            "https://ptb.discordapp.com/api/webhooks/123456789/test_secret-token",
            "/api/webhooks/123456789/test_secret-token",
            WEBHOOK.replace("/", "\\/"),
        ):
            with self.subTest(route=route):
                self.output.seek(0)
                self.output.truncate()
                logger.error("Delivery error: %s", route)
                self.assert_redacted()

    def test_cached_exception_and_stack_text_are_sanitized_before_handlers(self):
        record = logging.LogRecord(logger.name, logging.ERROR, __file__, 1,
                                   "Worker error", (), None)
        record.exc_text = "Cached traceback: " + WEBHOOK
        record.stack_info = "Stack text: " + WEBHOOK
        logger.handle(record)
        self.assert_redacted()
        self.assertNotIn("test_secret-token", record.exc_text)
        self.assertNotIn("test_secret-token", record.stack_info)
        self.assertIsNone(record.exc_info)

    def test_unrelated_discord_links_and_diagnostic_fields_are_preserved(self):
        text = "Carrier XFH-27F: https://discord.com/channels/123/456; HTTP 500"
        logger.error(text)
        self.assertIn(text, self.output.getvalue())
        self.assertNotIn("[REDACTED", self.output.getvalue())

    def test_filter_keeps_edmc_context_fields(self):
        record = logging.LogRecord(logger.name, logging.DEBUG, __file__, 1, WEBHOOK, (), None)
        record.osthreadid = 123
        record.__dict__["class"] = "CarrierRegistry"
        record.qualname = "main_loop"
        self.assertTrue(_WebhookRedactionFilter().filter(record))
        self.assertEqual(record.osthreadid, 123)
        self.assertEqual(record.__dict__["class"], "CarrierRegistry")
        self.assertEqual(record.qualname, "main_loop")


if __name__ == "__main__":
    unittest.main()
