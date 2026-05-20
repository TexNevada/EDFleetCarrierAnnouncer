"""Shared logger for the EDFCA plugin.

Acquired via EDMC's ``get_plugin_logger`` so records carry the extra fields
(``osthreadid``, ``class``, ``qualname`` …) that EDMC's log formatter expects.
Falls back to a plain ``logging.Logger`` outside EDMC (e.g. standalone tests).
"""

import logging

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
