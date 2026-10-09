"""
Event log – append-only log of all incoming carrier events, plus dedup.

Every raw carrier event (from journal files) is appended to ``event_log.log``
as one JSON object per line, unmodified.  Announced payloads are also appended
as-is.  On startup the log is replayed: any entry containing an ``event_type``
key is treated as an already-announced payload and its dedup key is rebuilt.
Raw observations older than CACHE_TTL are pruned on load. Delivery identities
do not expire. Pending webhook deliveries are persisted in a separate outbox.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone

from _logger import logger

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(_CONFIG_DIR, "event_log.log")

# How long (seconds) to keep raw observations before pruning on startup.
CACHE_TTL = 60 * 60 * 24  # 24 hours


def _atomic_write(path: str, contents: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix=".edfca-outbox-", suffix=".tmp", delete=False) as fh:
            temporary = fh.name
            fh.write(contents)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def _make_key(payload: dict) -> str:
    """
    Build a dedup key from the meaningful fields of a payload.

    Two payloads with the same key are considered duplicates and the
    second one should NOT be posted to Discord.
    """
    parts = [
        payload.get("callsign", ""),
        payload.get("event_type", ""),
        payload.get("current_location") or "",
        payload.get("destination") or "",
    ]
    if payload.get("jump_id"):
        return "|".join([payload.get("callsign", ""), payload.get("event_type", ""), payload["jump_id"]])
    if payload.get("event_id"):
        return "|".join([payload.get("callsign", ""), payload.get("event_type", ""), payload["event_id"]])
    info = payload.get("system_info") or {}
    parts.append(info.get("timestamp") or "")
    return "|".join(parts)


def _entry_epoch(entry: dict) -> float:
    """Best-effort epoch from an entry's ``timestamp`` field (ISO-8601).
    Returns 0.0 if the field is missing or unparseable."""
    ts = entry.get("timestamp")
    if not ts:
        # Announced payloads store the timestamp inside system_info.
        info = entry.get("system_info")
        if isinstance(info, dict):
            ts = info.get("timestamp")
    if not ts or not isinstance(ts, str):
        return 0.0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, OSError):
        return 0.0


class EventCache:
    """
    Append-only event log with in-memory dedup index.

    * ``log_raw()`` – appends a raw event dict to the log file unmodified.
    * ``is_duplicate()`` / ``record_and_log()`` – dedup layer that prevents
      the same announced payload from being sent to Discord twice.

    On startup the log file is replayed so the dedup set survives restarts.
    Only raw observations expire; accepted delivery identities are retained.
    """

    def __init__(self, path: str = LOG_FILE, ttl: int = CACHE_TTL) -> None:
        self._path = path
        self._ttl = ttl
        self._announced: set[str] = set()
        self._outbox_path = path + ".outbox.json"
        self._pending: dict[str, dict] = {}
        self._load()
        self._load_outbox()

    # ── public API ───────────────────────────────────────────────────────

    def log_raw(self, event: dict) -> None:
        """Append a raw event dict to the log file, unmodified."""
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError as exc:
            logger.warning("Failed to write raw event to %s: %s", self._path, exc)

    def is_duplicate(self, payload: dict) -> bool:
        """Return True if this payload has already been announced."""
        return _make_key(payload) in self._announced

    def record_and_log(self, payload: dict) -> None:
        """Mark a payload as announced and append it to the log file."""
        key = _make_key(payload)
        if key not in self._announced:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._announced.add(key)
        if key in self._pending:
            pending = dict(self._pending)
            pending.pop(key)
            self._save_outbox(pending)

    def queue(self, payload: dict) -> bool:
        """Persist before POSTing; terminal events supersede unsent schedules."""
        key = _make_key(payload)
        if key in self._announced or key in self._pending:
            return False
        pending = dict(self._pending)
        if payload.get("event_type") in ("jump_completed", "carrier_arrival", "jump_cancelled") and payload.get("jump_id"):
            pending = {k: row for k, row in pending.items()
                       if not (row["payload"].get("event_type") == "jump_started"
                               and row["payload"].get("callsign") == payload.get("callsign")
                               and row["payload"].get("jump_id") == payload["jump_id"])}
        pending[key] = {"payload": payload, "attempts": 0, "next_attempt": 0.0}
        self._save_outbox(pending)
        return True

    def due(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        ready = []
        blocked = set()
        for key, row in self._pending.items():
            if key in self._announced:
                continue
            webhook = row["payload"].get("discord_webhook")
            if row["next_attempt"] > now:
                blocked.add(webhook)
            elif webhook not in blocked:
                ready.append(row)
        return ready

    def defer(self, payload: dict, delay: float | None = None, now: float | None = None) -> None:
        key = _make_key(payload)
        row = self._pending[key]
        attempts = row["attempts"] + 1
        delay = min(300, 2 ** min(attempts, 9)) if delay is None else max(1, delay)
        pending = dict(self._pending)
        pending[key] = {**row, "attempts": attempts, "next_attempt": (time.time() if now is None else now) + delay}
        self._save_outbox(pending)

    def reconcile_schedules(self, registry) -> None:
        """Drop unsent requests invalidated by cancellation, arrival or a gap."""
        pending = dict(self._pending)
        for key, row in self._pending.items():
            payload = row["payload"]
            if payload.get("event_type") != "jump_started" or not payload.get("jump_id"):
                continue
            carrier = registry.get(payload["callsign"])
            if carrier is None or not carrier.jumping or carrier.jump_id != payload["jump_id"]:
                pending.pop(key)
                continue
            departure = (payload.get("system_info") or {}).get("departure_time")
            if departure:
                try:
                    if datetime.fromisoformat(departure.replace("Z", "+00:00")).timestamp() <= time.time():
                        pending.pop(key)
                except ValueError:
                    pending.pop(key)
        if pending != self._pending:
            self._save_outbox(pending)

    def update_webhooks(self, carriers: list[dict]) -> None:
        routes = {c["callsign"].upper(): c.get("discord_webhook") for c in carriers}
        pending = {}
        for key, row in self._pending.items():
            callsign = row["payload"]["callsign"]
            webhook = routes.get(callsign)
            if not webhook:
                continue
            changed = row["payload"].get("discord_webhook") != webhook
            pending[key] = {**row, "payload": {**row["payload"], "discord_webhook": webhook},
                            "next_attempt": 0.0 if changed else row["next_attempt"]}
        if pending != self._pending:
            self._save_outbox(pending)

    def _load_outbox(self) -> None:
        if not os.path.exists(self._outbox_path):
            return
        with open(self._outbox_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("version") != 1 or not isinstance(data.get("pending"), dict):
            raise ValueError("Unsupported delivery outbox format")
        self._pending = {key: row for key, row in data["pending"].items() if key not in self._announced}

    def _save_outbox(self, pending: dict[str, dict]) -> None:
        _atomic_write(self._outbox_path, json.dumps({"version": 1, "pending": pending}))
        self._pending = pending

    # ── internal ─────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Replay the log file to rebuild the dedup set, pruning old entries."""
        if not os.path.exists(self._path):
            logger.info("No existing event log — starting fresh")
            return

        now = datetime.now(timezone.utc).timestamp()
        cutoff = now - self._ttl
        kept_lines: list[str] = []
        count = 0

        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                epoch = _entry_epoch(entry)
                if "event_type" not in entry and epoch > 0 and epoch < cutoff:
                    continue
                kept_lines.append(stripped)
                if "event_type" in entry:
                    self._announced.add(_make_key(entry))
                    count += 1

        # Rewrite the log with only the kept (non-expired) lines.
        try:
            _atomic_write(self._path, "".join(line + "\n" for line in kept_lines))
        except OSError as exc:
            logger.warning("Failed to prune event log: %s", exc)

        logger.info("Loaded %d announced event(s) from %s", count, os.path.basename(self._path))
