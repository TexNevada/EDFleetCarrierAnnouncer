"""Shared logger for the EDFCA plugin.

Acquired via EDMC's ``get_plugin_logger`` so records carry the extra fields
(``osthreadid``, ``class``, ``qualname`` …) that EDMC's log formatter expects.
Falls back to a plain ``logging.Logger`` outside EDMC (e.g. standalone tests).
Discord webhook routes are redacted before records reach EDMC's handlers.
"""

import logging
import re


# Also match relative routes in requests exceptions and JSON-escaped slashes.
_WEBHOOK_URL = re.compile(
    r"(?:(?:https?:(?:/|\\/){2})?(?:[a-z0-9-]+\.)?discord(?:app)?\.com)?"
    r"(?:/|\\/)api(?:/|\\/)(?:v\d+(?:/|\\/))?webhooks(?:/|\\/)"
    r"[^\s\x22\x27<>()\[\]{},]+",
    re.IGNORECASE,
)
_REDACTED_WEBHOOK = "[REDACTED DISCORD WEBHOOK]"


class _WebhookRedactionFilter(logging.Filter):
    """Remove webhook credentials from messages, tracebacks and stack text."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Render first so positional arguments, mapping arguments, and nested
        # payload representations all receive the same redaction. Do not
        # mutate the original payload/configuration objects used for delivery.
        record.msg = _WEBHOOK_URL.sub(_REDACTED_WEBHOOK, record.getMessage())
        record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = _WEBHOOK_URL.sub(_REDACTED_WEBHOOK, record.exc_text)
        # Keep the sanitized traceback text for handlers, without retaining
        # an exception object that another formatter could render unsanitized.
        record.exc_info = None
        if record.stack_info:
            record.stack_info = _WEBHOOK_URL.sub(_REDACTED_WEBHOOK, record.stack_info)
        return True

try:
    from EDMCLogging import get_plugin_logger

    logger = get_plugin_logger("EDFCA")
except ImportError:
    try:
        from config import appname

        logger = logging.getLogger(f"{appname}.EDFCA")
    except ImportError:
        logger = logging.getLogger("EDFCA")

# EDMC sets the LogRecord factory via setLoggerClass(LoggerMixin), but that
# only applies to loggers created AFTER the call.  If our logger was cached
# beforehand (e.g. EDMC imported a module that touched logging before
# EDMCLogging ran), its records would lack osthreadid and EDMC's file
# handler would KeyError on format().  Attaching the context filter to our
# own logger guarantees the fields are present before the record propagates.
try:
    from EDMCLogging import EDMCContextFilter

    if not any(isinstance(f, EDMCContextFilter) for f in logger.filters):
        logger.addFilter(EDMCContextFilter())
except ImportError:
    pass

if not any(isinstance(f, _WebhookRedactionFilter) for f in logger.filters):
    logger.addFilter(_WebhookRedactionFilter())
