"""How the window presents the controls: section order, icons, hints, colours.

The registry (``ddc/vcp.py``) says what each control *is*; this module says how
a person should meet it.  Kept apart so that neither the service nor the CLI
carries presentation, and so the reasoning lives in one place.  The research
behind it is in ``docs/PESQUISA-UI.md``.

* **Order follows how often a control matters**, as the user set it (D28):
  brightness and contrast; colour intensity; the white and black balance;
  colour by hue; the colour temperature and the picture mode; then what this
  PC adds on top; and last what is set once and rarely touched again - sound,
  VGA geometry, the input and the menu language.  Writes do not follow this
  order - a restore keeps the registry's, coarse before fine.
* **Every card says where the change happens**: on the monitor, in its
  hardware, or on this PC, in software (``Section.on_pc``).  They are
  different things - the PC's adjustments bend the signal, are lost when
  monitorctl stops, and apply on top of the monitor's.
* **Every section has an icon and a one-line hint** saying what it changes, so
  "gain" and "offset" do not have to be known words.
* **A control tied to one colour is drawn in that colour**: a dot before its
  name and its slider.  Colour is never the only cue - the name stays written
  (WCAG 1.4.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

__all__ = [
    "CHANNEL_OF",
    "ICON_FONT",
    "SECTIONS",
    "Section",
    "section_key",
    "section_named",
]

#: Windows' icon font, on Windows 10 and 11.  Without it the icons are left out.
ICON_FONT = "Segoe MDL2 Assets"


@dataclass(frozen=True, slots=True)
class Section:
    """One card of a monitor's page.

    Attributes:
        key: A registry category, or a section of its own (see
            :data:`_SECTION_OF_FEATURE`).
        icon: Glyph in :data:`ICON_FONT`, or ``""``.
        hint: What the controls in it change, in a few words.
        title: Overrides the category's label; needed only for a section that
            is not a category.
        on_pc: Applied by this PC (the graphics card or driver), in software,
            rather than by the monitor's hardware.
    """

    key: str
    icon: str
    hint: str
    title: str = ""
    on_pc: bool = False


SECTIONS: tuple[Section, ...] = (
    Section(
        "luminance", "\ue706", "How bright the screen is, and how far white is from black"
    ),
    Section(
        "image", "\ue7b3", "Colour intensity, detail and the monitor's own processing"
    ),
    Section(
        "white_balance", "\ue793", "Tints the bright end: use it to neutralise whites"
    ),
    Section("black_level", "\ue708", "Tints the dark end: use it to neutralise shadows"),
    Section("saturation", "\ue790", "Colour intensity of one hue at a time"),
    Section("hue", "\ue7ad", "Shifts one hue at a time around the colour wheel"),
    Section("color_temperature", "\ue9ca", "How warm or cool white looks"),
    Section(
        "mode",
        "\ueb9f",
        "A preset for the whole picture; switching it can reset the controls above",
        title="Picture Mode",
    ),
    Section(
        "gpu_gamma",
        "\ue9d9",
        "Bends the signal after the monitor's own settings; set those first",
        on_pc=True,
    ),
    Section(
        "gpu_color",
        "\ue950",
        "Applied by the graphics driver on top of the monitor",
        on_pc=True,
    ),
    Section("audio", "\ue767", "The monitor's own speakers or headphone jack"),
    Section("geometry", "\ue9a6", "Only does anything on a VGA (D-Sub) input"),
    Section("input", "\ue7b5", "Which connector the monitor shows"),
    Section("osd", "\ue713", "The monitor's own on-screen menu"),
)

#: Controls shown outside their registry category.  The picture mode belongs to
#: "image" in the standard, but it is a preset over everything else, so it gets
#: a card of its own, beside the colour temperature presets.
_SECTION_OF_FEATURE = MappingProxyType({"picture_mode": "mode"})

_BY_KEY = {section.key: section for section in SECTIONS}


def section_key(feature_key: str, category: str) -> str:
    """The section a control is shown in."""
    return _SECTION_OF_FEATURE.get(feature_key, category)


def section_named(key: str) -> Section:
    """The section ``key``; an unknown one gets no icon and no hint."""
    return _BY_KEY.get(key, Section(key, "", ""))


#: Controls that act on a single colour, by feature key.
CHANNEL_OF = MappingProxyType(
    {
        "red_gain": "red",
        "green_gain": "green",
        "blue_gain": "blue",
        "red_black_level": "red",
        "green_black_level": "green",
        "blue_black_level": "blue",
        "saturation_red": "red",
        "saturation_yellow": "yellow",
        "saturation_green": "green",
        "saturation_cyan": "cyan",
        "saturation_blue": "blue",
        "saturation_magenta": "magenta",
        "hue_red": "red",
        "hue_yellow": "yellow",
        "hue_green": "green",
        "hue_cyan": "cyan",
        "hue_blue": "blue",
        "hue_magenta": "magenta",
    }
)
