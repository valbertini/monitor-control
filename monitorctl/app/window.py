"""The monitorctl window: every control of every monitor, plus the settings.

This module is imported the first time the window is opened, never before, so a
session that lives in the tray never loads Tcl/Tk.

Threading: every method here runs on the main thread.  Anything that touches a
monitor is handed to the :class:`~monitorctl.app.worker.Worker` and comes back
through a callback, so the window never freezes while the bus is busy - a full
probe takes seconds.

Layout, top to bottom: a toolbar (Auto apply, Apply, Discard; Save, Restore,
Rescan, Calibration, theme);
one tab per monitor - its profile bar, then section cards side by side in the
order and with the icons, hints and channel colours of ``presentation.py``,
then the controls it lacks, folded into one card; a Settings tab; a status bar.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, field
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Protocol

from .. import __version__, autostart
from ..ddc.errors import DdcError, ProfileError
from ..ddc.profiles import MonitorProfiles, check_profile_name
from ..ddc.service import (
    FeatureState,
    MonitorService,
    MonitorState,
    Reassert,
    RestoreReport,
    WriteOutcome,
)
from ..ddc.snapshot import Snapshot
from ..ddc.vcp import CATEGORIES, FEATURES, inert_keys
from ..fileio import state_directory
from . import win32
from .calibration import CalibrationWindow
from .presentation import (
    CHANNEL_OF,
    ICON_FONT,
    SECTIONS,
    Section,
    section_key,
    section_named,
)
from .settings import (
    MAX_INTERVAL_MINUTES,
    MIN_INTERVAL_MINUTES,
    RefreshMode,
    SettingsStore,
    Theme,
)
from .theme import Palette, apply_theme, scale_style
from .worker import Worker

__all__ = ["Bridge", "MainWindow"]

_log = logging.getLogger(__name__)

#: Slider writes wait for the value to settle: a drag produces dozens of values
#: per second, and every one written would be one more EEPROM cycle.
DEBOUNCE_MS = 250
TOOLTIP_DELAY_MS = 600
#: Marks a control tied to one colour channel, in that colour.
DOT = "\u25cf"
#: The per-control "back to the saved value" mark (an anticlockwise arrow).
REVERT_GLYPH = "\u21ba"
#: Width a card column needs; the number of columns follows the window.
COLUMN_WIDTH = 330
#: How often the busy spinner turns, and by how many degrees.
SPIN_MS = 80
SPIN_STEP = 30


class Bridge(Protocol):
    """What the window needs from the application that hosts it."""

    worker: Worker
    settings: SettingsStore

    def settings_changed(self) -> None: ...
    def set_editing(self, editing: bool) -> None: ...
    def window_hidden(self) -> None: ...
    def tray_status(self) -> str: ...


# --------------------------------------------------------------------------- #
# Small widgets
# --------------------------------------------------------------------------- #


class Tooltip:
    """A hover tip.  Keyboard users get the same text in the status bar."""

    def __init__(
        self,
        widget: tk.Widget,
        text: str | Callable[[], str],
        palette: Callable[[], Palette],
    ) -> None:
        """
        Args:
            widget: What to hover.
            text: The tip, or a function computing it when it is shown.
            palette: Current colours, read when the tip is shown.
        """
        self._widget = widget
        self._text = text
        self._palette = palette
        self._after: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        # A redraw destroys rows under the pointer; a pending tip would then
        # fire for a widget that no longer exists.
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _event: object = None) -> None:
        self._cancel()
        self._after = self._widget.after(TOOLTIP_DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            self._widget.after_cancel(self._after)
            self._after = None

    def _show(self) -> None:
        self._after = None
        if self._tip is not None or not self._widget.winfo_viewable():
            return  # e.g. the window was hidden while the pointer rested
        text = self._text() if callable(self._text) else self._text
        if not text:
            return
        p = self._palette()
        x = self._widget.winfo_rootx() + 12
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        tip = tk.Toplevel(self._widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tip,
            text=text,
            justify="left",
            wraplength=380,
            background=p.surface_3,
            foreground=p.text,
            borderwidth=1,
            relief="solid",
            padx=8,
            pady=6,
        ).pack()
        self._tip = tip

    def _hide(self, _event: object = None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class Spinner:
    """A small turning arc that says "working" where the work is reported.

    In place of a busy pointer: a "watch" cursor over the whole window reads
    as a hang, and holds nothing back anyway - every control already refuses
    what cannot run while the bus is busy, and says so.
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        size: int,
        background: Callable[[], str],
        colour: Callable[[], str],
    ) -> None:
        """
        Args:
            master: Where it goes; the caller places :attr:`canvas`.
            size: Width and height, in pixels.
            background: The colour behind it, read on every :meth:`recolour`.
            colour: The arc's colour, likewise.
        """
        self.canvas = tk.Canvas(
            master, width=size, height=size, highlightthickness=0, borderwidth=0
        )
        inset = 2
        self._arc = self.canvas.create_arc(
            inset,
            inset,
            size - inset,
            size - inset,
            start=90,
            extent=270,
            style="arc",
            width=2,
        )
        self._background = background
        self._colour = colour
        self._angle = 90
        self._after: str | None = None
        self.recolour()

    @property
    def turning(self) -> bool:
        return self._after is not None

    def start(self) -> None:
        if self._after is None:
            self._turn()

    def stop(self) -> None:
        if self._after is not None:
            self.canvas.after_cancel(self._after)
            self._after = None

    def recolour(self) -> None:
        self.canvas.configure(background=self._background())
        self.canvas.itemconfigure(self._arc, outline=self._colour())

    def _turn(self) -> None:
        self._angle = (self._angle - SPIN_STEP) % 360
        self.canvas.itemconfigure(self._arc, start=self._angle)
        self._after = self.canvas.after(SPIN_MS, self._turn)


#: Widget classes whose stock bindings change their value on a wheel turn.
WHEEL_CLASSES = ("TSpinbox", "TCombobox")


def disable_wheel_on_controls(root: tk.Misc) -> None:
    """Stop the mouse wheel from changing values.

    Scrolling down a page of controls, the pointer comes to rest over one, and
    the next notch quietly moves a gain or switches a preset - a write the user
    never meant, and an EEPROM cycle besides.  Values change by click, drag
    or keyboard only.  The bindings are removed for the class, so every
    spinbox and combobox in the app - the Settings tab's too - behaves alike.
    """
    for name in WHEEL_CLASSES:
        for sequence in ("<MouseWheel>", "<Shift-MouseWheel>"):
            root.unbind_class(name, sequence)


class ScrollFrame(ttk.Frame):
    """A vertically scrolling container; children go in :attr:`body`."""

    def __init__(self, master: tk.Widget, palette: Palette) -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, background=palette.bg)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind(
            "<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._wheel)
        )
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_body(self, _event: object) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _wheel(self, event: tk.Event) -> None:
        # The wheel always scrolls the page, over a control too: controls no
        # longer take it (see ``disable_wheel_on_controls``).  Only an open
        # drop-down list keeps it, to scroll its own entries.
        if isinstance(event.widget, tk.Listbox):
            return
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


# --------------------------------------------------------------------------- #
# One control
# --------------------------------------------------------------------------- #


@dataclass(eq=False)
class FeatureRow:
    """The widgets and write state of one control on one monitor."""

    window: MainWindow
    monitor_id: str
    feature: FeatureState
    #: The monitor as the window files it (:meth:`MainWindow._ident`): its
    #: EDID identity, which a renumbering leaves alone.  ``monitor_id`` is a
    #: position, right only until the next re-enumeration; whatever comes
    #: back to a row later finds the monitor by this.
    ident: str = ""
    confirmed: int | None = None
    in_flight: bool = False
    pending: int | None = None
    debounce: str | None = None
    #: The user typed in the number field since it last showed a value.  Only
    #: then does leaving the field count as asking for a write.
    typed: bool = False
    widgets: list[tk.Widget] = field(default_factory=list)
    scale_var: tk.DoubleVar | None = None
    text_var: tk.StringVar | None = None
    choice_var: tk.StringVar | None = None
    spin: ttk.Spinbox | None = None
    labels: dict[str, int] = field(default_factory=dict)
    #: Enabled only while the value differs from the profile in use; it puts
    #: the saved value back.  A button, so the keyboard reaches it too.
    revert: ttk.Button | None = None
    name_label: ttk.Label | None = None

    @property
    def key(self) -> str:
        return self.feature.key

    def build(self, parent: ttk.Frame, row: int) -> None:
        f = self.feature
        self.confirmed = f.value
        usable = f.supported and f.writable
        source = f"0x{f.code:02X}" if f.code is not None else f.source
        channel = CHANNEL_OF.get(f.key)
        if channel:
            # The channel's colour, beside a name that still says it.
            ttk.Label(parent, text=DOT, style=f"{channel.capitalize()}.Dot.TLabel").grid(
                row=row, column=0, sticky="w", padx=(0, 6)
            )
        name = ttk.Label(
            parent,
            text=f.label,
            style="Feature.TLabel" if usable else "ReadOnly.TLabel",
            takefocus=False,
        )
        name.grid(row=row, column=1, sticky="w", padx=(0, 8), pady=3)
        self.name_label = name
        tip = f"{f.tooltip}\n\n{source}" if f.tooltip else source
        if not usable:
            # Unsupported controls never get a row (see _page): only one that
            # exists but cannot be changed lands here.
            tip += f"\n\nRead-only: {f.error or 'it can be read but not changed'}"
        Tooltip(name, tip, lambda: self.window.palette)
        self.widgets.append(name)

        if f.kind == "continuous":
            maximum = f.maximum or 100
            self.scale_var = tk.DoubleVar(parent, value=f.value or 0)
            scale = ttk.Scale(
                parent,
                from_=0,
                to=maximum,
                orient="horizontal",
                variable=self.scale_var,
                command=self._on_scale,
                length=150,
                style=scale_style(channel),
            )
            scale.grid(row=row, column=2, sticky="ew", pady=3)
            # Overrides the stock press, which steps one unit toward the
            # pointer: the knob goes where the user clicks, and follows a drag
            # from there.
            scale.bind("<Button-1>", self._on_scale_pointer)
            scale.bind("<B1-Motion>", self._on_scale_pointer)
            self.text_var = tk.StringVar(
                parent, value="" if f.value is None else str(f.value)
            )
            validate = (parent.register(self._validate_text), "%P")
            self.spin = ttk.Spinbox(
                parent,
                from_=0,
                to=maximum,
                increment=1,
                width=5,
                textvariable=self.text_var,
                validate="key",
                validatecommand=validate,
                command=self._on_text_commit,
            )
            self.spin.grid(row=row, column=3, sticky="e", padx=(8, 0), pady=3)
            self.spin.bind("<Key>", self._on_key, add="+")
            self.spin.bind("<Return>", lambda _e: self._on_text_commit(force=True))
            self.spin.bind("<FocusOut>", self._on_text_commit)
            self.widgets += [scale, self.spin]
            for widget in (scale, self.spin):
                widget.bind("<FocusIn>", lambda _e, t=tip: self.window.hint(t), add="+")
        else:
            self.labels = {o.label: o.value for o in f.options}
            current = next((o.label for o in f.options if o.value == f.value), "")
            if f.value is not None and not current:
                current = f"Value 0x{f.value:02X}"
            self.choice_var = tk.StringVar(parent, value=current)
            combo = ttk.Combobox(
                parent,
                textvariable=self.choice_var,
                values=list(self.labels),
                state="readonly",
                width=18,
            )
            combo.grid(row=row, column=2, columnspan=2, sticky="ew", pady=3)
            combo.bind("<<ComboboxSelected>>", self._on_choice)
            combo.bind("<FocusIn>", lambda _e, t=tip: self.window.hint(t), add="+")
            self.widgets.append(combo)

        if not usable:
            for widget in self.widgets[1:]:
                widget.state(["disabled"])
            return
        # Always gridded and always the same width - only its glyph comes and
        # goes - so the slider beside it never resizes under the pointer.
        self.revert = ttk.Button(
            parent, style="Revert.TButton", width=2, command=self._revert
        )
        self.revert.grid(row=row, column=4, sticky="e", padx=(4, 0))
        self.revert.bind(
            "<FocusIn>", lambda _e: self.window.hint(self._revert_tip()), add="+"
        )
        Tooltip(self.revert, self._revert_tip, lambda: self.window.palette)
        self.refresh_drift()
        staged = self._staged()
        if staged is not None:
            # Redrawn while a change waits for Apply: still show that change.
            self.show(staged)
            self.mark_staged(True)

    def mark_staged(self, staged: bool) -> None:
        """Mark the control as changed on screen but not written yet.

        A dot after the name as well as the colour, so the mark does not rest
        on colour alone.
        """
        if self.name_label is not None:
            self.name_label.configure(
                text=f"{self.feature.label}  {DOT}" if staged else self.feature.label,
                style="Staged.Feature.TLabel" if staged else "Feature.TLabel",
            )
        self.refresh_drift()

    # Drift ------------------------------------------------------------------ #

    def _saved(self) -> int | None:
        return self.window.saved_value(self.monitor_id, self.key)

    @property
    def drifted(self) -> bool:
        saved = self._saved()
        current = self.window.values.get((self.monitor_id, self.key))
        return saved is not None and current is not None and saved != current

    def _staged(self) -> int | None:
        return self.window.staged.get((self.ident, self.key))

    @property
    def waiting(self) -> bool:
        """A change to this control waits for Apply."""
        return self._staged() is not None

    def refresh_drift(self) -> None:
        """Offer the mark while there is something to go back from: a change
        waiting for Apply, or a value that differs from the profile in use."""
        if self.revert is None:
            return
        offered = self.waiting or self.drifted
        self.revert.configure(text=REVERT_GLYPH if offered else "")
        self.revert.state(["!disabled"] if offered else ["disabled"])

    def _revert_tip(self) -> str:
        if self.waiting:
            return (
                f"Cancel this change (the monitor keeps {self._describe(self.confirmed)})"
            )
        if not self.drifted:
            return ""
        return f"Back to the saved value ({self._describe(self._saved())})"

    def _describe(self, value: int) -> str:
        return next((k for k, v in self.labels.items() if v == value), str(value))

    def _revert(self) -> None:
        if self.window.busy:
            return
        # A drag still waiting for its debounce would otherwise land after
        # this and undo it.
        self.cancel_debounce()
        if self.waiting:
            self.window.unstage(self)
            return
        if not self.drifted:
            return
        saved = self._saved()
        self.show(saved)
        self._commit(saved)

    # Input ------------------------------------------------------------------ #

    def _validate_text(self, proposed: str) -> bool:
        # Digits only while typing; the range is checked on commit, so a user
        # can clear the field and type a new number.
        return proposed == "" or (proposed.isdigit() and len(proposed) <= 5)

    def _on_scale_pointer(self, event: tk.Event) -> str:
        scale = event.widget
        if not scale.instate(["!disabled"]):
            return "break"
        scale.focus_set()
        value = round(scale.get(event.x, event.y))
        if value != round(self.scale_var.get()):
            self.scale_var.set(value)
            self._on_scale(str(value))
        return "break"

    def _on_scale(self, raw: str) -> None:
        # Only the user moves the slider into here: setting its variable from
        # code never invokes the widget's command.
        value = round(float(raw))
        self.text_var.set(str(value))
        self._debounce(value)

    def _on_key(self, event: tk.Event) -> None:
        if event.char.isdigit() or event.keysym in ("BackSpace", "Delete"):
            self.typed = True

    def _on_text_commit(self, _event: object = None, *, force: bool = False) -> None:
        """Commit what the field holds: write it, or with auto apply off, hold it.

        A value equal to the confirmed one is normally not worth a write.  On
        a forgetful control it is, if the user asked for it - typed it, or
        pressed Enter: the screen may be wrong while the field says it is
        right.  Merely leaving the field is not asking; it happens on every
        click elsewhere, and each one would cost two EEPROM writes.
        """
        asked = force or self.typed
        self.typed = False
        text = self.text_var.get().strip()
        maximum = self.feature.maximum or 100
        if not text.isdigit() or not 0 <= int(text) <= maximum:
            self.spin.configure(style="Invalid.TSpinbox")
            self.window.status(
                f"{self.feature.label}: enter a whole number from 0 to {maximum}",
                error=True,
            )
            self.show(self.confirmed)
            spin = self.spin
            self.window.root.after(
                1200,
                lambda: spin.winfo_exists() and spin.configure(style="TSpinbox"),
            )
            return
        value = int(text)
        self.scale_var.set(value)
        if (
            value != self.confirmed
            or self.waiting  # typing the monitor's value back cancels the change
            or (asked and self.feature.key in self.window.volatile(self.monitor_id))
        ):
            self._debounce(value, delay=0)

    def _on_choice(self, _event: object) -> None:
        value = self.labels.get(self.choice_var.get())
        if value is not None:
            self._commit(value)

    def _debounce(self, value: int, delay: int = DEBOUNCE_MS) -> None:
        if self.debounce is not None:
            self.window.root.after_cancel(self.debounce)
        # On the root, not on the row's own widget: a reload that redraws the
        # rows meanwhile must not drop the write the user just made.
        self.debounce = self.window.root.after(delay, lambda: self._commit(value))
        # Known to the window, which may have to send it at once (Save) or
        # drop it (Restore) instead of waiting for the delay.
        self.window.settling[self] = value

    def cancel_debounce(self) -> None:
        """Drop a change still waiting for the value to settle."""
        if self.debounce is not None:
            self.window.root.after_cancel(self.debounce)
            self.debounce = None
        self.window.settling.pop(self, None)

    def flush(self) -> None:
        """Commit now what would be committed when the value settles."""
        value = self.window.settling.get(self)
        self.cancel_debounce()
        if self.typed:
            # Typed into the field and not committed yet - Ctrl+S with the
            # focus still there: that number is what the user sees.
            self._on_text_commit()
            value = self.window.settling.get(self, value)
            self.cancel_debounce()
        if value is not None:
            self._commit(value)

    # Writing ---------------------------------------------------------------- #

    def _commit(self, value: int) -> None:
        self.debounce = None
        self.window.settling.pop(self, None)
        live = self.window.live_row(self.ident, self.key)
        if live is not self and (not self.in_flight or not self.window.auto_apply):
            # Redrawn since the change was made (a debounce or a focus-out
            # firing after a reload): this row's widgets are gone, so the row
            # on screen takes the change - or nothing does, if the control is
            # no longer shown.  A write still in flight from here keeps its
            # order through ``pending`` and comes back once it lands.
            if live is not None:
                live.show(value)
                live._commit(value)
            return
        if not self.window.auto_apply:
            self.window.stage(self, value)
            return
        if self.waiting:
            # Written now, so nothing about it waits any more.
            self.window.staged.pop((self.ident, self.key), None)
            self.mark_staged(False)
        if self.window.busy:
            self.window.status(
                f"{self.feature.label}: not written, wait for the current operation."
            )
            self.show(self.confirmed)
            return
        if self.in_flight:
            self.pending = value
            return
        self.in_flight = True
        self.window.writing.add(self)
        self.window._refresh_state()  # holds the keeper off (see there)
        key = self.key
        # Found again inside the job: a keeper rescan queued ahead of it may
        # have renumbered the monitors, and this position be another's now.
        locate = self.window.locator(self.monitor_id)
        self.window.bridge.worker.submit(
            lambda s: s.write_feature(locate(s), key, value),
            self._written,
            label=f"Writing {self.feature.label}",
        )

    def _written(self, outcome: WriteOutcome | None, error: BaseException | None) -> None:
        self.in_flight = False
        self.window.writing.discard(self)
        live = self.window.live_row(self.ident, self.key)
        if live is not None and live is not self:
            # The window was redrawn while this write was in flight; the row on
            # screen now is another object, built from the value before it.
            live.confirm(outcome, error)
        if error is not None:
            self.window.status(f"{self.feature.label}: {error}", error=True)
            self.pending = None
            self.show(self.confirmed)
            self.window._refresh_state()  # nothing in flight to wait for
        else:
            self.confirmed = (
                outcome.confirmed if outcome.confirmed is not None else outcome.applied
            )
            # Under the monitor's id *now*, which a redraw may have changed.
            monitor_id = self.window._monitor_id_of(self.ident)
            if monitor_id is not None:
                self.window.record(monitor_id, self.key, self.confirmed)
            if outcome.warnings:
                self.window.status(f"{self.feature.label}: {outcome.warnings[-1]}")
            if self.pending is None:
                self.show(self._on_screen())
        if self.pending is not None:
            value, self.pending = self.pending, None
            if value != self.confirmed:
                self._commit(value)
        self.window.edits_landed()

    def confirm(self, outcome: WriteOutcome | None, error: BaseException | None) -> None:
        """Take over the result of a write made through a row since destroyed."""
        if error is None and outcome is not None:
            self.confirmed = (
                outcome.confirmed if outcome.confirmed is not None else outcome.applied
            )
            self.show(self._on_screen())
            self.refresh_drift()

    def _on_screen(self) -> int | None:
        """What the control should display: a waiting change, else the monitor."""
        staged = self._staged()
        return self.confirmed if staged is None else staged

    def show(self, value: int | None) -> None:
        """Display ``value`` without writing it."""
        if value is None:
            return
        self.typed = False
        if self.scale_var is not None:
            self.scale_var.set(value)
            self.text_var.set(str(value))
        elif self.choice_var is not None:
            label = next(
                (k for k, v in self.labels.items() if v == value),
                f"Value 0x{value:02X}",
            )
            self.choice_var.set(label)


@dataclass(frozen=True, slots=True)
class _Page:
    """What one monitor's page shows, in order."""

    sections: tuple[tuple[Section, tuple[FeatureState, ...]], ...]
    #: Controls the monitor does not have; listed apart, never as dead sliders.
    unavailable: tuple[FeatureState, ...]


def _page(monitor: MonitorState) -> _Page:
    """Sort a monitor's controls into the sections of ``presentation.SECTIONS``.

    A control the monitor does not implement leaves its section for the
    "not available" list, and a section left empty is not shown: a page of
    greyed-out sliders hides the few that work.  A control that exists but is
    read-only stays in place, disabled, since its value still means something.
    Within a section the registry order holds.  This is presentation only - a
    restore still writes in registry order, coarse controls before fine ones.
    """
    order = {section.key: index for index, section in enumerate(SECTIONS)}
    grouped: dict[str, list[FeatureState]] = {}
    unavailable: list[FeatureState] = []
    for feature in monitor.features:
        if feature.supported:
            key = section_key(feature.key, feature.category)
            grouped.setdefault(key, []).append(feature)
        else:
            unavailable.append(feature)
    sections = [(section_named(key), tuple(members)) for key, members in grouped.items()]
    sections.sort(key=lambda entry: order.get(entry[0].key, len(order)))
    return _Page(tuple(sections), tuple(unavailable))


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #


def ask_name(
    parent: tk.Misc,
    palette: Palette,
    *,
    title: str,
    prompt: str,
    initial: str,
    validate: Callable[[str], str | None],
) -> str | None:
    """Ask for a profile name in a small modal dialog, themed like the window.

    ``validate`` returns an error for the person, or ``None`` to accept; the
    dialog stays open, showing the error, until a name is accepted or the user
    cancels.  Returns the accepted name, trimmed, or ``None``.
    """
    top = tk.Toplevel(parent)
    top.title(title)
    top.configure(background=palette.surface)
    top.resizable(False, False)
    top.transient(parent.winfo_toplevel())
    body = ttk.Frame(top, style="Card.TFrame", padding=16)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=prompt, style="Card.TLabel", wraplength=360).pack(anchor="w")
    value = tk.StringVar(top, value=initial)
    entry = ttk.Entry(body, textvariable=value, width=40)
    entry.pack(fill="x", pady=(8, 4))
    error = ttk.Label(body, text="", style="CardError.TLabel", wraplength=360)
    error.pack(anchor="w")
    buttons = ttk.Frame(body, style="Card.TFrame")
    buttons.pack(anchor="e", pady=(10, 0))
    result: list[str] = []

    def accept(_event: object = None) -> None:
        name = value.get().strip()
        problem = validate(name)
        if problem:
            error.configure(text=problem)
            entry.focus_set()
            return
        result.append(name)
        top.destroy()

    ttk.Button(buttons, text="OK", style="Accent.TButton", command=accept).pack(
        side="left"
    )
    ttk.Button(buttons, text="Cancel", command=top.destroy).pack(side="left", padx=(6, 0))
    top.bind("<Return>", accept)
    top.bind("<Escape>", lambda _e: top.destroy())
    entry.select_range(0, "end")
    entry.focus_set()
    top.update_idletasks()
    owner = parent.winfo_toplevel()
    x = owner.winfo_rootx() + (owner.winfo_width() - top.winfo_reqwidth()) // 2
    y = owner.winfo_rooty() + (owner.winfo_height() - top.winfo_reqheight()) // 3
    top.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    top.grab_set()
    top.wait_window()
    return result[0] if result else None


@dataclass(eq=False)
class ProfileBar:
    """The profile controls at the top of one monitor's tab."""

    window: MainWindow
    monitor: MonitorState
    choice: tk.StringVar | None = None
    combo: ttk.Combobox | None = None
    rename_button: ttk.Button | None = None
    new_button: ttk.Button | None = None
    delete_button: ttk.Menubutton | None = None
    delete_menu: tk.Menu | None = None
    state_label: ttk.Label | None = None

    #: Shown in the selector of a monitor that has no profile yet.
    NO_PROFILE = "No profile yet"

    def build(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(10, 0))
        self.choice = tk.StringVar(bar)
        ttk.Label(bar, text="Profile").pack(side="left", padx=(0, 8))
        self.combo = ttk.Combobox(
            bar, textvariable=self.choice, state="readonly", width=26
        )
        self.combo.pack(side="left")
        self.combo.bind("<<ComboboxSelected>>", self._chosen)
        self.new_button = ttk.Button(
            bar, text="New…", command=lambda: self.window.new_profile(self.monitor.id)
        )
        self.new_button.pack(side="left", padx=(8, 0))
        self.rename_button = ttk.Button(
            bar,
            text="Rename…",
            command=lambda: self.window.rename_profile(self.monitor.id),
        )
        self.rename_button.pack(side="left", padx=(6, 0))
        self.delete_button = ttk.Menubutton(bar, text="Delete")
        self.delete_menu = tk.Menu(self.delete_button, tearoff=False)
        self.delete_button.configure(menu=self.delete_menu)
        self.delete_button.pack(side="left", padx=(6, 0))
        self.state_label = ttk.Label(bar, style="Dim.TLabel")
        self.state_label.pack(side="left", padx=(14, 0))
        palette = lambda: self.window.palette  # noqa: E731
        Tooltip(
            self.combo,
            "Choosing another profile puts its values on this monitor - at once, or "
            "as changes waiting for Apply when Auto apply is off.",
            palette,
        )
        Tooltip(
            self.new_button,
            "Save this monitor's current values as a new profile, and use it.",
            palette,
        )
        Tooltip(
            self.delete_button,
            "Delete a profile this monitor is not using. The one in use cannot be "
            "deleted: switch first.",
            palette,
        )
        self.refresh()

    @property
    def entry(self) -> MonitorProfiles | None:
        return self.window.profiles.get(self.monitor.id)

    def refresh(self) -> None:
        """Show the monitor's current profiles and whether it drifted."""
        entry = self.entry
        names = list(entry.names) if entry else []
        active = entry.active_profile if entry else None
        self.combo.configure(values=names)
        self.choice.set(active.name if active else self.NO_PROFILE)
        self.delete_menu.delete(0, "end")
        others = [name for name in names if active is None or name != active.name]
        for name in others:
            self.delete_menu.add_command(
                label=name,
                command=lambda n=name: self.window.delete_profile(self.monitor.id, n),
            )
        busy = self.window.busy
        self.combo.state(["disabled"] if busy or not names else ["!disabled"])
        self.new_button.state(["disabled"] if busy else ["!disabled"])
        self.rename_button.state(["disabled"] if busy or not active else ["!disabled"])
        self.delete_button.state(["disabled"] if busy or not others else ["!disabled"])
        if self.window.staged_count(self.monitor.id):
            text, style = "changes not applied", "Drifted.TLabel"
        elif active is None:
            text, style = "Save stores this monitor's values as “Default”.", "Dim.TLabel"
        elif self.window.differences(self.monitor.id):
            text, style = "unsaved changes", "Drifted.TLabel"
        else:
            text, style = f"saved {_stamp(active.saved_at)}", "Dim.TLabel"
        self.state_label.configure(text=text, style=style)

    def _chosen(self, _event: object) -> None:
        name = self.choice.get()
        active = self.entry.active_profile if self.entry else None
        if active is not None and name == active.name:
            return
        self.window.switch_profile(self.monitor.id, name)


def _stamp(saved_at: float) -> str:
    """``14:03`` today, ``02/10 14:03`` on another day."""
    when = time.localtime(saved_at)
    today = when[:3] == time.localtime()[:3]
    return time.strftime("%H:%M" if today else "%d/%m %H:%M", when)


def _suggest_name(entry: MonitorProfiles | None) -> str:
    taken = {n.casefold() for n in entry.names} if entry else set()
    n = len(taken) + 1
    while f"profile {n}" in taken:
        n += 1
    return f"Profile {n}"


def _name_problem(
    text: str, entry: MonitorProfiles | None, keep: str | None
) -> str | None:
    """Why ``text`` cannot name a profile of ``entry``, or ``None`` if it can.

    ``keep`` is the name being replaced on a rename: changing only its case is
    allowed.  The service checks again; this is for an answer while typing.
    """
    try:
        name = check_profile_name(text)
    except ProfileError as exc:
        return str(exc)
    if entry is not None:
        clash = entry.profile(name)
        if clash is not None and (keep is None or clash.name != keep):
            return f"There is already a profile named “{clash.name}”."
    return None


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #


class MainWindow:
    """The application window.  Created once; hidden, never destroyed, on close."""

    def __init__(self, root: tk.Tk, bridge: Bridge, icon_path: str | None) -> None:
        self.root = root
        self.bridge = bridge
        self.busy = False
        self.monitors: tuple[MonitorState, ...] = ()
        #: Each connected monitor's profiles, by monitor id, as the service
        #: matched them; ``None`` for a monitor with nothing saved.
        self.profiles: dict[str, MonitorProfiles | None] = {}
        self.values: dict[tuple[str, str], int | None] = {}
        self.rows: dict[tuple[str, str], FeatureRow] = {}
        self.profile_bars: dict[str, ProfileBar] = {}
        #: Changes made with auto apply off, by (monitor identity, feature
        #: key), waiting for Apply.  Kept across redraws.
        self.staged: dict[tuple[str, str], int] = {}
        #: Rows whose change waits for the value to settle (the debounce), with
        #: that value; and rows whose write is on its way.  Rows a redraw
        #: replaced included: their changes are the user's all the same.
        self.settling: dict[FeatureRow, int] = {}
        self.writing: set[FeatureRow] = set()
        #: What runs once every change made has landed (see ``_after_edits``),
        #: by name, so asking twice does not run it twice.
        self._after_edits_land: dict[str, Callable[[], None]] = {}
        #: An operation asked for while another ran, run when it ends.
        self._deferred: Callable[[], None] | None = None
        #: Asks for a profile name; replaceable, since a modal dialog cannot
        #: be answered from a test.
        self.ask_name = ask_name
        self.stale = True
        #: A reload asked for while busy, run when it ends: ``None`` for none,
        #: otherwise whether it should probe.
        self._load_wanted: bool | None = None
        #: Calibration windows still open, to follow a theme switch.
        self._calibrations: list[CalibrationWindow] = []
        #: Whether the "not available" list is unfolded; kept across redraws.
        self.show_unavailable = False
        self._loaded_once = False
        self._status_after: str | None = None

        root.title("monitorctl")
        if icon_path:
            try:
                root.iconbitmap(default=icon_path)
            except tk.TclError:
                _log.info("could not set the window icon from %s", icon_path)
        root.minsize(760, 480)
        geometry = bridge.settings.get().window_geometry
        root.geometry(geometry if geometry and self._on_screen(geometry) else "1180x760")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self._callback_failed
        disable_wheel_on_controls(root)

        self.palette = apply_theme(root, bridge.settings.get().theme)
        self._icons = ICON_FONT in tkfont.families(root)
        self._build()
        self._bind_keys()
        self._apply_title_bar()

    # Construction ----------------------------------------------------------- #

    def _build(self) -> None:
        toolbar = ttk.Frame(self.root, style="Toolbar.TFrame", padding=(12, 8))
        toolbar.pack(side="top", fill="x")
        self.auto_apply_var = tk.BooleanVar(
            self.root, value=self.bridge.settings.get().auto_apply
        )
        self.auto_check = auto = ttk.Checkbutton(
            toolbar,
            text="Auto apply",
            variable=self.auto_apply_var,
            command=self._toggle_auto_apply,
        )
        auto.pack(side="left")
        self.apply_button = ttk.Button(toolbar, text="Apply", command=self.apply)
        self.apply_button.pack(side="left", padx=(8, 0))
        self.discard_button = ttk.Button(
            toolbar, text="Discard", style="Link.TButton", command=self.discard_staged
        )
        self.discard_button.pack(side="left", padx=(4, 0))
        self.save_button = ttk.Button(
            toolbar, text="Save", style="Accent.TButton", command=self.save
        )
        self.save_button.pack(side="left", padx=(18, 0))
        self.restore_button = ttk.Button(toolbar, text="Restore", command=self.restore)
        self.restore_button.pack(side="left", padx=(6, 0))
        self.rescan_button = ttk.Button(
            toolbar, text="Rescan", command=lambda: self.load(probe=True)
        )
        self.rescan_button.pack(side="left", padx=(18, 0))
        ttk.Button(toolbar, text="Calibration", command=self.open_calibration).pack(
            side="left", padx=(6, 0)
        )
        self.theme_button = ttk.Button(toolbar, width=3, command=self.toggle_theme)
        self.theme_button.pack(side="right")
        self.snapshot_label = ttk.Label(toolbar, style="Toolbar.TLabel")
        self.snapshot_label.pack(side="right", padx=(0, 14))
        Tooltip(self.snapshot_label, self._snapshot_tooltip, lambda: self.palette)
        for button, text in (
            (
                self.save_button,
                "Store every current value in the profile each monitor is using, and "
                "push the ones a monitor forgets (Ctrl+S)",
            ),
            (
                self.restore_button,
                "Put back the values of the profile each monitor is using",
            ),
            (self.rescan_button, "Read every control of every monitor again (F5)"),
            (self.theme_button, "Switch between dark and light"),
            (
                auto,
                "On: every change goes to the monitor at once. Off: changes wait "
                "until Apply, so several can be set and judged together",
            ),
            (
                self.apply_button,
                "Write the changes waiting in the window. With none waiting, send "
                "what the window shows again to the controls a monitor forgets - "
                "after it was switched off, for instance",
            ),
            (self.discard_button, "Forget the changes waiting in the window"),
        ):
            Tooltip(button, text, lambda: self.palette)

        status = ttk.Frame(self.root, style="Status.TFrame", padding=(12, 4))
        status.pack(side="bottom", fill="x")
        # Beside the message that says what is being done; shown while busy.
        self.spinner = Spinner(
            status,
            size=14,
            background=lambda: self.palette.surface,
            colour=lambda: self.palette.accent,
        )
        self.status_label = ttk.Label(status, style="Status.TLabel")
        self.status_label.pack(side="left")
        self.keeper_label = ttk.Label(status, style="Status.TLabel")
        self.keeper_label.pack(side="right")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(side="top", fill="both", expand=True, padx=8, pady=(6, 8))
        self.notebook.enable_traversal()
        # Stands for the monitor tabs until there are any: while the first
        # read runs, with a spinner where the monitors will appear.
        self.placeholder = ttk.Frame(self.notebook)
        centre = ttk.Frame(self.placeholder)
        centre.place(relx=0.5, rely=0.5, anchor="center")
        self.placeholder_spinner = Spinner(
            centre,
            size=28,
            background=lambda: self.palette.bg,
            colour=lambda: self.palette.accent,
        )
        self.placeholder_label = ttk.Label(
            centre, text="Reading monitors…", style="Dim.TLabel", justify="center"
        )
        self.placeholder_label.pack(side="bottom")
        self.notebook.add(self.placeholder, text="Monitors")
        self.settings_tab = self._build_settings()
        self.notebook.add(self.settings_tab, text="Settings")
        self._refresh_state()

    def _build_settings(self) -> ttk.Frame:
        settings = self.bridge.settings.get()
        outer = ScrollFrame(self.notebook, self.palette)
        page = outer.body
        page.configure(padding=16)

        keep = ttk.Labelframe(page, text="Keep settings applied")
        keep.pack(fill="x", pady=(0, 12))
        ttk.Label(
            keep,
            text=(
                "Some monitors forget part of their settings when switched off but keep "
                "reporting them. monitorctl pushes those controls again when needed. "
                "Monitors that keep their settings are never written to automatically."
            ),
            style="Card.TLabel",
            wraplength=640,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))

        self.paused_var = tk.BooleanVar(self.root, value=settings.paused)
        ttk.Checkbutton(
            keep,
            text="Pause automatic re-apply",
            variable=self.paused_var,
            command=self._settings_edited,
        ).pack(anchor="w", pady=2)

        interval_row = ttk.Frame(keep, style="Card.TFrame")
        interval_row.pack(anchor="w", pady=(6, 2))
        ttk.Label(interval_row, text="Check every", style="Card.TLabel").pack(side="left")
        self.interval_var = tk.StringVar(self.root, value=str(settings.interval_minutes))
        interval = ttk.Spinbox(
            interval_row,
            from_=MIN_INTERVAL_MINUTES,
            to=MAX_INTERVAL_MINUTES,
            width=6,
            textvariable=self.interval_var,
            command=self._settings_edited,
        )
        interval.pack(side="left", padx=6)
        interval.bind("<Return>", lambda _e: self._settings_edited())
        interval.bind("<FocusOut>", lambda _e: self._settings_edited())
        ttk.Label(interval_row, text="minutes", style="Card.TLabel").pack(side="left")

        self.mode_var = tk.StringVar(self.root, value=settings.mode.value)
        ttk.Radiobutton(
            keep,
            text="Detect when a monitor comes back on  (reads only; writes only after a power cycle)",
            value=RefreshMode.DETECT.value,
            variable=self.mode_var,
            command=self._settings_edited,
        ).pack(anchor="w", pady=(8, 2))
        ttk.Radiobutton(
            keep,
            text="Rewrite forgotten controls on every check  (one step away and back; invisible)",
            value=RefreshMode.REWRITE.value,
            variable=self.mode_var,
            command=self._settings_edited,
        ).pack(anchor="w", pady=2)

        self.apply_start_var = tk.BooleanVar(self.root, value=settings.apply_on_start)
        ttk.Checkbutton(
            keep,
            text="Apply saved settings when monitorctl starts",
            variable=self.apply_start_var,
            command=self._settings_edited,
        ).pack(anchor="w", pady=(8, 2))

        startup = ttk.Labelframe(page, text="Startup")
        startup.pack(fill="x", pady=(0, 12))
        self.autostart_var = tk.BooleanVar(self.root, value=self._autostart_enabled())
        ttk.Checkbutton(
            startup,
            text="Start with Windows",
            variable=self.autostart_var,
            command=self._toggle_autostart,
        ).pack(anchor="w")
        ttk.Label(
            startup,
            text="Also listed in Task Manager → Startup apps, where it can be turned off.",
            style="Card.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        files = ttk.Labelframe(page, text="Files")
        files.pack(fill="x", pady=(0, 12))
        ttk.Label(
            files,
            text=f"Saved settings, preferences and the log live in\n{state_directory()}",
            style="Card.TLabel",
            justify="left",
        ).pack(anchor="w")
        ttk.Button(files, text="Open folder", command=self._open_state_dir).pack(
            anchor="w", pady=(8, 0)
        )

        ttk.Label(page, text=f"monitorctl {__version__}", style="Faint.TLabel").pack(
            anchor="w", pady=(4, 0)
        )
        return outer

    def _bind_keys(self) -> None:
        self.root.bind("<Control-s>", lambda _e: self.save())
        self.root.bind("<F5>", lambda _e: self.load(probe=True))
        self.root.bind("<Escape>", lambda _e: self.close())

    def _apply_title_bar(self) -> None:
        try:
            self.root.update_idletasks()
            hwnd = int(self.root.wm_frame(), 16)
            win32.enable_dark_title_bar(
                hwnd, self.bridge.settings.get().theme is Theme.DARK
            )
        except (tk.TclError, ValueError, OSError):
            pass

    # Showing and hiding ------------------------------------------------------ #

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.refresh_keeper_label()
        if self.stale:
            self.load(probe=not self._loaded_once)

    def close(self) -> None:
        """Hide to the tray, offering to deal with unsaved changes first."""
        self._settle_then(self._hide, cancel="keep the window open")

    def confirm_exit(self, proceed: Callable[[], None]) -> None:
        """Exit was chosen in the tray: offer to deal with unsaved changes first.

        Exiting with them is not neutral: the next start re-applies the saved
        settings, silently undoing the unsaved ones on every forgetful
        control.  ``proceed`` runs once they are settled, or at once if there
        are none.
        """
        hidden = self.root.state() == "withdrawn"
        # Busy, a change sent now would be refused; _settle_then says to wait.
        if (
            not hidden
            and not self.busy
            and self._after_edits("exit", lambda: self.confirm_exit(proceed))
        ):
            return
        if hidden or not (self.drifted() or self.staged):
            proceed()
            return
        # Brought forward without show(): that may start a reload, and a busy
        # window cannot settle anything.
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._settle_then(proceed, cancel="do not exit")

    def _settle_then(self, then: Callable[[], None], *, cancel: str) -> None:
        if self.busy:
            self.status("Wait for the current operation to finish.")
            return
        # A drag let go a moment ago is not in the values yet: without this,
        # closing within the debounce would ask nothing about it.
        if self._after_edits("close", lambda: self._settle_then(then, cancel=cancel)):
            return
        if self.staged:
            # Changes not written yet come first: they decide what "unsaved"
            # even means.
            answer = messagebox.askyesnocancel(
                "Changes not applied",
                f"{len(self.staged)} change(s) are waiting in the window.\n\n"
                "Yes: apply them.\nNo: discard them.\n"
                f"Cancel: {cancel}.",
                parent=self.root,
                icon="warning",
            )
            if answer is None or not self._free_after_dialog():
                return
            if answer:
                self.apply_staged(then=lambda: self._settle_then(then, cancel=cancel))
                return
            self.discard_staged()
        if not self.drifted():
            then()
            return
        answer = messagebox.askyesnocancel(
            "Unsaved changes",
            "Some settings differ from the profiles in use.\n\n"
            "Yes: save them into those profiles.\n"
            "No: put the profiles' values back.\n"
            f"Cancel: {cancel}.",
            parent=self.root,
            icon="warning",
        )
        if answer is None or not self._free_after_dialog():
            return
        if answer:
            self.save(then=then)
        else:
            self.restore(then=then)

    def _hide(self) -> None:
        with contextlib.suppress(TypeError, ValueError):
            self.bridge.settings.update(window_geometry=self.root.geometry())
        self.root.withdraw()
        self.bridge.set_editing(False)
        self.bridge.window_hidden()

    def _on_screen(self, geometry: str) -> bool:
        """Whether a saved geometry still lands on a connected monitor."""
        try:
            position = geometry.split("+", 1)[1].replace("+-", "+ -").replace("+", " ")
            x, y = (int(part) for part in position.split())
        except (IndexError, ValueError):
            return False
        left = self.root.winfo_vrootx()
        top = self.root.winfo_vrooty()
        width = self.root.winfo_vrootwidth()
        height = self.root.winfo_vrootheight()
        return left - 100 <= x <= left + width - 100 and top <= y <= top + height - 100

    # Loading and rendering -------------------------------------------------- #

    def load(self, *, probe: bool) -> None:
        """Fetch monitors and redraw.

        ``probe`` re-reads every control.  Without it the cached values are
        used, and read only where the service has none.

        Asked while another operation runs - a keeper rescan, a tray save -
        it is not dropped: it runs when that operation ends.
        """
        if self.busy:
            self._load_wanted = probe or bool(self._load_wanted)
            return
        self._load_wanted = None
        self._set_busy(
            True, "Reading every control of every monitor…" if probe else "Loading…"
        )

        def fetch(
            service: MonitorService,
        ) -> tuple[tuple[MonitorState, ...], dict[str, MonitorProfiles | None]]:
            monitors = service.list_monitors(probe=False)
            if probe or not all(m.probed for m in monitors):
                monitors = service.refresh(probe=True)
            return monitors, service.profiles_by_monitor()

        self.bridge.worker.submit(fetch, self._loaded, label="Reading monitors")

    def _loaded(self, result: object, error: BaseException | None) -> None:
        self._set_busy(False)
        if error is not None:
            self.status(f"Could not read the monitors: {error}", error=True)
            return
        self.monitors, self.profiles = result
        self.stale = False
        self._loaded_once = True
        self.render()
        count = len(self.monitors)
        self.status(
            f"{count} monitor{'s' if count != 1 else ''} found."
            if count
            else "No DDC/CI monitor found."
        )

    def render(self) -> None:
        selected = self._selected_monitor_name()
        for tab in self.notebook.tabs():
            widget = self.notebook.nametowidget(tab)
            if widget is not self.settings_tab:
                self.notebook.forget(tab)
                if widget is not self.placeholder:
                    widget.destroy()
        self.rows.clear()
        self.profile_bars.clear()
        self.values = {(m.id, f.key): f.value for m in self.monitors for f in m.features}

        if not self.monitors:
            self.placeholder_label.configure(
                text="No DDC/CI-capable monitor was found.\n"
                "Check that DDC/CI is enabled in the monitor's own menu, then press Rescan."
            )
            self.notebook.insert(0, self.placeholder, text="Monitors")
        for position, monitor in enumerate(self.monitors):
            tab = self._monitor_tab(monitor)
            self.notebook.insert(position, tab, text=f"  {monitor.name}  ")
            if monitor.name == selected:
                self.notebook.select(tab)
                selected = None
        if self.monitors and selected is not None:
            # Nothing to stay on (first load, or that monitor is gone): start
            # on the first monitor, not on whatever tab Tk falls back to.
            self.notebook.select(0)
        self._prune_staged()
        self._refresh_state()

    def _selected_monitor_name(self) -> str | None:
        try:
            current = self.notebook.select()
            text = self.notebook.tab(current, "text").strip()
        except tk.TclError:
            return None
        return text if text and text != "Settings" else None

    def _monitor_tab(self, monitor: MonitorState) -> ScrollFrame:
        outer = ScrollFrame(self.notebook, self.palette)
        page = outer.body
        page.configure(padding=(12, 10))
        content = _page(monitor)

        shown = sum(len(members) for _section, members in content.sections)
        facts = [
            monitor.device,
            monitor.hardware_id or "EDID unavailable",
            f"MCCS {monitor.mccs_version}" if monitor.mccs_version else "",
            f"{shown} controls",
        ]
        facts_label = ttk.Label(
            page, text="   ·   ".join(f for f in facts if f), style="Dim.TLabel"
        )
        facts_label.pack(anchor="w")
        if monitor.warnings:
            # Parser diagnostics: worth finding, not worth a banner.
            Tooltip(
                facts_label,
                "Notes from reading this monitor:\n" + "\n".join(monitor.warnings),
                lambda: self.palette,
            )
        bar = ProfileBar(self, monitor)
        self.profile_bars[monitor.id] = bar
        bar.build(page)
        if monitor.volatile:
            note = ttk.Label(
                page,
                text=(
                    "This monitor forgets its colour gains and black levels when it is "
                    "switched off, while still reporting them. monitorctl puts them back "
                    "automatically (Settings tab); Restore does it on demand."
                ),
                style="Note.TLabel",
                justify="left",
            )
            note.pack(anchor="w", fill="x", pady=(8, 0))
            note.bind(
                "<Configure>", lambda e: note.configure(wraplength=max(200, e.width - 24))
            )

        grid = ttk.Frame(page)
        grid.pack(fill="both", expand=True, pady=(12, 0))
        cards = [
            self._section_card(grid, monitor, section, members)
            for section, members in content.sections
        ]
        spare = self._unavailable_card(grid, content.unavailable)

        state = {"columns": 0}

        def layout(_event: object = None) -> None:
            width = max(outer.canvas.winfo_width(), COLUMN_WIDTH)
            columns = max(1, width // COLUMN_WIDTH)
            previous = state["columns"]
            if columns == previous:
                return
            state["columns"] = columns
            for column in range(max(columns, previous)):
                # Only the columns in use share the uniform group: an empty
                # column in the group would take as much width as a full one.
                if column < columns:
                    grid.columnconfigure(column, weight=1, uniform="col")
                else:
                    grid.columnconfigure(column, weight=0, uniform="")
            for index, card in enumerate(cards):
                card.grid(
                    row=index // columns,
                    column=index % columns,
                    sticky="nsew",
                    padx=5,
                    pady=5,
                )
            if spare is not None:
                spare.grid(
                    row=-(-len(cards) // columns),  # the row after the last card
                    column=0,
                    columnspan=columns,
                    sticky="ew",
                    padx=5,
                    pady=(10, 5),
                )

        outer.canvas.bind("<Configure>", layout, add="+")
        layout()
        return outer

    def _section_card(
        self,
        parent: ttk.Frame,
        monitor: MonitorState,
        section: Section,
        members: tuple[FeatureState, ...],
    ) -> ttk.Labelframe:
        """A card: icon and title, what the controls change, the controls."""
        category = CATEGORIES.get(section.key)
        title = section.title or (category.label if category else section.key)
        header = ttk.Frame(parent, style="Card.TFrame")
        if section.icon and self._icons:
            ttk.Label(header, text=section.icon, style="CardIcon.TLabel").pack(
                side="left", padx=(0, 6)
            )
        ttk.Label(header, text=title, style="CardTitle.TLabel").pack(side="left")
        # Where the change happens, in words: the monitor's own hardware, or
        # software on this PC that bends the signal on top of it.
        tag = ttk.Label(
            header,
            text="PC software" if section.on_pc else "monitor",
            style="TagPc.TLabel" if section.on_pc else "TagMonitor.TLabel",
        )
        tag.pack(side="left", padx=(8, 0))
        Tooltip(
            tag,
            "Applied by this PC's graphics card or driver, not by the monitor: it "
            "changes the signal, on top of the monitor's own settings, and only "
            "while monitorctl runs."
            if section.on_pc
            else "Stored and applied by the monitor itself, over DDC/CI.",
            lambda: self.palette,
        )
        card = ttk.Labelframe(parent, labelwidget=header)
        if category is not None:
            Tooltip(header, category.description, lambda: self.palette)
        if section.hint:
            hint = ttk.Label(card, text=section.hint, style="Hint.TLabel", justify="left")
            hint.grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 4))
            # Wrap to the card, which grows with the window.
            card.bind(
                "<Configure>",
                lambda e: hint.configure(wraplength=max(120, e.width - 30)),
                add="+",
            )
        card.columnconfigure(2, weight=1)
        ident = self._ident(monitor.id)
        for index, feature in enumerate(members, start=1):
            row = FeatureRow(self, monitor.id, feature, ident=ident)
            row.build(card, index)
            self.rows[(monitor.id, feature.key)] = row
        return card

    def _unavailable_card(
        self, parent: ttk.Frame, features: tuple[FeatureState, ...]
    ) -> ttk.Labelframe | None:
        """The controls this monitor does not have, folded away by default.

        Still listed, so it stays obvious what exists and what this monitor
        lacks; never as a column of dead sliders burying the working ones.
        """
        if not features:
            return None
        # The title sits inside the frame, not in its label: folded, the card
        # has no other content, and an empty labelframe collapses to a line.
        card = ttk.Labelframe(parent)
        header = ttk.Frame(card, style="Card.TFrame")
        header.pack(anchor="w", fill="x")
        ttk.Label(
            header,
            text=f"Not available on this monitor ({len(features)})",
            style="CardTitle.TLabel",
        ).pack(side="left")
        names = ttk.Label(
            card,
            text="   ·   ".join(f.label for f in features),
            style="Hint.TLabel",
            wraplength=1000,
            justify="left",
        )
        toggle = ttk.Button(header, style="Link.TButton")

        def paint() -> None:
            if self.show_unavailable:
                names.pack(anchor="w", fill="x", pady=(6, 0))
                toggle.configure(text="Hide")
            else:
                names.pack_forget()
                toggle.configure(text="Show")

        def flip() -> None:
            self.show_unavailable = not self.show_unavailable
            paint()

        toggle.configure(command=flip)
        toggle.pack(side="left", padx=(10, 0))
        Tooltip(
            card,
            "The monitor did not answer these over DDC/CI. Most monitors implement "
            "only part of the standard; their own menu may still have them.",
            lambda: self.palette,
        )
        names.bind("<Configure>", lambda e: names.configure(wraplength=max(200, e.width)))
        paint()
        return card

    # Saved settings -------------------------------------------------------- #

    def volatile(self, monitor_id: str) -> frozenset[str]:
        return next(
            (m.volatile for m in self.monitors if m.id == monitor_id), frozenset()
        )

    def record(self, monitor_id: str, key: str, value: int) -> None:
        """Note a confirmed write, for the unsaved-changes hint."""
        self.values[(monitor_id, key)] = value
        self._refresh_state()

    def saved_value(self, monitor_id: str, key: str) -> int | None:
        """The value of ``key`` in the monitor's profile in use, if any.

        ``None`` for a control inert on the monitor's input: its reading is
        noise, so it is never compared with the profile (D32).
        """
        if key in self._inert(monitor_id):
            return None
        entry = self.profiles.get(monitor_id)
        active = entry.active_profile if entry else None
        return None if active is None else active.values.get(key)

    def _inert(self, monitor_id: str) -> frozenset[str]:
        return inert_keys(self.values.get((monitor_id, "input_source")))

    def has_saved(self) -> bool:
        """Whether any connected monitor has a profile in use."""
        return any(e is not None and e.active_profile for e in self.profiles.values())

    def differences(self, monitor_id: str | None = None) -> list[str]:
        """Every control that differs from its monitor's profile in use, in words.

        Args:
            monitor_id: Only this monitor; ``None`` for all of them.
        """
        found: list[str] = []
        for monitor in self.monitors:
            if monitor_id is not None and monitor.id != monitor_id:
                continue
            entry = self.profiles.get(monitor.id)
            active = entry.active_profile if entry else None
            if active is None:
                continue
            inert = self._inert(monitor.id)
            for key, value in active.values.items():
                if key in inert:
                    continue
                current = self.values.get((monitor.id, key))
                if current is not None and current != value:
                    feature = monitor.feature(key)
                    label = feature.label if feature else key
                    found.append(
                        f"{monitor.name} · {label}: saved {value}, now {current}"
                    )
        return found

    def drifted(self) -> bool:
        return bool(self.differences())

    def _snapshot_tooltip(self) -> str:
        if not self.has_saved():
            return "Nothing has been saved yet. Save stores every current value."
        differences = self.differences()
        if not differences:
            return "Everything on screen matches the profiles in use."
        shown = differences[:12]
        more = len(differences) - len(shown)
        tail = [f"… and {more} more"] if more else []
        return "\n".join(["Differs from the profiles in use:", *shown, *tail])

    def save(self, *, then: Callable[[], None] | None = None) -> None:
        if self.busy:
            self._when_free(lambda: self.save(then=then), "Save")
            return
        # What is saved is what the window shows, the last drag included:
        # its write is sent now instead of after the debounce, and the save
        # waits for it.
        if self._after_edits("save", lambda: self.save(then=then)):
            return
        if self.staged:
            # Save keeps what is on screen, so what waits is applied first.
            self.apply_staged(then=lambda: self.save(then=then))
            return
        self._set_busy(True, "Saving…")

        def run(
            service: MonitorService,
        ) -> tuple[Snapshot, RestoreReport, dict[str, MonitorProfiles | None]]:
            snapshot = service.save_snapshot()
            # Push the controls a monitor forgets, so what is saved is also
            # what the screen shows - the read-back cannot tell otherwise.
            report = service.reassert_saved(reassert=Reassert.PIVOT)
            return snapshot, report, service.profiles_by_monitor()

        def done(result: object, error: BaseException | None) -> None:
            self._set_busy(False)
            if error is not None:
                self.status(f"Could not save: {error}", error=True)
                return
            snapshot, report, self.profiles = result
            pushed = len([o for o in report.applied if not o.key.startswith("gpu_")])
            message = f"Saved {snapshot.value_count} settings."
            if pushed:
                message += f" Pushed {pushed} that a monitor forgets."
            if report.failed:
                message += f" {len(report.failed)} could not be pushed: {report.failed[0].reason}"
            self.status(message, error=bool(report.failed), ok=not report.failed)
            self._refresh_state()
            if then is not None:
                then()

        self.bridge.worker.submit(run, done, label="Saving")

    def restore(self, *, then: Callable[[], None] | None = None) -> None:
        if self.busy:
            self._when_free(lambda: self.restore(then=then), "Restore")
            return
        if not self.has_saved():
            self.status("Nothing has been saved yet.", error=True)
            return
        # A drag still settling would land after the restore and undo it:
        # dropped, and a write already on its way is waited for.
        if self._after_edits("restore", lambda: self.restore(then=then), keep=False):
            return
        # The saved values replace whatever was waiting.
        self.discard_staged()
        self._set_busy(True, "Restoring…")

        def run(
            service: MonitorService,
        ) -> tuple[
            RestoreReport, tuple[MonitorState, ...], dict[str, MonitorProfiles | None]
        ]:
            report = service.restore_snapshot(reassert=Reassert.PIVOT)
            return report, service.list_monitors(), service.profiles_by_monitor()

        def done(result: object, error: BaseException | None) -> None:
            self._set_busy(False)
            if error is not None:
                self.status(f"Could not restore: {error}", error=True)
                return
            report, self.monitors, self.profiles = result
            self.render()
            message = f"Restored: {len(report.applied)} written, {report.unchanged} already correct."
            problems = list(report.failed) + list(report.skipped)
            if problems:
                message += f" {len(problems)} not restored: {problems[0].reason}"
            self.status(message, error=bool(report.failed), ok=not problems)
            if then is not None:
                then()

        self.bridge.worker.submit(run, done, label="Restoring")

    # Changes waiting for Apply ---------------------------------------------- #

    @property
    def auto_apply(self) -> bool:
        return self.bridge.settings.get().auto_apply

    def _ident(self, monitor_id: str) -> str:
        """What a waiting change is filed under.

        The monitor's EDID identity, which survives Windows renumbering
        ``\\\\.\\DISPLAYn`` under a rescan - filed by position, a change made
        on one monitor would be applied to whichever took its place.  The
        position only when the identity is missing or shared by two panels.
        """
        monitor = next((m for m in self.monitors if m.id == monitor_id), None)
        if monitor is None or not monitor.hardware_id:
            return monitor_id
        twins = sum(1 for m in self.monitors if m.hardware_id == monitor.hardware_id)
        return monitor.hardware_id if twins == 1 else monitor_id

    def _monitor_id_of(self, ident: str) -> str | None:
        return next((m.id for m in self.monitors if self._ident(m.id) == ident), None)

    def live_row(self, ident: str, key: str) -> FeatureRow | None:
        """The row on screen for a control of the monitor filed as ``ident``."""
        monitor_id = self._monitor_id_of(ident)
        return None if monitor_id is None else self.rows.get((monitor_id, key))

    def locator(self, monitor_id: str) -> Callable[[MonitorService], str]:
        """How a job finds, on the bus thread, the monitor the user acted on.

        A job waits in the queue behind whatever is there - a keeper rescan
        among them, after which Windows may have handed this position to the
        other monitor.  The job finds the monitor again by its EDID identity
        when it runs (:meth:`MonitorService.resolve`), and fails rather than
        write the wrong one if it is gone.
        """
        monitor = next((m for m in self.monitors if m.id == monitor_id), None)
        hardware_id = monitor.hardware_id if monitor is not None else ""
        return lambda service: service.resolve(hardware_id, monitor_id)

    # Changes not landed yet ------------------------------------------------ #

    def _edits_pending(self) -> bool:
        return bool(
            self.settling or self.writing or any(row.typed for row in self.rows.values())
        )

    def _after_edits(
        self, name: str, action: Callable[[], None], *, keep: bool = True
    ) -> bool:
        """Make ``action`` wait for the changes the user made to land.

        A change is not in the values until its write comes back: a drag
        waits for the value to settle (``DEBOUNCE_MS``), then for the bus.
        Save, closing and exiting act on the values, so they would miss a
        change made a moment before.  With ``keep``, a change still settling
        is sent now rather than after the delay - the user's own idea of it
        was settled when they pressed Save; without it (Restore, which puts
        other values over it) it is dropped.  A write already on its way is
        waited for either way: the bus runs it before anything queued later,
        but its result has to be drawn first.

        Returns whether ``action`` was put off; it then runs, once, when the
        last write lands (asking again under the same ``name`` replaces it).
        """
        if not self._edits_pending():
            return False
        for row in {*self.settling, *self.writing, *self.rows.values()}:
            if keep:
                row.flush()
            else:
                row.cancel_debounce()
                row.typed = False
                row.pending = None
        if not self.writing:
            # Nothing left on its way (staged, or dropped): go on at once.
            return False
        self._after_edits_land[name] = action
        return True

    def edits_landed(self) -> None:
        """A write came back: run what waited for all of them."""
        if self.writing or not self._after_edits_land:
            return
        waiting = list(self._after_edits_land.values())
        self._after_edits_land.clear()
        for action in waiting:
            action()

    def _when_free(self, action: Callable[[], None], what: str) -> None:
        """Run ``action`` when the operation in progress ends.

        For Save and Restore from the tray or the keyboard, which reach the
        window while the toolbar's buttons are disabled: dropped silently,
        the user would believe they had saved.
        """
        self._deferred = action
        self.status(f"{what} will run when the current operation ends…", sticky=True)

    def staged_value(self, monitor_id: str, key: str) -> int | None:
        return self.staged.get((self._ident(monitor_id), key))

    def staged_count(self, monitor_id: str | None = None) -> int:
        if monitor_id is None:
            return len(self.staged)
        ident = self._ident(monitor_id)
        return sum(1 for owner, _key in self.staged if owner == ident)

    def _prune_staged(self) -> None:
        """After a redraw: drop waiting changes whose monitor or control is gone."""
        for ident, key in list(self.staged):
            monitor_id = self._monitor_id_of(ident)
            row = self.rows.get((monitor_id, key)) if monitor_id else None
            if row is None or row.revert is None:
                del self.staged[(ident, key)]

    def stage(self, row: FeatureRow, value: int) -> None:
        """Hold a change in the window until Apply (auto apply off).

        A change back to the value the monitor has is no change - except on a
        forgetful control, where re-sending the reported value is how the
        screen is made to match it again, and while a write is still on its
        way, when the monitor's value is not known yet.
        """
        key = (row.ident, row.key)
        same = value == row.confirmed and not row.in_flight
        if same and row.key not in self.volatile(row.monitor_id):
            self.staged.pop(key, None)
        else:
            self.staged[key] = value
        row.mark_staged(key in self.staged)
        self._refresh_state()

    def unstage(self, row: FeatureRow) -> None:
        """Forget one control's waiting change; it shows the monitor's value."""
        if self.staged.pop((row.ident, row.key), None) is not None:
            row.mark_staged(False)
            row.show(row.confirmed)
            self._refresh_state()

    def apply_staged(
        self,
        *,
        monitor_id: str | None = None,
        then: Callable[[], None] | None = None,
    ) -> None:
        """Write the changes waiting in the window, coarse controls first.

        Args:
            monitor_id: Only this monitor's; ``None`` for all.
            then: Run once every one of them has been written.
        """
        if self.busy:
            return
        # A drag let go a moment ago is part of what waits.
        if self._after_edits(
            "apply", lambda: self.apply_staged(monitor_id=monitor_id, then=then)
        ):
            return
        ident = None if monitor_id is None else self._ident(monitor_id)
        order = {key: index for index, key in enumerate(FEATURES)}
        items = sorted(
            (
                (self.locator(owner_id), key, value, owner)
                for (owner, key), value in self.staged.items()
                if (ident is None or owner == ident)
                and (owner_id := self._monitor_id_of(owner)) is not None
            ),
            key=lambda item: order.get(item[1], len(order)),
        )
        if not items:
            if then is not None:
                then()
            return
        self._set_busy(True, f"Applying {len(items)} change(s)…")

        def run(service: MonitorService) -> list[tuple[str, str, object]]:
            results: list[tuple[str, str, object]] = []
            for locate, key, value, owner in items:
                try:
                    outcome: object = service.write_feature(locate(service), key, value)
                except DdcError as exc:
                    outcome = exc
                results.append((owner, key, outcome))
            return results

        def done(results: object, error: BaseException | None) -> None:
            self._set_busy(False)
            if error is not None:
                self.status(f"Could not apply: {error}", error=True)
                return
            failed: list[str] = []
            for owner, key, outcome in results:
                if isinstance(outcome, BaseException):
                    # Kept waiting, so it can be applied again or discarded.
                    failed.append(f"{key}: {outcome}")
                    continue
                self.staged.pop((owner, key), None)
                confirmed = (
                    outcome.confirmed
                    if outcome.confirmed is not None
                    else outcome.applied
                )
                # Under the monitor's id in the window's list, whatever the
                # bus called it: the two may differ until the next reload.
                target = self._monitor_id_of(owner)
                if target is not None:
                    self.values[(target, key)] = confirmed
                row = self.live_row(owner, key)
                if row is not None:
                    row.mark_staged(False)
                    row.confirm(outcome, None)
            self._refresh_state()
            if failed:
                self.status(
                    f"{len(failed)} change(s) not applied - {failed[0]}", error=True
                )
                return
            self.status(f"Applied {len(results)} change(s).", ok=True)
            if then is not None:
                then()

        self.bridge.worker.submit(run, done, label="Applying")

    def discard_staged(self, monitor_id: str | None = None) -> None:
        """Forget waiting changes: those controls show the monitor's values."""
        ident = None if monitor_id is None else self._ident(monitor_id)
        for owner, key in list(self.staged):
            if ident is not None and owner != ident:
                continue
            del self.staged[(owner, key)]
            target = self._monitor_id_of(owner)
            row = self.rows.get((target, key)) if target else None
            if row is not None:
                row.mark_staged(False)
                row.show(row.confirmed)
        self._refresh_state()

    def apply(self) -> None:
        """The Apply button: write what waits - or, with nothing waiting,
        push what the window shows to the controls a monitor forgets."""
        if self.busy:
            return
        if self._after_edits("apply", self.apply):
            return
        if self.staged:
            self.apply_staged()
        else:
            self.push_current()

    def push_current(self) -> None:
        """Send what the window shows again to the controls a monitor forgets.

        Nothing waits, yet the screen may not show what the window does: a
        forgetful monitor (``ddc/quirks.py``) switched off and on where no
        event reached the keeper, or while it was paused - and its read-back
        cannot tell.  This is the user's way to make it so: the push Save
        ends with, but of the values on screen, saved or not.  The controls
        a monitor keeps are not written: their read-back is the truth, and
        another write would only cost an EEPROM cycle.
        """
        if self.busy or not self.monitors:
            return
        self._set_busy(True, "Applying what the window shows…")

        def run(service: MonitorService) -> RestoreReport:
            return service.reassert_current(reassert=Reassert.PIVOT)

        def done(report: object, error: BaseException | None) -> None:
            self._set_busy(False)
            if error is not None:
                self.status(f"Could not apply: {error}", error=True)
                return
            if not report.applied and not report.failed:
                self.status(
                    "Nothing to apply: no change waits, and no monitor here forgets "
                    "its settings.",
                    ok=True,
                )
                return
            message = f"Applied what the window shows: {len(report.applied)} written."
            if report.failed:
                message += f" {len(report.failed)} failed: {report.failed[0].reason}"
            self.status(message, error=bool(report.failed), ok=not report.failed)

        self.bridge.worker.submit(run, done, label="Applying")

    def _toggle_auto_apply(self) -> None:
        on = self.auto_apply_var.get()
        if self.busy or self.writing:
            # Switching modes mid-write would leave a change neither applied
            # nor waiting.
            self.auto_apply_var.set(not on)
            self.status("Wait for the current operation to finish.")
            return
        self.bridge.settings.update(auto_apply=on)
        if on and self.staged:
            # "Auto apply" means what is on screen goes to the monitor.
            self.apply_staged()
        self._refresh_state()

    def _stage_profile(self, monitor_id: str) -> None:
        """Show the active profile's values as changes waiting for Apply."""
        entry = self.profiles.get(monitor_id)
        active = entry.active_profile if entry else None
        if active is None:
            return
        # As the service's profile switch: nothing inert on the current input
        # is written, so nothing inert waits for Apply either (D32).
        inert = self._inert(monitor_id)
        for key, value in active.values.items():
            if key in inert:
                continue
            row = self.rows.get((monitor_id, key))
            if row is None or row.revert is None:  # absent or read-only
                continue
            row.show(value)
            self.stage(row, value)

    # Profiles -------------------------------------------------------------- #

    def _monitor(self, monitor_id: str) -> MonitorState:
        return next(m for m in self.monitors if m.id == monitor_id)

    def _free_after_dialog(self, monitor_id: str | None = None) -> bool:
        """Whether an action the user just confirmed can still go ahead.

        A modal dialog runs its own event loop, and the worker's results keep
        arriving in it: a keeper reload may have started, or the monitor gone,
        while the question was on screen.
        """
        if self.busy:
            self.status("Wait for the current operation to finish, then try again.")
            return False
        if monitor_id is not None and all(m.id != monitor_id for m in self.monitors):
            self.status("That monitor is no longer connected.", error=True)
            return False
        return True

    def switch_profile(self, monitor_id: str, name: str) -> None:
        """Use another profile on one monitor.

        With auto apply on, its values go to the monitor at once; off, they
        are shown as changes waiting for Apply.  Changes not saved to the
        profile being left - written or still waiting - are not thrown away
        silently: they can be saved into it first.
        """
        bar = self.profile_bars.get(monitor_id)
        if self.busy:
            self.status("Wait for the current operation to finish.")
            if bar is not None:
                bar.refresh()
            return
        # The question below is about every change made, the last drag too.
        if self._after_edits("switch", lambda: self.switch_profile(monitor_id, name)):
            return
        monitor = self._monitor(monitor_id)
        entry = self.profiles.get(monitor_id)
        current = entry.active_profile if entry else None
        waiting = self.staged_count(monitor_id)
        if current is not None and (waiting or self.differences(monitor_id)):
            pending = f" ({waiting} not applied yet)" if waiting else ""
            answer = messagebox.askyesnocancel(
                "Unsaved changes",
                f"{monitor.name} has changes not saved to “{current.name}”{pending}.\n\n"
                f"Yes: save them to “{current.name}”, then switch.\n"
                "No: discard them and switch.\n"
                f"Cancel: stay on “{current.name}”.",
                parent=self.root,
                icon="warning",
            )
            if answer is None or not self._free_after_dialog(monitor_id):
                if bar is not None:
                    bar.refresh()
                return
            if answer:
                # What waits is part of what is to be saved.
                self.apply_staged(
                    monitor_id=monitor_id,
                    then=lambda: self._switch(monitor_id, name, save_first=True),
                )
                return
            self.discard_staged(monitor_id)
        self._switch(monitor_id, name, save_first=False)

    def _switch(self, monitor_id: str, name: str, *, save_first: bool) -> None:
        monitor = self._monitor(monitor_id)
        apply_now = self.auto_apply
        ident = self._ident(monitor_id)
        locate = self.locator(monitor_id)
        self._set_busy(True, f"Switching {monitor.name} to “{name}”…")

        def run(
            service: MonitorService,
        ) -> tuple[
            RestoreReport | None,
            tuple[MonitorState, ...],
            dict[str, MonitorProfiles | None],
        ]:
            target = locate(service)
            if save_first:
                service.save_snapshot(monitor_ids=[target])
            report = None
            if apply_now:
                report = service.activate_profile(target, name, reassert=Reassert.PIVOT)
            else:
                # Nothing is written until Apply - but what waits is decided
                # against the monitor, not the cache, as a restore would.
                service.select_profile(target, name)
                service.reread(target)
            return report, service.list_monitors(), service.profiles_by_monitor()

        def done(result: object, error: BaseException | None) -> None:
            self._set_busy(False)
            if error is not None:
                self.status(f"Could not switch profile: {error}", error=True)
                self._refresh_after_failure(ident)
                return
            report, self.monitors, self.profiles = result
            if report is None:
                self.render()
                # The list just drawn is the bus's: the id may be new.
                current = self._monitor_id_of(ident)
                if current is not None:
                    self._stage_profile(current)
                self.status(
                    f"{monitor.name} now uses “{name}”: "
                    f"{self.staged_count(current) if current else 0} change(s) "
                    "waiting for Apply."
                )
                return
            self.render()
            message = (
                f"{monitor.name} now uses “{name}”: {len(report.applied)} written, "
                f"{report.unchanged} already correct."
            )
            problems = list(report.failed) + list(report.skipped)
            if problems:
                message += f" {len(problems)} not applied: {problems[0].reason}"
            self.status(message, error=bool(report.failed), ok=not problems)

        self.bridge.worker.submit(run, done, label="Switching profile")

    def new_profile(self, monitor_id: str) -> None:
        """Save one monitor's current values as a new profile, and use it.

        "Current" is what is on screen: changes waiting for Apply on that
        monitor are applied first - once the name is given, so cancelling
        the question writes nothing.
        """
        if self.busy:
            return
        monitor = self._monitor(monitor_id)
        entry = self.profiles.get(monitor_id)
        name = self.ask_name(
            self.root,
            self.palette,
            title="New profile",
            prompt=f"Save the current values of {monitor.name} as a new profile named:",
            initial=_suggest_name(entry),
            validate=lambda text: _name_problem(text, entry, None),
        )
        if name is None or not self._free_after_dialog(monitor_id):
            return
        self.apply_staged(
            monitor_id=monitor_id, then=lambda: self._create_profile(monitor_id, name)
        )

    def _create_profile(self, monitor_id: str, name: str) -> None:
        monitor = self._monitor(monitor_id)
        ident = self._ident(monitor_id)
        locate = self.locator(monitor_id)
        self._set_busy(True, f"Saving “{name}”…")

        def run(service: MonitorService) -> tuple[RestoreReport, dict]:
            target = locate(service)
            service.create_profile(target, name)
            # Save semantics: what a monitor forgets is pushed, so the new
            # profile is also what the screen shows.
            report = service.reassert_saved(reassert=Reassert.PIVOT, monitor_ids=[target])
            return report, service.profiles_by_monitor()

        def done(result: object, error: BaseException | None) -> None:
            self._set_busy(False)
            if error is not None:
                self.status(f"Could not create the profile: {error}", error=True)
                self._refresh_after_failure(ident)
                return
            report, self.profiles = result
            self._refresh_state()
            message = f"{monitor.name} now uses the new profile “{name}”."
            if report.failed:
                message += f" {len(report.failed)} could not be pushed: {report.failed[0].reason}"
            self.status(message, error=bool(report.failed), ok=not report.failed)

        self.bridge.worker.submit(run, done, label="Creating profile")

    def rename_profile(self, monitor_id: str) -> None:
        """Rename the profile a monitor is using.  Nothing is written to it."""
        if self.busy:
            return
        monitor = self._monitor(monitor_id)
        entry = self.profiles.get(monitor_id)
        current = entry.active_profile if entry else None
        if current is None:
            return
        name = self.ask_name(
            self.root,
            self.palette,
            title="Rename profile",
            prompt=f"New name for “{current.name}” of {monitor.name}:",
            initial=current.name,
            validate=lambda text: _name_problem(text, entry, current.name),
        )
        if (
            name is None
            or name == current.name
            or not self._free_after_dialog(monitor_id)
        ):
            return
        locate = self.locator(monitor_id)
        self._profile_change(
            lambda s: s.rename_profile(locate(s), current.name, name),
            monitor_id,
            done_message=f"Renamed “{current.name}” to “{name}”.",
            label="Renaming profile",
        )

    def delete_profile(self, monitor_id: str, name: str) -> None:
        """Delete a profile a monitor is not using, after asking."""
        if self.busy:
            return
        monitor = self._monitor(monitor_id)
        if not messagebox.askyesno(
            "Delete profile",
            f"Delete the profile “{name}” of {monitor.name}?\n\nThis cannot be undone.",
            parent=self.root,
            icon="warning",
        ) or not self._free_after_dialog(monitor_id):
            return
        locate = self.locator(monitor_id)
        self._profile_change(
            lambda s: s.delete_profile(locate(s), name),
            monitor_id,
            done_message=f"Deleted “{name}”.",
            label="Deleting profile",
        )

    def _profile_change(
        self,
        change: Callable[[MonitorService], object],
        monitor_id: str,
        *,
        done_message: str,
        label: str,
    ) -> None:
        """Run a change that touches only the profiles file, then redraw."""
        ident = self._ident(monitor_id)
        self._set_busy(True, f"{label}…")

        def run(service: MonitorService) -> dict[str, MonitorProfiles | None]:
            change(service)
            return service.profiles_by_monitor()

        def done(result: object, error: BaseException | None) -> None:
            self._set_busy(False)
            if error is not None:
                self.status(str(error), error=True)
                self._refresh_after_failure(ident)
                return
            self.profiles = result
            self._refresh_state()
            self.status(done_message, ok=True)

        self.bridge.worker.submit(run, done, label=label)

    def _refresh_after_failure(self, ident: str) -> None:
        """Show the profile bar of the monitor filed as ``ident`` as it is."""
        monitor_id = self._monitor_id_of(ident)
        bar = self.profile_bars.get(monitor_id) if monitor_id else None
        if bar is not None:
            bar.refresh()

    # Keeper -------------------------------------------------------------- #

    def keeper_changed(self, rescanned: bool, reason: str, written: int) -> None:
        """The keeper acted.  Reload what it invalidated."""
        if written:
            self.status(
                f"Re-applied {written} setting{'s' if written != 1 else ''} ({reason}).",
                ok=True,
            )
        if rescanned:
            # Ids may have changed; values of known monitors were kept by the
            # service, so this is cheap unless a new monitor appeared.
            self.stale = True
            if self.root.state() != "withdrawn":
                self.load(probe=False)
        self.refresh_keeper_label()

    def refresh_keeper_label(self) -> None:
        self.keeper_label.configure(text=self.bridge.tray_status())

    # Settings -------------------------------------------------------------- #

    def _settings_edited(self) -> None:
        text = self.interval_var.get().strip()
        current = self.bridge.settings.get()
        try:
            minutes = int(text)
            updated = self.bridge.settings.update(
                interval_minutes=minutes,
                mode=RefreshMode(self.mode_var.get()),
                paused=self.paused_var.get(),
                apply_on_start=self.apply_start_var.get(),
            )
        except (TypeError, ValueError):
            self.interval_var.set(str(current.interval_minutes))
            self.status(
                f"The interval must be a whole number of minutes from {MIN_INTERVAL_MINUTES} "
                f"to {MAX_INTERVAL_MINUTES}.",
                error=True,
            )
            return
        if updated != current:
            self.bridge.settings_changed()
            self.refresh_keeper_label()
            self.status("Settings saved.", ok=True)

    def _autostart_enabled(self) -> bool:
        try:
            return autostart.is_enabled()
        except autostart.AutostartError as exc:
            _log.warning("%s", exc)
            return False

    def _toggle_autostart(self) -> None:
        wanted = self.autostart_var.get()
        try:
            if wanted:
                autostart.enable()
                self.status("monitorctl will start with Windows.", ok=True)
            else:
                autostart.disable()
                self.status("monitorctl will no longer start with Windows.", ok=True)
        except autostart.AutostartError as exc:
            self.autostart_var.set(not wanted)
            self.status(str(exc), error=True)

    def _open_state_dir(self) -> None:
        directory = state_directory()
        directory.mkdir(parents=True, exist_ok=True)
        os.startfile(directory)

    def toggle_theme(self) -> None:
        theme = (
            Theme.LIGHT if self.bridge.settings.get().theme is Theme.DARK else Theme.DARK
        )
        self.bridge.settings.update(theme=theme)
        self.palette = apply_theme(self.root, theme)
        self._apply_title_bar()
        # Classic Tk widgets do not follow ttk styles: recolour them by hand.
        p = self.palette
        for widget in self._descendants(self.root):
            if isinstance(widget, tk.Canvas):
                # The calibration patterns keep their exact field colours.
                if widget.winfo_toplevel() is self.root:
                    widget.configure(background=p.bg)
            elif isinstance(widget, tk.Menu):
                widget.configure(
                    background=p.surface_2,
                    foreground=p.text,
                    activebackground=p.accent,
                    activeforeground=p.accent_text,
                )
            elif isinstance(widget, ttk.Combobox):
                # The drop-down list is created once, on first use, with the
                # colours of the moment; dropped, Tk builds it again with the
                # theme's (theme.py sets them as options).
                popdown = f"{widget}.popdown"
                if self.root.tk.call("winfo", "exists", popdown):
                    self.root.tk.call("destroy", popdown)
        for spinner in (self.spinner, self.placeholder_spinner):
            spinner.recolour()  # the status bar's is not on the page colour
        self._calibrations = [c for c in self._calibrations if c.top.winfo_exists()]
        for calibration in self._calibrations:
            calibration.set_palette(p)
        self._refresh_state()

    def _descendants(self, widget: tk.Misc) -> list[tk.Misc]:
        found = [widget]
        for child in widget.winfo_children():
            found.extend(self._descendants(child))
        return found

    def open_calibration(self) -> None:
        """Open the guide, or bring forward the one already open."""
        self._calibrations = [c for c in self._calibrations if c.top.winfo_exists()]
        if self._calibrations:
            self._calibrations[0].focus()
            return
        self._calibrations.append(CalibrationWindow(self.root, self.palette))

    # Status ---------------------------------------------------------------- #

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        for button in (self.save_button, self.restore_button, self.rescan_button):
            button.state(["disabled"] if busy else ["!disabled"])
        if busy:
            self.status(message, sticky=True)
        self._show_spinners(busy)
        self._refresh_state()
        if not busy and self._deferred is not None:
            # A Save or Restore asked for meanwhile (see _when_free); before a
            # reload also waiting, which queues itself behind it again.
            action, self._deferred = self._deferred, None
            self.root.after_idle(action)
        if not busy and self._load_wanted is not None:
            # A reload asked for while busy (see load); after the result of
            # the operation that just ended has been drawn.
            probe = self._load_wanted
            self._load_wanted = None
            self.root.after_idle(lambda: self.load(probe=probe))

    def _show_spinners(self, busy: bool) -> None:
        """Turn the spinners while busy: beside the status message, and where
        the monitors will appear if none is shown yet."""
        if busy:
            if not self.spinner.turning:
                self.spinner.canvas.pack(
                    side="left", padx=(0, 8), before=self.status_label
                )
                self.spinner.start()
            if not self.placeholder_spinner.turning:
                self.placeholder_spinner.canvas.pack(side="top", pady=(0, 10))
                self.placeholder_spinner.start()
        else:
            for spinner in (self.spinner, self.placeholder_spinner):
                spinner.stop()
                spinner.canvas.pack_forget()

    def _refresh_state(self) -> None:
        """Redraw everything that depends on saved-vs-current and busy: the
        toolbar, its label, each profile bar, and whether the keeper waits."""
        self.theme_button.configure(
            text="☀" if self.bridge.settings.get().theme is Theme.DARK else "☾"
        )
        saved = self.has_saved()
        if not self.busy:
            self.restore_button.state(["!disabled"] if saved else ["disabled"])
        waiting = self.staged_count()
        self.auto_check.state(["disabled"] if self.busy else ["!disabled"])
        self.apply_button.configure(text=f"Apply ({waiting})" if waiting else "Apply")
        # Always there to press: with nothing waiting it pushes what the
        # window shows (push_current).  Highlighted only when changes wait.
        can_apply = bool(waiting or self.monitors) and not self.busy
        self.apply_button.state(["!disabled"] if can_apply else ["disabled"])
        self.apply_button.configure(
            style="Accent.TButton" if can_apply and waiting else "TButton"
        )
        can_discard = waiting and not self.busy
        self.discard_button.state(["!disabled"] if can_discard else ["disabled"])
        drifted = self.drifted()
        # The keeper also waits while the user's own writes are on their way:
        # until their result is drained here, the drift they create is not
        # known yet, and a check run between them would undo it on the
        # monitor (or under a Save chained after an Apply).
        working = self.busy or bool(self.writing)
        self.bridge.set_editing((drifted or working) and self.root.state() != "withdrawn")
        if waiting:
            text, style = "changes not applied", "Drifted.Toolbar.TLabel"
        elif not saved:
            text, style = "nothing saved", "Toolbar.TLabel"
        elif drifted:
            text, style = "unsaved changes", "Drifted.Toolbar.TLabel"
        else:
            text, style = "all saved", "Toolbar.TLabel"
        self.snapshot_label.configure(text=text, style=style)
        for bar in self.profile_bars.values():
            bar.refresh()
        for row in self.rows.values():
            row.refresh_drift()

    def hint(self, text: str) -> None:
        """Tooltip text for keyboard users, shown in the status bar."""
        self.status(text.split("\n\n")[0])

    def status(
        self, message: str, *, error: bool = False, ok: bool = False, sticky: bool = False
    ) -> None:
        style = (
            "StatusError.TLabel"
            if error
            else "StatusOk.TLabel"
            if ok
            else "Status.TLabel"
        )
        self.status_label.configure(text=message, style=style)
        if self._status_after is not None:
            self.root.after_cancel(self._status_after)
            self._status_after = None
        if not sticky and not error:
            self._status_after = self.root.after(
                8000, lambda: self.status_label.configure(text="")
            )

    def _callback_failed(self, exc_type, exc, tb) -> None:
        _log.error("unhandled error in the window", exc_info=(exc_type, exc, tb))
        message = str(exc) if isinstance(exc, DdcError) else f"Unexpected error: {exc}"
        self.status(message, error=True)
