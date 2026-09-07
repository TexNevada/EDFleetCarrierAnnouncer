"""
EDMC plugin entry point for Fleet Carrier Announcer.

EDMC discovers this file automatically when the plugin folder is placed
inside %LOCALAPPDATA%\\EDMarketConnector\\plugins.  The plugin starts a
background thread that tails journal files and announces carrier events
to Discord.  It does NOT interface with EDMC's own data — EDMC merely
acts as the host process.

The EDFCA tab in EDMC Settings allows editing carriers.json fields.
"""

import os
import sys
import threading
from typing import Any, Optional

import tkinter as tk
import tkinter.font as tkfont
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

# EDMC's settings window is built entirely from ttk widgets (myNotebook's
# nb.* classes).  Classic tk widgets do not follow ttk's colour scheme, so
# every widget in our tab is created from these aliases instead.  Falling back
# to plain ttk keeps things sane on older EDMC releases / outside EDMC.
_Label = getattr(nb, "Label", None) or ttk.Label
_Entry = getattr(nb, "EntryMenu", None) or getattr(nb, "Entry", None) or ttk.Entry
_Button = getattr(nb, "Button", None) or ttk.Button

# Padding constants mirroring EDMC's own prefs.py, so our tab lines up with
# the rest of the settings window.
_PADX = 10
_PADY = 1
_BOXY = 2

# EDMC's theme module — applies the user-selected (default/dark/transparent)
# colour scheme to our widgets.  Optional so the plugin still loads outside EDMC.
try:
    from theme import theme
except ImportError:
    theme = None

# Path to the Elite Dangerous journal folder, sourced from EDMC.  Falls back
# to EDMC's auto-detected default when the user hasn't overridden it.
JOURNAL_DIR: str = config.get_str("journaldir") or config.default_journal_dir

from _logger import logger

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
_rows_frame: Optional[ttk.Frame] = None

# ttk styles/fonts for the settings tab, created lazily once a root window exists.
_styles_ready = False
_remove_btn_style = ""
_heading_font: Optional[tkfont.Font] = None

# Main-window widgets (the "EDFCA: running" panel and per-carrier location rows).
_main_frame: Optional[tk.Frame] = None
_fc_location_rows: dict[str, dict[str, Any]] = {}
_REFRESH_MS = 2000


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


def plugin_app(parent: tk.Frame) -> tk.Frame:
    """Build the EDMC main-window panel.

    Shows the plugin status header plus, per watched carrier, the carrier's
    last known system in a readonly Entry so it can be selected and copied
    with Ctrl+C.
    """
    global _main_frame
    frame = tk.Frame(parent)
    frame.columnconfigure(0, weight=1)

    tk.Label(frame, text="EDFCA: running").grid(row=0, column=0, sticky="w")

    _main_frame = frame
    _rebuild_fc_location_rows()
    frame.after(_REFRESH_MS, _refresh_fc_locations)
    _apply_theme()
    return frame


def _rebuild_fc_location_rows() -> None:
    """(Re)build one location row per watched carrier under the main header."""
    global _fc_location_rows
    if _main_frame is None:
        return

    for row in _fc_location_rows.values():
        row["row"].destroy()
    _fc_location_rows = {}

    carriers = _load_carriers()
    multiple = len(carriers) > 1
    for i, c in enumerate(carriers, start=1):
        cs = (c.get("callsign") or "").strip().upper()
        if not cs:
            continue
        label_text = f"FC System ({cs}):" if multiple else "FC System:"

        row_frame = tk.Frame(_main_frame)
        row_frame.grid(row=i, column=0, sticky="ew", pady=(2, 0))
        row_frame.columnconfigure(1, weight=1)

        tk.Label(row_frame, text=label_text).grid(row=0, column=0, sticky="w")
        var = tk.StringVar(value=c.get("last_known_location") or "—")
        # A plain Label themes correctly under every EDMC theme — unlike a
        # readonly Entry, whose ``readonlybackground`` EDMC's theme module
        # does not manage.  Click-to-copy gives one-action copy UX.
        value_label = tk.Label(
            row_frame, textvariable=var, cursor="hand2", anchor="w",
        )
        value_label.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        value_label.bind("<Button-1>", _on_location_click)

        # Hover underline.  The normal font is deliberately left unset so
        # EDMC's theme module owns it (the dark/transparent themes switch to
        # Euro Caps); the underlined variant is derived at hover time from
        # whatever font is live then.
        value_label.bind("<Enter>", lambda e, c=cs: _set_location_hover(c, True))
        value_label.bind("<Leave>", lambda e, c=cs: _set_location_hover(c, False))

        _fc_location_rows[cs] = {
            "row": row_frame, "var": var, "label": value_label,
        }

    _apply_theme()


def _set_location_hover(callsign: str, hovering: bool) -> None:
    """Underline a location label while the pointer is over it."""
    row = _fc_location_rows.get(callsign)
    if row is None or not row["label"].winfo_exists():
        return

    label = row["label"]
    if hovering:
        base = label.cget("font")
        underlined = tkfont.Font(font=base)
        underlined.configure(underline=True)
        # Keep both alive — Tk drops named fonts when their last Python
        # reference is collected.
        row["base_font"] = base
        row["hover_font"] = underlined
        label.configure(font=underlined)
    else:
        label.configure(font=row.get("base_font") or "")


def _on_location_click(event) -> None:
    """Copy the clicked location to the clipboard."""
    widget = event.widget
    text = widget.cget("text")
    if not text or text == "—":
        return
    try:
        widget.clipboard_clear()
        widget.clipboard_append(text)
        widget.update()  # flush so other apps see the clipboard contents
        logger.info("Copied location to clipboard: %s", text)
    except tk.TclError:
        logger.exception("Failed to copy to clipboard")


def _apply_theme() -> None:
    """Apply EDMC's current theme to the main-window frame and its children.

    EDMC only walks the whole widget tree once, at startup.  Widgets we build
    later (when the carrier list changes) have to be themed by hand, otherwise
    they keep Tk's default grey-on-grey look while the rest of the window
    follows the user's theme.
    """
    if _main_frame is None or not _main_frame.winfo_exists():
        return
    if theme is None:
        return
    try:
        _theme_tree(_main_frame)
    except Exception:
        logger.exception("theme.update failed")


def _theme_tree(widget: tk.Misc) -> None:
    """Recursively theme ``widget``.

    ``theme.update()`` recolours the widget it is given plus its *direct*
    children only — our per-carrier location rows live one frame deeper, so
    walk the nested frames ourselves.
    """
    theme.update(widget)
    for child in widget.winfo_children():
        if isinstance(child, (tk.Frame, ttk.Frame)):
            _theme_tree(child)


def _refresh_fc_locations() -> None:
    """Poll the running registry and update each FC System field.
    Reschedules itself on the Tk main thread."""
    if _main_frame is None or not _main_frame.winfo_exists():
        return
    try:
        import listener
        if listener._registry is not None:
            for cs, row in _fc_location_rows.items():
                state = listener._registry.get(cs)
                if state is None:
                    continue
                loc = state.current_location or "—"
                if row["var"].get() != loc:
                    row["var"].set(loc)
    except Exception:
        logger.exception("Failed to refresh FC location display")
    _main_frame.after(_REFRESH_MS, _refresh_fc_locations)


# ── EDFCA settings tab ──────────────────────────────────────────────────────

def _init_prefs_styles() -> None:
    """Register the ttk styles and fonts our settings tab needs.

    On Windows EDMC paints notebook pages white through myNotebook's ``nb.*``
    styles; ttk widgets we create ourselves need the same treatment or they
    come out SystemButtonFace grey against a white page.
    """
    global _styles_ready, _remove_btn_style, _heading_font
    if _styles_ready:
        return

    try:
        style = ttk.Style()
        button_base = "TButton"
        if sys.platform == "win32":
            page_bg = getattr(nb, "PAGEBG", "SystemWindow")
            page_fg = getattr(nb, "PAGEFG", "SystemWindowText")
            style.configure("nb.TLabelframe", background=page_bg)
            style.configure("nb.TLabelframe.Label", background=page_bg, foreground=page_fg)
            button_base = "nb.TButton"

        # Dotted prefix inherits everything else from the base button style.
        _remove_btn_style = f"EDFCARemove.{button_base}"
        style.configure(_remove_btn_style, foreground="red")

        _heading_font = tkfont.nametofont("TkDefaultFont").copy()
        _heading_font.configure(weight="bold")
    except Exception:
        logger.exception("Failed to register EDFCA styles")
        _remove_btn_style = ""
        _heading_font = None

    _styles_ready = True


def _page_bg() -> str:
    """Background colour of an EDMC settings page.

    Only needed for ``tk.Canvas``, which has no ttk equivalent and therefore
    can't pick the colour up from a style.
    """
    if sys.platform == "win32":
        return getattr(nb, "PAGEBG", "SystemWindow")
    try:
        return ttk.Style().lookup("TFrame", "background") or ""
    except Exception:
        return ""


def _page_frame(master, **kw) -> ttk.Frame:
    """A ttk.Frame that matches the settings-page background.

    Used instead of ``nb.Frame`` for inner containers — nb.Frame grids a 5px
    spacer child of its own, which would stack up once per carrier row.
    """
    if sys.platform == "win32":
        return ttk.Frame(master, style="nb.TFrame", **kw)
    return ttk.Frame(master, **kw)


def plugin_prefs(parent, cmdr: str, is_beta: bool):
    """
    Called by EDMC to build the EDFCA settings tab.
    Returns a frame that EDMC places inside a notebook tab.
    """
    global _carrier_rows, _rows_frame

    try:
        _carrier_rows = []
        _init_prefs_styles()

        FrameClass = nb.Frame if nb else ttk.Frame
        frame = FrameClass(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        _Label(
            frame, text="Fleet Carrier Announcer — Carriers",
            **({"font": _heading_font} if _heading_font else {}),
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=_PADX, pady=_PADY)

        # Scrollable area for carrier rows
        canvas = tk.Canvas(
            frame, highlightthickness=0, borderwidth=0, background=_page_bg(),
        )
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        _rows_frame = _page_frame(canvas)

        _rows_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        window_id = canvas.create_window((0, 0), window=_rows_frame, anchor="nw")
        # Keep the row container as wide as the canvas so entries stretch.
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfigure(window_id, width=e.width),
        )
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.grid(row=1, column=0, sticky="nsew", padx=(_PADX, 0), pady=_BOXY)
        scrollbar.grid(row=1, column=1, sticky="ns", padx=(0, _PADX), pady=_BOXY)

        # Load existing carriers and build a row for each
        carriers = _load_carriers()
        for carrier_data in carriers:
            _add_carrier_row(carrier_data)

        # Add button
        btn_frame = _page_frame(frame)
        btn_frame.grid(row=2, column=0, sticky="w", padx=_PADX, pady=(_BOXY, _PADY))
        _Button(
            btn_frame, text="+ Add Carrier", command=_on_add_carrier,
        ).pack(side="left")

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
        reloaded = reload_carriers()
        # Push the new list into the running registry so the live plugin
        # picks up adds/removes/edits without an EDMC restart.
        from listener import refresh_carriers
        refresh_carriers(reloaded)
        # Rebuild the main-window location rows so adds/removes appear there too.
        _rebuild_fc_location_rows()
        logger.info(f"[EDFCA] Saved {len(carriers_out)} carrier(s) to carriers.json")

    except Exception:
        logger.exception("Failed to save EDFCA settings")


# ── row helpers ──────────────────────────────────────────────────────────────

def _add_carrier_row(data: Optional[dict] = None) -> None:
    """Add a carrier editing row to the settings tab."""
    if data is None:
        data = {}

    row_idx = len(_carrier_rows)
    row_frame = ttk.LabelFrame(
        _rows_frame, text=f"Carrier {row_idx + 1}", padding=_BOXY * 2,
        style="nb.TLabelframe" if sys.platform == "win32" else "",
    )
    row_frame.pack(fill="x", padx=(0, _PADX), pady=(0, _BOXY))
    row_frame.columnconfigure(1, weight=1)

    vars_dict: dict[str, tk.StringVar] = {}
    for field_row, (key, label) in enumerate(_EDITABLE_FIELDS):
        _Label(row_frame, text=label + ":").grid(
            row=field_row, column=0, sticky="w", padx=(0, _PADX), pady=_PADY,
        )
        var = tk.StringVar(value=data.get(key, ""))
        _Entry(row_frame, textvariable=var, width=50).grid(
            row=field_row, column=1, sticky="ew", pady=_PADY,
        )
        vars_dict[key] = var

    # Remove button
    row_data: dict = {"frame": row_frame, "vars": vars_dict, "_deleted": False}

    def _on_remove(rd=row_data):
        rd["_deleted"] = True
        rd["frame"].pack_forget()
        rd["frame"].destroy()

    btn_row = len(_EDITABLE_FIELDS)
    ttk.Button(
        row_frame, text="✕ Remove", style=_remove_btn_style, command=_on_remove,
    ).grid(row=btn_row, column=1, sticky="e", pady=(_BOXY * 2, 0))

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

