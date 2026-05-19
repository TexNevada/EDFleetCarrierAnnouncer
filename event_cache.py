"""
Event log – append-only log of all incoming carrier events, plus dedup.

Every raw carrier event (from journal files) is appended to ``event_log.log``
as one JSON object per line, unmodified.  Announced payloads are also appended
as-is.  On startup the log is replayed: any entry containing an ``event_type``
key is treated as an already-announced payload and its dedup key is rebuilt.
The log is pruned of entries older than CACHE_TTL on load.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(_CONFIG_DIR, "event_log.log")

# How long (seconds) to keep entries before pruning on startup.
CACHE_TTL = 60 * 60 * 24  # 24 hours


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
    Entries older than CACHE_TTL are pruned from the log on load.
    """

    def __init__(self, path: str = LOG_FILE, ttl: int = CACHE_TTL) -> None:
        self._path = path
        self._ttl = ttl
        self._announced: set[str] = set()
        self._load()

    # ── public API ───────────────────────────────────────────────────────

    def log_raw(self, event: dict) -> None:
        """Append a raw event dict to the log file, unmodified."""
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError as exc:
            print(f"[log] Failed to write: {exc}")

    def is_duplicate(self, payload: dict) -> bool:
        """Return True if this payload has already been announced."""
        return _make_key(payload) in self._announced

    def record_and_log(self, payload: dict) -> None:
        """Mark a payload as announced and append it to the log file."""
        self._announced.add(_make_key(payload))
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except OSError as exc:
            print(f"[log] Failed to write: {exc}")

    # ── internal ─────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Replay the log file to rebuild the dedup set, pruning old entries."""
        if not os.path.exists(self._path):
            print("[log] No existing log file — starting fresh")
            return

        now = datetime.now(timezone.utc).timestamp()
        cutoff = now - self._ttl
        kept_lines: list[str] = []
        count = 0

        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        entry = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    # Prune entries older than TTL using the timestamp field.
                    epoch = _entry_epoch(entry)
                    if epoch > 0 and epoch < cutoff:
                        continue
                    kept_lines.append(stripped)
                    # Entries with event_type are announced payloads.
                    if "event_type" in entry:
                        self._announced.add(_make_key(entry))
                        count += 1
        except OSError:
            pass

        # Rewrite the log with only the kept (non-expired) lines.
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                for ln in kept_lines:
                    fh.write(ln + "\n")
        except OSError as exc:
            print(f"[log] Failed to prune log: {exc}")

        print(f"[log] Loaded {count} announced event(s) from {os.path.basename(self._path)}")
