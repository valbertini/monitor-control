"""A short guide for adjusting a monitor by eye, one test pattern per step.

The steps and their patterns come from the usual by-eye methods - the AVS HD 709
clipping patterns, Lagom's LCD tests, ArgyllCMS's adjustment order, EIZO's
preparation advice; ``docs/PESQUISA-CALIBRACAO.md`` has the sources.  Each step
says what to look at, what "right" looks like and which control to move, and
the window is meant to be dragged onto the monitor being adjusted and made
full screen (F11): a pattern only says something about the screen it is on.

Patterns are exact sRGB values drawn on a canvas.  The process is DPI aware, so
a canvas pixel is a screen pixel - which the gamma lines need - and nothing is
scaled or blended by the toolkit.

The clipping bars blink once a second (well under WCAG 2.3.1's three flashes):
a faint bar next to a field of its background is easy to miss when still and
hard to miss when it comes and goes.
"""

from __future__ import annotations

import math
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

from .theme import Palette

__all__ = ["STEPS", "CalibrationWindow", "Step"]

#: Half a blink cycle: one flash per second.
BLINK_MS = 500

#: Levels the clipping bars use, after the AVS HD 709 disc mapped to PC levels
#: (its video black 16 and white 235 are 0 and 255 here).
SHADOW_BARS = tuple(range(1, 26))
HIGHLIGHT_BARS = (235, *range(240, 255))
#: Lagom's static squares, for the same ends without blinking.
SHADOW_SQUARES = (*range(1, 16), 20, 25, 30, 40)
HIGHLIGHT_SQUARES = tuple(range(244, 255))
#: Solid greys beside the gamma lines.  Black and white lines average half the
#: light; at gamma 2.2 that is 186 (188 with the sRGB curve).
GAMMA_SOLIDS = (176, 181, 186, 191, 196)
GAMMA_TARGET = 186


def _grey(level: int) -> str:
    return f"#{level:02x}{level:02x}{level:02x}"


@dataclass(frozen=True, slots=True)
class Step:
    """One page of the guide.

    Attributes:
        title: Short name, for the step list.
        look: What to look at, and what right looks like.
        adjust: Which control to move, in order of preference.
        background: The pattern's own field colour.
        draw: Name of the :class:`CalibrationWindow` method drawing it.
    """

    title: str
    look: str
    adjust: str
    background: str
    draw: str


STEPS: tuple[Step, ...] = (
    Step(
        "Before you start",
        "Check these five things before the tests, then move through the steps with "
        "← →. F11 shows a step full screen; Esc leaves it.",
        "",
        "#262b34",
        "_draw_preparation",
    ),
    Step(
        "1  Brightness",
        "White should look like a sheet of paper in this room - bright, but not "
        "glaring. The grey square in the middle is a half-tone for scale.",
        "Monitor: Brightness (the backlight). It raises white and black together.",
        "#ffffff",
        "_draw_brightness",
    ),
    Step(
        "2  Highlights",
        "Every bar should blink, and white should stay neutral. If bars near 254 "
        "stop blinking, or white turns pinkish or tinted, the top end is clipping. "
        "At least the bars up to 250 should blink; the squares below should all be "
        "distinguishable.",
        "Monitor: Contrast. Raise it until bars stop blinking or white tints, then "
        "lower it until they blink again.",
        "#ffffff",
        "_draw_highlights",
    ),
    Step(
        "3  Shadows",
        "Best in a dim room. Bars 1 and 2 may stay invisible on many panels; bar 3 "
        "(at worst 5) and everything above it should blink. Black itself should stay "
        "black, not grey.",
        "Monitor: Black level / offset (all three together) if it has them, or its "
        "gamma preset. Only then the PC: Gamma or Brightness (software). Afterwards, "
        "check Highlights again - the two interact.",
        "#000000",
        "_draw_shadows",
    ),
    Step(
        "4  Grey balance",
        "Every grey should look neutral - no pink, green or blue cast. Judge the "
        "large light patch for the bright end and the dark one for the shadows. Your "
        "eye adapts to the screen's white, so compare with something truly white or "
        "grey in the room if you can.",
        "First the colour temperature preset. Then Gain for the light greys: lower "
        "the channel that is too strong rather than raising the others. Then Black "
        "level / offset for the dark greys. Repeat once, they interact.",
        "#7f7f7f",
        "_draw_grey_balance",
    ),
    Step(
        "5  Gamma",
        "Step back or squint until the line pattern looks like a flat grey. The solid "
        "square that disappears into it shows the monitor's gamma - each is labelled "
        "with the gamma it stands for; 186 (2.2) is the standard. Needs full screen "
        "at the native resolution.",
        "Monitor: its gamma setting, if any. Otherwise the PC: Gamma (software).",
        "#7f7f7f",
        "_draw_gamma",
    ),
    Step(
        "6  Gradients",
        "Each ramp should be smooth, without bands, steps or colour fringes. Some "
        "banding on an inexpensive panel is normal.",
        "If bands appeared after the steps above, back the PC corrections off - every "
        "software curve costs some smoothness.",
        "#262b34",
        "_draw_gradients",
    ),
    Step(
        "7  Colours",
        "Open, beside this window, a photo or an app whose colours you know well and "
        "that has a good variety of them - your own photos, a familiar website, a "
        "game. Faces, skies and greenery are the quickest to look wrong. The bars "
        "below are the six colours at 75%.",
        "Only for an obvious error: PC Saturation (vibrance), or the monitor's "
        "six-axis Saturation and Hue if it has them.",
        "#262b34",
        "_draw_colours",
    ),
)


class CalibrationWindow:
    """The guide: a step bar, the step's instructions and its pattern."""

    def __init__(self, master: tk.Misc, palette: Palette) -> None:
        #: Owns the blink timer: a timer on the Toplevel would die with it and
        #: then fire into a deleted command.
        self._clock = master
        self.index = 0
        self.blinking = True
        self._phase = True
        self._after: str | None = None
        self._redraw: str | None = None
        #: Canvas items that blink, with the colour they show when "on".
        self._blinkers: list[tuple[int, str]] = []

        self.top = tk.Toplevel(master)
        self.top.title("monitorctl — calibration")
        self.top.geometry("1100x760")
        self.top.minsize(720, 520)
        self.top.configure(background=palette.bg)

        self.chrome = ttk.Frame(self.top, padding=(16, 12, 16, 8))
        self.chrome.pack(side="top", fill="x")
        nav = ttk.Frame(self.chrome)
        nav.pack(fill="x")
        ttk.Button(nav, text="‹ Previous", command=lambda: self.go(-1)).pack(side="left")
        self.step_choice = ttk.Combobox(
            nav,
            values=[step.title for step in STEPS],
            state="readonly",
            width=24,
        )
        self.step_choice.pack(side="left", padx=8)
        self.step_choice.bind(
            "<<ComboboxSelected>>",
            lambda _e: self.show(self.step_choice.current()),
        )
        ttk.Button(nav, text="Next ›", command=lambda: self.go(1)).pack(side="left")
        ttk.Button(nav, text="Full screen (F11)", command=self.toggle_fullscreen).pack(
            side="right"
        )
        self.blink_var = tk.BooleanVar(self.top, value=True)
        ttk.Checkbutton(
            nav, text="Blink", variable=self.blink_var, command=self._blink_toggled
        ).pack(side="right", padx=12)
        self.look_label = ttk.Label(self.chrome, justify="left")
        self.look_label.pack(anchor="w", fill="x", pady=(10, 2))
        self.adjust_label = ttk.Label(self.chrome, style="Dim.TLabel", justify="left")
        self.adjust_label.pack(anchor="w", fill="x")
        self.chrome.bind(
            "<Configure>",
            lambda e: [
                label.configure(wraplength=max(300, e.width - 40))
                for label in (self.look_label, self.adjust_label)
            ],
        )

        self.canvas = tk.Canvas(self.top, highlightthickness=0, background="#000000")
        self.canvas.pack(fill="both", expand=True)
        # Debounced: dragging the window edge sends a stream of these, and the
        # gradient and gamma patterns are thousands of canvas items.
        self.canvas.bind("<Configure>", lambda _e: self._schedule_draw())
        for sequence, action in (
            ("<Left>", lambda: self.go(-1)),
            ("<Right>", lambda: self.go(1)),
            ("<F11>", self.toggle_fullscreen),
            ("<Escape>", self._escape),
        ):
            self.top.bind(sequence, lambda _e, a=action: a())
        self.top.bind("<Destroy>", self._destroyed, add="+")
        self.show(0)
        self._tick()

    # Navigation -------------------------------------------------------------- #

    @property
    def step(self) -> Step:
        return STEPS[self.index]

    def show(self, index: int) -> None:
        self.index = max(0, min(len(STEPS) - 1, index))
        self.step_choice.current(self.index)
        self.look_label.configure(text=self.step.look)
        adjust = self.step.adjust
        self.adjust_label.configure(text=f"Adjust - {adjust}" if adjust else "")
        self.draw()

    def go(self, delta: int) -> None:
        self.show(self.index + delta)

    def toggle_fullscreen(self) -> None:
        """Full screen on the monitor the window is on, without the step bar."""
        full = not bool(self.top.attributes("-fullscreen"))
        self.top.attributes("-fullscreen", full)
        if full:
            self.chrome.pack_forget()
        else:
            self.chrome.pack(side="top", fill="x", before=self.canvas)
        # The canvas changes size, so <Configure> redraws it.

    def _escape(self) -> None:
        # Only leaves full screen: closing the guide on a second press would
        # lose the step the user is on.  The window's own close button closes.
        if self.top.attributes("-fullscreen"):
            self.toggle_fullscreen()

    def focus(self) -> None:
        """Bring the guide forward, as asked for again from the main window."""
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()

    def set_palette(self, palette: Palette) -> None:
        """Follow a theme switch.  The patterns keep their exact colours."""
        self.top.configure(background=palette.bg)

    def _schedule_draw(self) -> None:
        if self._redraw is not None:
            self._clock.after_cancel(self._redraw)
        self._redraw = self._clock.after(60, self._redraw_now)

    def _redraw_now(self) -> None:
        self._redraw = None
        if self.top.winfo_exists():
            self.draw()

    # Blinking ---------------------------------------------------------------- #

    def _blink_toggled(self) -> None:
        self.blinking = self.blink_var.get()
        self._phase = True
        self._paint_blinkers()

    def _tick(self) -> None:
        if not self.top.winfo_exists():
            return
        if self.blinking:
            self._phase = not self._phase
            self._paint_blinkers()
        self._after = self._clock.after(BLINK_MS, self._tick)

    def _paint_blinkers(self) -> None:
        off = self.step.background
        for item, colour in self._blinkers:
            self.canvas.itemconfigure(item, fill=colour if self._phase else off)

    def _destroyed(self, event: tk.Event) -> None:
        if event.widget is not self.top:
            return
        for timer in (self._after, self._redraw):
            if timer is not None:
                self._clock.after_cancel(timer)
        self._after = self._redraw = None

    # Drawing ----------------------------------------------------------------- #

    def draw(self) -> None:
        c = self.canvas
        c.delete("all")
        self._blinkers.clear()
        c.configure(background=self.step.background)
        width, height = max(c.winfo_width(), 400), max(c.winfo_height(), 300)
        getattr(self, self.step.draw)(width, height)
        if self.top.attributes("-fullscreen"):
            self._caption(width, height)
        self._paint_blinkers()

    def _ink(self, background: str) -> str:
        """A label colour readable on ``background`` without being a test patch."""
        level = int(background[1:3], 16)
        return (
            "#808080" if level in (0, 255) else ("#202020" if level > 128 else "#c8c8c8")
        )

    def _text(self, x: float, y: float, text: str, background: str, **kw) -> None:
        self.canvas.create_text(
            x, y, text=text, fill=self._ink(background), font=("Segoe UI", 10), **kw
        )

    def _caption(self, width: int, height: int) -> None:
        """In full screen the step bar is gone: name the step, small, at the
        bottom, on a strip of its own so it never sits on a pattern."""
        self.canvas.create_rectangle(
            0, height - 34, width, height, fill="#000000", width=0
        )
        self.canvas.create_text(
            width / 2,
            height - 17,
            text=f"{self.step.title}    ← →  steps    F11 / Esc  leave full screen",
            fill="#808080",
            font=("Segoe UI", 10),
        )

    def _bars(
        self,
        levels: tuple[int, ...],
        background: str,
        top: float,
        bottom: float,
        width: int,
    ) -> None:
        """Numbered bars across the width, each blinking against the field."""
        margin = width * 0.06
        cell = (width - 2 * margin) / len(levels)
        for index, level in enumerate(levels):
            left = margin + index * cell
            item = self.canvas.create_rectangle(
                left + cell * 0.12,
                top,
                left + cell * 0.88,
                bottom,
                fill=_grey(level),
                width=0,
            )
            self._blinkers.append((item, _grey(level)))
            self._text(left + cell / 2, bottom + 16, str(level), background)

    def _squares(
        self, levels: tuple[int, ...], background: str, top: float, width: int
    ) -> None:
        """Lagom-style static squares, numbered, for the same end of the scale."""
        margin = width * 0.06
        cell = (width - 2 * margin) / len(levels)
        side = min(cell * 0.8, 70)
        for index, level in enumerate(levels):
            centre = margin + (index + 0.5) * cell
            self.canvas.create_rectangle(
                centre - side / 2,
                top,
                centre + side / 2,
                top + side,
                fill=_grey(level),
                width=0,
            )
            self._text(centre, top + side + 14, str(level), background)

    def _draw_preparation(self, width: int, height: int) -> None:
        lines = (
            "1.  Warm up: about 30 minutes on.",
            "2.  Light: your usual light; dim it for the shadow step.",
            "3.  Monitor: dynamic contrast, eco mode and enhancements off; "
            "Standard / sRGB / User preset.",
            '4.  This PC: the "PC software" cards back to neutral (50; Hue angle 0).',
            "5.  This window: on the monitor you are adjusting, full screen (F11).",
        )
        for index, line in enumerate(lines):
            self.canvas.create_text(
                width * 0.1,
                height * 0.2 + index * 44,
                text=line,
                anchor="w",
                width=width * 0.8,
                fill="#e6e9ef",
                font=("Segoe UI", 13),
            )

    def _draw_brightness(self, width: int, height: int) -> None:
        side = min(width, height) * 0.28
        self.canvas.create_rectangle(
            (width - side) / 2,
            (height - side) / 2,
            (width + side) / 2,
            (height + side) / 2,
            fill=_grey(188),
            width=0,
        )

    def _draw_highlights(self, width: int, height: int) -> None:
        background = self.step.background
        self._bars(HIGHLIGHT_BARS, background, height * 0.08, height * 0.48, width)
        self._squares(HIGHLIGHT_SQUARES, background, height * 0.62, width)

    def _draw_shadows(self, width: int, height: int) -> None:
        background = self.step.background
        self._bars(SHADOW_BARS, background, height * 0.08, height * 0.48, width)
        self._squares(SHADOW_SQUARES, background, height * 0.62, width)

    def _draw_grey_balance(self, width: int, height: int) -> None:
        background = self.step.background
        ramp = tuple(round(i * 25.5) for i in range(11))
        cell = width / len(ramp)
        for index, level in enumerate(ramp):
            self.canvas.create_rectangle(
                index * cell,
                0,
                (index + 1) * cell,
                height * 0.3,
                fill=_grey(level),
                width=0,
            )
        for left, level, name in ((0.08, 204, "80% - Gain"), (0.54, 64, "25% - Offset")):
            self.canvas.create_rectangle(
                width * left,
                height * 0.38,
                width * (left + 0.38),
                height * 0.88,
                fill=_grey(level),
                width=0,
            )
            self._text(width * (left + 0.19), height * 0.93, name, background)

    def _draw_gamma(self, width: int, height: int) -> None:
        background = self.step.background
        top, bottom = height * 0.15, height * 0.75
        cell = width / (len(GAMMA_SOLIDS) * 2 + 1)
        for index, level in enumerate(GAMMA_SOLIDS):
            left = cell * (1 + index * 2)
            # One-pixel black and white lines, then the solid grey inside them.
            self.canvas.create_rectangle(
                left, top, left + cell, bottom, fill="#000000", width=0
            )
            for y in range(int(top), int(bottom), 2):
                self.canvas.create_line(left, y, left + cell, y, fill="#ffffff")
            inset = cell * 0.3
            self.canvas.create_rectangle(
                left + inset,
                top + (bottom - top) * 0.3,
                left + cell - inset,
                bottom - (bottom - top) * 0.3,
                fill=_grey(level),
                width=0,
            )
            # Lines average half the light: the grey that matches them shows
            # gamma = log(0.5) / log(level / 255).
            gamma = math.log(0.5) / math.log(level / 255)
            label = f"{level}  ·  {gamma:.1f}"
            if level == GAMMA_TARGET:
                label += "  (standard)"
            self._text(left + cell / 2, bottom + 18, label, background)

    def _draw_gradients(self, width: int, height: int) -> None:
        # 256 levels exist; more rectangles would only repeat them.
        steps = max(64, min(256, width))
        band = height / 4
        for row, channel in enumerate(((1, 1, 1), (1, 0, 0), (0, 1, 0), (0, 0, 1))):
            for s in range(steps):
                level = round(s * 255 / (steps - 1))
                colour = "#{:02x}{:02x}{:02x}".format(*(level * k for k in channel))
                left = s * width / steps
                self.canvas.create_rectangle(
                    left,
                    row * band,
                    left + width / steps + 1,
                    (row + 1) * band,
                    fill=colour,
                    width=0,
                )

    def _draw_colours(self, width: int, height: int) -> None:
        background = self.step.background
        bars: tuple[tuple[str, str], ...] = (
            ("White", "#bfbfbf"),
            ("Yellow", "#bfbf00"),
            ("Cyan", "#00bfbf"),
            ("Green", "#00bf00"),
            ("Magenta", "#bf00bf"),
            ("Red", "#bf0000"),
            ("Blue", "#0000bf"),
        )
        cell = width / len(bars)
        for index, (name, colour) in enumerate(bars):
            self.canvas.create_rectangle(
                index * cell,
                height * 0.1,
                (index + 1) * cell,
                height * 0.75,
                fill=colour,
                width=0,
            )
            self._text(index * cell + cell / 2, height * 0.8, name, background)
