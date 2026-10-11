"""Colours and ttk styles for the window, in dark and light.

ttk's ``clam`` theme is the base because it is the only built-in theme
whose every element takes colours from the style database; ``vista`` draws
native controls that ignore them, which rules out a dark mode.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk

from .settings import Theme

__all__ = ["CHANNELS", "PALETTES", "THIN_SCALE", "Palette", "apply_theme", "scale_style"]


@dataclass(frozen=True, slots=True)
class Palette:
    bg: str
    surface: str
    surface_2: str
    surface_3: str
    border: str
    text: str
    text_dim: str
    text_faint: str
    accent: str
    accent_text: str
    warn: str
    warn_bg: str
    error: str
    ok: str


PALETTES: dict[Theme, Palette] = {
    Theme.DARK: Palette(
        bg="#0f1115",
        surface="#171a20",
        surface_2="#1f232b",
        surface_3="#262b34",
        border="#2a2f39",
        text="#e6e9ef",
        text_dim="#99a2b2",
        text_faint="#6b7483",
        accent="#6aa0f5",
        accent_text="#0f1115",
        warn="#e8c37a",
        warn_bg="#362c17",
        error="#f08080",
        ok="#7fcf9a",
    ),
    Theme.LIGHT: Palette(
        bg="#f6f7f9",
        surface="#ffffff",
        surface_2="#f0f2f5",
        surface_3="#e6e9ee",
        border="#dfe3e8",
        text="#16191d",
        text_dim="#5c6570",
        text_faint="#8b939d",
        accent="#2f6fdb",
        accent_text="#ffffff",
        warn="#8a6100",
        warn_bg="#fff6e0",
        error="#c0392b",
        ok="#1e7d45",
    ),
}


#: Styles whose ``clam`` layout has a dotted focus ring; derived styles
#: (``Accent.TButton``, ...) take their parent's layout.
FOCUS_RING_STYLES = (
    "TButton",
    "TCheckbutton",
    "TRadiobutton",
    "TMenubutton",
    "TNotebook.Tab",
)

Layout = list[tuple[str, dict]]


def without_focus_ring(layout: Layout) -> Layout:
    """``layout`` with every ``*.focus`` element replaced by what it holds.

    The ring is an element wrapping the label, so nothing moves when what it
    wraps takes its place: a lone child, which only filled the ring, takes
    the ring's side and sticky; several children keep their own, which
    placed them inside it.  Idempotent: a theme switch re-applies it to a
    layout that no longer has a ring.
    """
    result: Layout = []
    for name, options in layout:
        children = without_focus_ring(options.get("children", []))
        if name.endswith(".focus"):
            place = {k: v for k, v in options.items() if k in ("side", "sticky")}
            for child, child_options in children:
                merged = (
                    {**child_options, **place}
                    if len(children) == 1
                    else {**place, **child_options}
                )
                result.append((child, merged))
            continue
        options = dict(options)
        if children:
            options["children"] = children
        result.append((name, options))
    return result


def apply_theme(root: tk.Tk, theme: Theme) -> Palette:
    """Configure every style the window uses; returns the palette applied."""
    p = PALETTES[theme]
    style = ttk.Style(root)
    style.theme_use("clam")

    base = tkfont.nametofont("TkDefaultFont")
    base.configure(family="Segoe UI", size=10)
    for name in ("TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkfont.nametofont(name).configure(family="Segoe UI", size=10)

    root.configure(background=p.bg)
    style.configure(
        ".",
        background=p.bg,
        foreground=p.text,
        fieldbackground=p.surface_2,
        bordercolor=p.border,
        lightcolor=p.border,
        darkcolor=p.border,
        troughcolor=p.surface_3,
        focuscolor=p.accent,
        selectbackground=p.accent,
        selectforeground=p.accent_text,
        insertcolor=p.text,
        arrowcolor=p.text_dim,
    )
    style.map(".", foreground=[("disabled", p.text_faint)])

    style.configure("TFrame", background=p.bg)
    style.configure("Card.TFrame", background=p.surface)
    style.configure("Toolbar.TFrame", background=p.surface)
    style.configure("Status.TFrame", background=p.surface)

    style.configure("TLabel", background=p.bg, foreground=p.text)
    style.configure("Card.TLabel", background=p.surface)
    style.configure("Toolbar.TLabel", background=p.surface, foreground=p.text_dim)
    style.configure("Status.TLabel", background=p.surface, foreground=p.text_dim)
    style.configure("StatusError.TLabel", background=p.surface, foreground=p.error)
    style.configure("StatusOk.TLabel", background=p.surface, foreground=p.ok)
    style.configure("Drifted.Toolbar.TLabel", background=p.surface, foreground=p.warn)
    style.configure("Drifted.TLabel", foreground=p.warn)
    style.configure("CardError.TLabel", background=p.surface, foreground=p.error)
    style.configure("Dim.TLabel", foreground=p.text_dim)
    style.configure("Goal.TLabel", font=("Segoe UI Semibold", 11))
    style.configure("Faint.TLabel", foreground=p.text_faint)
    style.configure("Feature.TLabel", background=p.surface)
    style.configure("ReadOnly.TLabel", background=p.surface, foreground=p.text_faint)
    # Changed on screen, waiting for Apply.  The Apply button's count says so
    # in words too.
    style.configure("Staged.Feature.TLabel", background=p.surface, foreground=p.accent)
    # Where a card's changes happen: in the monitor, or on this PC.
    style.configure(
        "TagMonitor.TLabel",
        background=p.surface_3,
        foreground=p.text_dim,
        font=("Segoe UI Semibold", 8),
        padding=(6, 0),
    )
    style.configure(
        "TagPc.TLabel",
        background=p.warn_bg,
        foreground=p.warn,
        font=("Segoe UI Semibold", 8),
        padding=(6, 0),
    )
    style.configure(
        "Note.TLabel", background=p.warn_bg, foreground=p.warn, padding=(10, 6)
    )

    style.configure(
        "TLabelframe",
        background=p.surface,
        bordercolor=p.border,
        lightcolor=p.surface,
        darkcolor=p.surface,
        relief="solid",
        borderwidth=1,
        padding=(10, 6, 10, 10),
    )
    style.configure(
        "TLabelframe.Label",
        background=p.surface,
        foreground=p.text,
        font=("Segoe UI Semibold", 10),
    )
    # A section card: icon and title in the frame's label, a one-line hint,
    # then the controls.
    style.configure(
        "CardTitle.TLabel",
        background=p.surface,
        foreground=p.text,
        font=("Segoe UI Semibold", 10),
    )
    style.configure(
        "CardIcon.TLabel",
        background=p.surface,
        foreground=p.accent,
        font=("Segoe MDL2 Assets", 12),
    )
    style.configure(
        "Hint.TLabel", background=p.surface, foreground=p.text_dim, font=("Segoe UI", 9)
    )
    for channel, colour in CHANNELS[theme].items():
        style.configure(
            f"{channel.capitalize()}.Dot.TLabel",
            background=p.surface,
            foreground=colour,
            font=("Segoe UI", 9),
        )
    style.configure(
        "Revert.TButton",
        background=p.surface,
        foreground=p.warn,
        bordercolor=p.surface,
        lightcolor=p.surface,
        darkcolor=p.surface,
        padding=0,
        font=("Segoe UI Semibold", 11),
    )
    style.map(
        "Revert.TButton",
        background=[("disabled", p.surface), ("active", p.surface_2)],
        bordercolor=[("focus", p.accent)],
    )
    style.configure(
        "Link.TButton",
        background=p.surface,
        foreground=p.accent,
        bordercolor=p.surface,
        lightcolor=p.surface,
        darkcolor=p.surface,
        padding=(4, 0),
        font=("Segoe UI", 9),
    )
    style.map(
        "Link.TButton",
        background=[("active", p.surface_2), ("pressed", p.surface_2)],
        bordercolor=[("focus", p.accent)],
    )

    style.configure(
        "TButton",
        background=p.surface_2,
        foreground=p.text,
        bordercolor=p.border,
        lightcolor=p.surface_2,
        darkcolor=p.surface_2,
        padding=(12, 5),
        focuscolor=p.accent,
    )
    style.map(
        "TButton",
        background=[
            ("disabled", p.surface),
            ("pressed", p.surface_3),
            ("active", p.surface_3),
        ],
        foreground=[("disabled", p.text_faint)],
        bordercolor=[("focus", p.accent)],
    )
    style.configure(
        "TMenubutton",
        background=p.surface_2,
        foreground=p.text,
        bordercolor=p.border,
        lightcolor=p.surface_2,
        darkcolor=p.surface_2,
        arrowcolor=p.text_dim,
        padding=(10, 5),
    )
    style.map(
        "TMenubutton",
        background=[
            ("disabled", p.surface),
            ("pressed", p.surface_3),
            ("active", p.surface_3),
        ],
        foreground=[("disabled", p.text_faint)],
        arrowcolor=[("disabled", p.text_faint)],
        bordercolor=[("focus", p.accent)],
    )
    style.configure(
        "Accent.TButton",
        background=p.accent,
        foreground=p.accent_text,
        lightcolor=p.accent,
        darkcolor=p.accent,
        bordercolor=p.accent,
    )
    style.map(
        "Accent.TButton",
        background=[("disabled", p.surface), ("pressed", p.accent), ("active", p.accent)],
        foreground=[("disabled", p.text_faint)],
    )

    style.configure("TNotebook", background=p.bg, borderwidth=0, tabmargins=(8, 6, 8, 0))
    # clam draws a dotted ring around the label of whatever has focus - and a
    # click gives focus to a tab, a button or a check box, so the ring showed up
    # after every click and looked like a glitch.  Focus shows instead in the
    # accent colour: a button's border, a tab's or a check box's text.
    for name in FOCUS_RING_STYLES:
        style.layout(name, without_focus_ring(style.layout(name)))
    style.configure(
        "TNotebook.Tab",
        background=p.bg,
        foreground=p.text_dim,
        padding=(14, 6),
        borderwidth=0,
        lightcolor=p.bg,
        bordercolor=p.bg,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", p.surface)],
        # The focus ring is gone (above); a tab focused from the keyboard
        # shows in the accent colour instead.
        foreground=[
            ("focus", "selected", p.accent),
            ("selected", p.text),
            ("active", p.text),
        ],
        lightcolor=[("selected", p.accent)],
    )

    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(
            name, background=p.surface, foreground=p.text, indicatorcolor=p.surface_2
        )
        style.map(
            name,
            indicatorcolor=[("selected", p.accent), ("pressed", p.surface_3)],
            background=[("active", p.surface)],
            foreground=[("disabled", p.text_faint), ("focus", p.accent)],
        )

    _thin_scale(root, style, p, theme)

    for name in ("TSpinbox", "TCombobox", "TEntry"):
        style.configure(
            name,
            fieldbackground=p.surface_2,
            background=p.surface_2,
            foreground=p.text,
            bordercolor=p.border,
            lightcolor=p.surface_2,
            darkcolor=p.surface_2,
            arrowsize=12,
            padding=(4, 2),
        )
        style.map(
            name,
            fieldbackground=[("disabled", p.surface), ("readonly", p.surface_2)],
            foreground=[("disabled", p.text_faint)],
            bordercolor=[("focus", p.accent), ("invalid", p.error)],
            selectbackground=[("readonly", p.surface_2)],
            selectforeground=[("readonly", p.text)],
        )
    style.configure("Invalid.TSpinbox", bordercolor=p.error, lightcolor=p.error)

    style.configure(
        "Vertical.TScrollbar",
        background=p.surface_2,
        troughcolor=p.bg,
        arrowcolor=p.text_dim,
    )

    # Classic-Tk parts the style database does not reach.
    root.option_add("*TCombobox*Listbox.background", p.surface_2)
    root.option_add("*TCombobox*Listbox.foreground", p.text)
    root.option_add("*TCombobox*Listbox.selectBackground", p.accent)
    root.option_add("*TCombobox*Listbox.selectForeground", p.accent_text)
    root.option_add("*TCombobox*Listbox.font", base)
    # Menus are classic Tk too.  Options apply to menus created after this,
    # so a menu built before a theme switch keeps its colours until redrawn.
    root.option_add("*Menu.background", p.surface_2)
    root.option_add("*Menu.foreground", p.text)
    root.option_add("*Menu.activeBackground", p.accent)
    root.option_add("*Menu.activeForeground", p.accent_text)
    root.option_add("*Menu.borderWidth", 0)
    return p


#: Style of the controls' sliders: a thin line with a round knob.  A control
#: that acts on one colour channel uses the variant of :func:`scale_style`.
THIN_SCALE = "Thin.Horizontal.TScale"

#: Colour channels a control can be tied to, in both themes.  Saturated enough
#: to read as "red", "green" ... at a glance, muted enough not to shout; the
#: light theme's are darker so they hold contrast on white.  Never the only
#: cue: every control keeps its written name.
CHANNELS: dict[Theme, dict[str, str]] = {
    Theme.DARK: {
        "red": "#ef5f57",
        "green": "#4cc46a",
        "blue": "#5b95f5",
        "yellow": "#e3c24a",
        "cyan": "#3fc8d4",
        "magenta": "#cf6bdc",
    },
    Theme.LIGHT: {
        "red": "#d0352c",
        "green": "#1f8f3e",
        "blue": "#2a66d6",
        "yellow": "#a88300",
        "cyan": "#0c8f9a",
        "magenta": "#a63bb5",
    },
}

#: At 96 DPI, in pixels.  The line is thin so that a column of sliders reads
#: as a set of values rather than a set of bars; the knob is large enough to
#: grab.
_TROUGH_HEIGHT = 4
_KNOB_DIAMETER = 14

#: Attribute of the root holding the slider images.  ttk elements are created
#: once per interpreter and keep pointing at the same images, so a theme switch
#: repaints them in place; the reference also keeps them from being garbage
#: collected, which would blank every slider.
_IMAGES_ATTRIBUTE = "_monitorctl_scale_images"


def scale_style(channel: str | None) -> str:
    """The slider style for a control tied to ``channel``, or the plain one."""
    return f"{channel.capitalize()}.{THIN_SCALE}" if channel else THIN_SCALE


def _mix(colour: str, other: str, amount: float) -> str:
    """``colour`` moved ``amount`` (0..1) of the way towards ``other``."""
    a = [int(colour[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(
        f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b, strict=True)
    )


def _thin_scale(root: tk.Tk, style: ttk.Style, p: Palette, theme: Theme) -> None:
    """Build or repaint the image elements the slider styles are made of.

    The ``clam`` scale draws a bevelled bar as thick as its thumb, and none of
    its options make it a line; image elements are the way ttk offers to draw
    something else.  Sizes follow the screen's DPI, since images do not scale
    with Tk's fonts.

    One style per colour channel: the knob in the channel's colour and the
    line faintly tinted with it, so the red gain looks like the red gain.
    """
    ratio = max(1.0, root.winfo_fpixels("1i") / 96.0)
    trough_h = max(2, round(_TROUGH_HEIGHT * ratio))
    knob = max(8, round(_KNOB_DIAMETER * ratio))
    variants: dict[str | None, dict[str, str]] = {
        None: {"trough": p.surface_3, "knob": p.text_dim, "knob_hot": p.accent},
    }
    for channel, colour in CHANNELS[theme].items():
        variants[channel] = {
            "trough": _mix(colour, p.surface, 0.62),
            "knob": colour,
            "knob_hot": _mix(colour, p.text, 0.35),
        }

    store: dict[str | None, dict[str, tk.PhotoImage]] | None = getattr(
        root, _IMAGES_ATTRIBUTE, None
    )
    created = store is None
    if store is None:
        store = {}
        setattr(root, _IMAGES_ATTRIBUTE, store)
    for channel, colours in variants.items():
        colours = {**colours, "trough_off": p.surface_2, "knob_off": p.surface_3}
        if channel not in store:
            store[channel] = {
                name: tk.PhotoImage(
                    master=root,
                    width=trough_h * 3 if name.startswith("trough") else knob,
                    height=trough_h if name.startswith("trough") else knob,
                )
                for name in colours
            }
        images = store[channel]
        for name, colour in colours.items():
            _paint_pill(images[name], colour)
        name = scale_style(channel)
        if created:
            _create_scale_elements(style, name, images, trough_h)
        style.configure(name, background=p.surface)
        # clam maps every widget's background to its own light grey when
        # disabled or hovered; around a thin line that grey is the whole widget.
        style.map(
            name,
            background=[
                ("disabled", p.surface),
                ("active", p.surface),
                ("pressed", p.surface),
            ],
        )


def _create_scale_elements(
    style: ttk.Style, name: str, images: dict[str, tk.PhotoImage], trough_h: int
) -> None:
    """Once per interpreter, which is once per root: the elements keep pointing
    at these images from then on, and a theme switch repaints the images."""
    element = name.removesuffix(".TScale") + ".Scale"
    style.element_create(
        f"{element}.trough",
        "image",
        images["trough"],
        ("disabled", images["trough_off"]),
        border=(trough_h, 0),
        sticky="ew",
    )
    style.element_create(
        f"{element}.slider",
        "image",
        images["knob"],
        ("disabled", images["knob_off"]),
        ("pressed", images["knob_hot"]),
        ("active", images["knob_hot"]),
        ("focus", images["knob_hot"]),
    )
    style.layout(
        name,
        [
            (
                f"{element}.trough",
                {
                    "sticky": "nswe",
                    "children": [(f"{element}.slider", {"side": "left", "sticky": ""})],
                },
            )
        ],
    )


def _paint_pill(image: tk.PhotoImage, colour: str) -> None:
    """Fill ``image`` with a pill: a circle when square, a rounded bar when wide."""
    width, height = image.width(), image.height()
    image.blank()
    radius = height / 2
    for y in range(height):
        dy = abs(y + 0.5 - radius)
        if dy > radius:
            continue
        inset = round(radius - (radius * radius - dy * dy) ** 0.5)
        if width - 2 * inset > 0:
            image.put(colour, to=(inset, y, width - inset, y + 1))
