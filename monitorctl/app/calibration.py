"""A short guide for adjusting a monitor by eye, one test pattern per step.

The steps and their patterns come from the usual by-eye methods - the AVS HD 709
clipping patterns, Lagom's LCD tests, ArgyllCMS's adjustment order, EIZO's
preparation advice; ``docs/PESQUISA-CALIBRACAO.md`` has the sources.  Each step
states one goal, as concretely as its pattern allows ("bar 3 blinks, but only
just"), then a line per thing that can be wrong and the control that fixes
it, and which way - no explanations to read while adjusting (D31).  The window is meant to be dragged onto the monitor being adjusted and made
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
#: The solid grey inside the gamma stripes.  Black and white lines average half
#: the light; at gamma 2.2 that is 186 (188 with the sRGB curve).
GAMMA_TARGET = 186
#: Strip at the bottom of a full-screen step: its goal and the keys.
CAPTION_HEIGHT = 48


def _grey(level: int) -> str:
    return f"#{level:02x}{level:02x}{level:02x}"


@dataclass(frozen=True, slots=True)
class Step:
    """One page of the guide.

    Attributes:
        title: Short name, for the step list.
        goal: The one thing to look for, in a sentence - what "right" looks
            like, as concretely as the pattern allows ("bar 3 just blinks").
        fixes: "What you see -> which control to move, which way", one line
            each, most likely first.
        background: The pattern's own field colour.
        draw: Name of the :class:`CalibrationWindow` method drawing it.
    """

    title: str
    goal: str
    fixes: tuple[str, ...]
    background: str
    draw: str


STEPS: tuple[Step, ...] = (
    Step(
        "Before you start",
        "Do these five things once, then go through the steps with ← →.",
        (),
        "#262b34",
        "_draw_preparation",
    ),
    Step(
        "1  Brightness",
        "The white looks like a sheet of paper in this room: bright, not glaring.",
        (
            "Glaring, or your eyes tire → lower Brightness (monitor).",
            "Dull or greyish → raise Brightness (monitor).",
        ),
        "#ffffff",
        "_draw_brightness",
    ),
    Step(
        "2  Highlights",
        "Bar 254 still blinks, but only just. Every bar below it blinks clearly.",
        (
            "The last bars do not blink → lower Contrast (monitor) until they do.",
            "White turns pinkish or tinted → lower Contrast (monitor).",
            "No setting makes 254 blink → settle for 250.",
        ),
        "#ffffff",
        "_draw_highlights",
    ),
    Step(
        "3  Shadows",
        "In a dim room: bar 3 blinks, but only just; 1 and 2 may stay invisible. "
        "Black stays black.",
        (
            "Bar 3 (or up to 5) does not blink → raise Black level (monitor, all "
            "three by the same amount), or else Gamma (software).",
            "Bar 1 is clearly visible and black looks grey → lower the same control.",
            "Then check step 2 again: the two interact.",
        ),
        "#000000",
        "_draw_shadows",
    ),
    Step(
        "4  Grey balance",
        "Every grey looks neutral: no pink, green or blue tint.",
        (
            "The light greys are tinted → lower that colour's Gain (monitor).",
            "The dark greys are tinted → lower that colour's Black level (monitor).",
            "Tint → colour to lower: pink Red · green Green · blue Blue · yellow Red "
            "and Green · purple Red and Blue · cyan Green and Blue.",
        ),
        "#7f7f7f",
        "_draw_grey_balance",
    ),
    Step(
        "5  Gamma",
        "Squint or step back until the stripes blur into a flat grey: the square in "
        "the middle disappears into them.",
        (
            "The square is darker than the stripes → raise Gamma (software).",
            "The square is lighter → lower Gamma (software).",
        ),
        "#7f7f7f",
        "_draw_gamma",
    ),
    Step(
        "6  Gradients",
        "Each ramp is smooth: no hard steps and no coloured bands.",
        (
            "Bands that were not there before the steps above → move the PC "
            "(software) controls back toward 50.",
            "A little banding on an inexpensive panel is normal.",
        ),
        "#262b34",
        "_draw_gradients",
    ),
    Step(
        "7  Colours",
        "Skin, sky and grass in a photo you know well look natural.",
        (
            "Clearly dull or clearly loud → Saturation (PC or monitor). Otherwise "
            "leave it.",
        ),
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
        self.goal_label = ttk.Label(self.chrome, style="Goal.TLabel", justify="left")
        self.goal_label.pack(anchor="w", fill="x", pady=(10, 4))
        self.fixes_label = ttk.Label(self.chrome, style="Dim.TLabel", justify="left")
        self.fixes_label.pack(anchor="w", fill="x")
        self.chrome.bind(
            "<Configure>",
            lambda e: [
                label.configure(wraplength=max(300, e.width - 40))
                for label in (self.goal_label, self.fixes_label)
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
        self.goal_label.configure(text=f"Goal: {self.step.goal}")
        self.fixes_label.configure(text="\n".join(f"•  {fix}" for fix in self.step.fixes))
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
        """In full screen the step bar is gone: the goal stays, small, at the
        bottom, on a strip of its own so it never sits on a pattern."""
        self.canvas.create_rectangle(
            0, height - CAPTION_HEIGHT, width, height, fill="#000000", width=0
        )
        self.canvas.create_text(
            width / 2,
            height - CAPTION_HEIGHT / 2,
            text=(
                f"{self.step.title}:  {self.step.goal}\n"
                "← →  steps    F11 / Esc  leave full screen"
            ),
            justify="center",
            width=width - 40,
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
            "1.  Monitor on for about 30 minutes.",
            "2.  Your usual room light.",
            "3.  On the monitor: dynamic contrast, eco and enhancements off; "
            "Standard, sRGB or User preset.",
            '4.  In monitorctl: every "PC" control at 50 (Hue angle 0).',
            "5.  Drag this window onto the monitor you are adjusting, then F11.",
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
        """One-pixel black and white stripes around one solid grey, large.

        One square to judge rather than a row of candidates: "does it vanish,
        or is it darker or lighter" is a question anyone can answer, and
        Gamma (software) moves the square while the stripes - pure black and
        white, which a gamma curve never moves - stay put.
        """
        background = self.step.background
        side = min(width * 0.6, height * 0.72)
        left, top = (width - side) / 2, (height - side) / 2 - 10
        self.canvas.create_rectangle(
            left, top, left + side, top + side, fill="#000000", width=0
        )
        for y in range(int(top), int(top + side), 2):
            self.canvas.create_line(left, y, left + side, y, fill="#ffffff")
        inset = side / 3
        self.canvas.create_rectangle(
            left + inset,
            top + inset,
            left + side - inset,
            top + side - inset,
            fill=_grey(GAMMA_TARGET),
            width=0,
        )
        # Lines average half the light: the grey that matches them shows
        # gamma = log(0.5) / log(level / 255).
        gamma = math.log(0.5) / math.log(GAMMA_TARGET / 255)
        self._text(
            width / 2,
            top + side + 18,
            f"Vanishes = gamma {gamma:.1f}, the standard",
            background,
        )

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
