"""
EDMC plugin entry point for Fleet Carrier Announcer.

EDMC discovers this file automatically when the plugin folder is placed
inside %LOCALAPPDATA%\\EDMarketConnector\\plugins.  The plugin starts a
background thread that tails journal files and announces carrier events
to Discord.  It does NOT interface with EDMC's own data — EDMC merely
acts as the host process.

The EDFCA tab in EDMC Settings allows editing carriers.json fields.
"""

import logging
import os
import sys
import threading
from typing import Optional

import tkinter as tk
from tkinter import ttk

# Ensure the plugin directory is on sys.path so sibling modules resolve.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from fc_config import _load_carriers, save_carriers, reload_carriers

# EDMC's config module is the source of truth for the journal directory.
from config import config

# Try to import EDMC's notebook module for proper settings tab styling.
try:
    import myNotebook as nb
except ImportError:
    nb = None

# Path to the Elite Dangerous journal folder, sourced from EDMC.  Falls back
# to EDMC's auto-detected default when the user hasn't overridden it.
JOURNAL_DIR: str = config.get_str("journaldir") or config.default_journal_dir

logger = logging.getLogger(__name__)

# Plugin metadata — this becomes the tab name in EDMC Settings.
plugin_name = "EDFCA"

_worker_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()

# Editable carrier fields (key in JSON → label in UI).
_EDITABLE_FIELDS = [
    ("callsign",        "Callsign"),
    ("CarrierID",       "Carrier ID"),
    ("name",            "Name"),
    ("discord_webhook", "Discord Webhook"),
    ("logo_url",        "Logo URL"),
]

# Holds the list of carrier row widgets while the prefs window is open.
_carrier_rows: list[dict] = []
_rows_frame: Optional[tk.Frame] = None


# ── EDMC lifecycle ───────────────────────────────────────────────────────────

def plugin_start3(plugin_dir: str) -> str:
    """Called by EDMC on startup.  Returns the plugin name for display."""
    global _worker_thread
    logger.info("Fleet Carrier Announcer starting …")

    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=_run_announcer,
        name="FCAnnouncerWorker",
        daemon=True,
    )
    _worker_thread.start()
    return plugin_name


def plugin_stop() -> None:
    """Called by EDMC on shutdown.  Signals the worker thread to stop."""
    logger.info("Fleet Carrier Announcer stopping …")
    _stop_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=5)


def plugin_app(parent: tk.Frame) -> tk.Label:
    """Optional: show a small label in the EDMC main window."""
    return tk.Label(parent, text="EDFCA: running")


# ── EDFCA settings tab ──────────────────────────────────────────────────────

def plugin_prefs(parent, cmdr: str, is_beta: bool):
    """
    Called by EDMC to build the EDFCA settings tab.
    Returns a frame that EDMC places inside a notebook tab.
    """
    global _carrier_rows, _rows_frame

    try:
        _carrier_rows = []

        # Use EDMC's nb.Frame if available, else fall back to tk.Frame.
        FrameClass = nb.Frame if nb else tk.Frame
        frame = FrameClass(parent)
        frame.columnconfigure(0, weight=1)

        # Title
        tk.Label(
            frame, text="Fleet Carrier Announcer — Carriers",
            font=("", 10, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=5, pady=(5, 10))

        # Scrollable area for carrier rows
        canvas = tk.Canvas(frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        _rows_frame = tk.Frame(canvas)

        _rows_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=_rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.grid(row=1, column=0, sticky="nsew", padx=5)
        scrollbar.grid(row=1, column=1, sticky="ns")
        frame.rowconfigure(1, weight=1)

        # Load existing carriers and build a row for each
        carriers = _load_carriers()
        for carrier_data in carriers:
            _add_carrier_row(carrier_data)

        # Add button
        btn_frame = tk.Frame(frame)
        btn_frame.grid(row=2, column=0, sticky="w", padx=5, pady=(10, 5))
        tk.Button(
            btn_frame, text="+ Add Carrier", command=_on_add_carrier,
        ).pack(side="left", padx=(0, 5))

        return frame

    except Exception:
        logger.exception("Failed to build EDFCA settings tab")
        return None


def prefs_changed(cmdr: str, is_beta: bool) -> None:
    """Called by EDMC when the user clicks OK in settings.  Saves to disk."""
    try:
        carriers_out: list[dict] = []

        # Read existing file to preserve last_known_location per callsign.
        existing = {c.get("callsign", "").upper(): c for c in _load_carriers()}

        for row in _carrier_rows:
            if row.get("_deleted"):
                continue
            entry: dict = {}
            for key, _label in _EDITABLE_FIELDS:
                val = row["vars"][key].get().strip()
                if val:
                    entry[key] = val
            # Skip completely empty rows (no callsign).
            if not entry.get("callsign"):
                continue
            # Preserve last_known_location from the existing config.
            cs = entry["callsign"].upper()
            old = existing.get(cs, {})
            if old.get("last_known_location"):
                entry["last_known_location"] = old["last_known_location"]
            carriers_out.append(entry)

        save_carriers(carriers_out)
        reload_carriers()
        logger.info(f"[EDFCA] Saved {len(carriers_out)} carrier(s) to carriers.json")

    except Exception:
        logger.exception("Failed to save EDFCA settings")


# ── row helpers ──────────────────────────────────────────────────────────────

def _add_carrier_row(data: Optional[dict] = None) -> None:
    """Add a carrier editing row to the settings tab."""
    if data is None:
        data = {}

    row_idx = len(_carrier_rows)
    row_frame = tk.LabelFrame(
        _rows_frame, text=f"Carrier {row_idx + 1}", padx=5, pady=5,
    )
    row_frame.pack(fill="x", padx=5, pady=(0, 5))
    row_frame.columnconfigure(1, weight=1)

    vars_dict: dict[str, tk.StringVar] = {}
    for field_row, (key, label) in enumerate(_EDITABLE_FIELDS):
        tk.Label(row_frame, text=label + ":").grid(
            row=field_row, column=0, sticky="w", padx=(0, 5),
        )
        var = tk.StringVar(value=data.get(key, ""))
        tk.Entry(row_frame, textvariable=var, width=50).grid(
            row=field_row, column=1, sticky="ew", pady=1,
        )
        vars_dict[key] = var

    # Remove button
    row_data: dict = {"frame": row_frame, "vars": vars_dict, "_deleted": False}

    def _on_remove(rd=row_data):
        rd["_deleted"] = True
        rd["frame"].pack_forget()
        rd["frame"].destroy()

    btn_row = len(_EDITABLE_FIELDS)
    tk.Button(row_frame, text="✕ Remove", fg="red", command=_on_remove).grid(
        row=btn_row, column=1, sticky="e", pady=(5, 0),
    )

    _carrier_rows.append(row_data)


def _on_add_carrier() -> None:
    """Callback for the '+ Add Carrier' button."""
    _add_carrier_row()


# ── background worker ────────────────────────────────────────────────────────

def _run_announcer() -> None:
    """Entry point for the background thread — runs the journal tailer loop."""
    from listener import main_loop
    try:
        main_loop(_stop_event, journal_dir=JOURNAL_DIR)
    except Exception:
        logger.exception("Fleet Carrier Announcer crashed")

