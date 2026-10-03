"""VCP feature registry.

A VCP (Virtual Control Panel) code is a single byte naming one monitor setting
in the VESA MCCS language.  The standard defines well over a hundred; this
registry exposes the subset that is both useful and safe to write.

Anything not listed here cannot be written by this application.  That is the
point: the allowlist *is* the primary safety mechanism, because the only
documented case of DDC/CI permanently damaging a monitor involves writing to
manufacturer-reserved codes.  See ``docs/PESQUISA-DDC-CI.md`` section 3.1.

Features carry a :class:`FeatureCategory` so the UI can group them, and a
``tooltip`` explaining what the control does and when it has no effect.  Most
monitors implement only a fraction of this list; the ones they do not implement
are shown disabled rather than hidden, so it stays obvious what exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

__all__ = [
    "CATEGORIES",
    "FEATURES",
    "FEATURES_BY_CODE",
    "CategoryInfo",
    "FeatureCategory",
    "FeatureKind",
    "VcpFeature",
    "feature_by_code",
]


class FeatureKind(Enum):
    """MCCS distinguishes continuous ranges from enumerated value sets."""

    CONTINUOUS = "continuous"
    """A 0..max integer range, e.g. brightness."""

    NON_CONTINUOUS = "non_continuous"
    """A set of discrete named values, e.g. input source."""


class FeatureCategory(Enum):
    """UI grouping.  Declaration order is display order."""

    LUMINANCE = "luminance"
    IMAGE = "image"
    COLOR_TEMPERATURE = "color_temperature"
    WHITE_BALANCE = "white_balance"
    BLACK_LEVEL = "black_level"
    SATURATION = "saturation"
    HUE = "hue"
    INPUT = "input"
    AUDIO = "audio"
    GEOMETRY = "geometry"
    OSD = "osd"
    # Applied on the GPU rather than by the monitor.  Last, so the
    # monitor's own controls come first in the UI.
    GPU_COLOR = "gpu_color"
    GPU_GAMMA = "gpu_gamma"


@dataclass(frozen=True, slots=True)
class CategoryInfo:
    """Presentation metadata for one category."""

    key: str
    label: str
    description: str


CATEGORIES: Mapping[str, CategoryInfo] = MappingProxyType(
    {
        FeatureCategory.LUMINANCE.value: CategoryInfo(
            key=FeatureCategory.LUMINANCE.value,
            label="Brightness & Contrast",
            description="Backlight output and the spread between black and white.",
        ),
        FeatureCategory.IMAGE.value: CategoryInfo(
            key=FeatureCategory.IMAGE.value,
            label="Image",
            description="Overall picture processing applied by the monitor's scaler.",
        ),
        FeatureCategory.COLOR_TEMPERATURE.value: CategoryInfo(
            key=FeatureCategory.COLOR_TEMPERATURE.value,
            label="Colour Temperature",
            description="How warm or cool white appears.",
        ),
        FeatureCategory.WHITE_BALANCE.value: CategoryInfo(
            key=FeatureCategory.WHITE_BALANCE.value,
            label="White Balance - Gain",
            description=(
                "Per-channel gain, which shifts the bright end of the scale. "
                "Use these to neutralise a colour cast in whites."
            ),
        ),
        FeatureCategory.BLACK_LEVEL.value: CategoryInfo(
            key=FeatureCategory.BLACK_LEVEL.value,
            label="Black Level - Offset",
            description=(
                "Per-channel offset, which shifts the dark end of the scale. "
                "Use these to neutralise a colour cast in shadows."
            ),
        ),
        FeatureCategory.SATURATION.value: CategoryInfo(
            key=FeatureCategory.SATURATION.value,
            label="Six-Axis Saturation",
            description="Colour intensity per primary and secondary hue.",
        ),
        FeatureCategory.HUE.value: CategoryInfo(
            key=FeatureCategory.HUE.value,
            label="Six-Axis Hue",
            description="Angular shift per primary and secondary hue.",
        ),
        FeatureCategory.INPUT.value: CategoryInfo(
            key=FeatureCategory.INPUT.value,
            label="Input",
            description="Which physical port the monitor displays.",
        ),
        FeatureCategory.AUDIO.value: CategoryInfo(
            key=FeatureCategory.AUDIO.value,
            label="Audio",
            description="Built-in speaker controls, where the monitor has speakers.",
        ),
        FeatureCategory.GEOMETRY.value: CategoryInfo(
            key=FeatureCategory.GEOMETRY.value,
            label="Geometry (analogue inputs)",
            description=(
                "Image placement and pixel clock. These only do anything on a "
                "VGA/D-Sub input; on a digital input they are inert and may "
                "report nonsense values."
            ),
        ),
        FeatureCategory.OSD.value: CategoryInfo(
            key=FeatureCategory.OSD.value,
            label="On-Screen Display",
            description="The monitor's own menu.",
        ),
        FeatureCategory.GPU_COLOR.value: CategoryInfo(
            key=FeatureCategory.GPU_COLOR.value,
            label="GPU Colour (driver)",
            description=(
                "Applied by the graphics driver, not the monitor. Use these "
                "when the panel has no equivalent control - saturation and hue "
                "need the colour channels mixed, which DDC/CI cannot do if the "
                "firmware does not implement it."
            ),
        ),
        FeatureCategory.GPU_GAMMA.value: CategoryInfo(
            key=FeatureCategory.GPU_GAMMA.value,
            label="GPU Tone Curve (software)",
            description=(
                "A per-channel lookup table between the framebuffer and the "
                "output. Shared with anything else that writes it (f.lux, "
                "Night Light) and reset by a driver reload or reboot."
            ),
        ),
    }
)


@dataclass(frozen=True, slots=True)
class VcpFeature:
    """Static metadata for one VCP code.

    Attributes:
        code: The MCCS VCP code.
        key: Stable machine-readable identifier: the name in saved profiles
            and on the command line.
        label: Short human-readable name.
        kind: Continuous range or enumerated set.
        category: UI grouping.
        tooltip: Longer explanation shown on hover.
        values: For non-continuous features, the standard value -> name map.
            Monitors advertise which of these they actually accept in their
            capabilities string; this map only supplies the names.
    """

    code: int
    key: str
    label: str
    kind: FeatureKind
    category: FeatureCategory
    tooltip: str
    values: Mapping[int, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not 0 <= self.code <= 0xFF:
            raise ValueError(f"VCP code out of range: {self.code}")
        if self.kind is FeatureKind.NON_CONTINUOUS and not self.values:
            raise ValueError(f"non-continuous feature {self.key!r} needs a value map")
        if self.kind is FeatureKind.CONTINUOUS and self.values:
            raise ValueError(f"continuous feature {self.key!r} must not define values")


def _values(pairs: dict[int, str]) -> Mapping[int, str]:
    return MappingProxyType(dict(pairs))


# MCCS 2.2a, "Input Source" (VCP 0x60).
_INPUT_SOURCES = _values(
    {
        0x01: "VGA 1",
        0x02: "VGA 2",
        0x03: "DVI 1",
        0x04: "DVI 2",
        0x05: "Composite 1",
        0x06: "Composite 2",
        0x07: "S-Video 1",
        0x08: "S-Video 2",
        0x09: "Tuner 1",
        0x0A: "Tuner 2",
        0x0B: "Tuner 3",
        0x0C: "Component 1",
        0x0D: "Component 2",
        0x0E: "Component 3",
        0x0F: "DisplayPort 1",
        0x10: "DisplayPort 2",
        0x11: "HDMI 1",
        0x12: "HDMI 2",
    }
)

# MCCS 2.2a, "Select Color Preset" (VCP 0x14).
_COLOR_PRESETS = _values(
    {
        0x01: "sRGB",
        0x02: "Display native",
        0x03: "4000 K",
        0x04: "5000 K",
        0x05: "6500 K",
        0x06: "7500 K",
        0x07: "8200 K",
        0x08: "9300 K",
        0x09: "10000 K",
        0x0A: "11500 K",
        0x0B: "User 1",
        0x0C: "User 2",
        0x0D: "User 3",
    }
)

_MUTE = _values({0x01: "Muted", 0x02: "Unmuted"})

# MCCS 2.2a, "Display Scaling" (VCP 0x86).  Abridged to the values monitors
# actually implement; the full table has a dozen expansion variants.
_SCALING = _values(
    {
        0x01: "No scaling",
        0x02: "Fill, ignore aspect",
        0x03: "Fit height, keep aspect",
        0x04: "Fit width, keep aspect",
        0x05: "Fit height, stretch",
        0x06: "Fit width, stretch",
        0x07: "Linear expansion (H)",
        0x08: "Linear expansion (H+V)",
        0x09: "Squeeze (H)",
        0x0A: "Squeeze (H+V)",
        0x0B: "Non-linear expansion",
    }
)

# MCCS 2.2a, "Screen Orientation" (VCP 0xAA).
_ORIENTATION = _values(
    {
        0x01: "0 degrees",
        0x02: "90 degrees",
        0x03: "180 degrees",
        0x04: "270 degrees",
    }
)

# MCCS 2.2a, "Auto Setup" (VCP 0x1E).  Writing 0x02 triggers one pass.
_AUTO_SETUP = _values({0x01: "Idle", 0x02: "Run auto setup now"})

# MCCS 2.2a, "Display Application" (VCP 0xDC).  Several vendors map their own
# picture-mode menu onto this code, so the names below are the standard ones and
# may not match what the monitor's own OSD calls them.
_PICTURE_MODE = _values(
    {
        0x00: "Standard / default",
        0x01: "Productivity",
        0x02: "Mixed",
        0x03: "Movie",
        0x04: "User defined",
        0x05: "Games",
        0x06: "Sports",
        0x07: "Professional",
        0x08: "Standard, uniform white",
    }
)

# MCCS 2.2a, "OSD Language" (VCP 0xCC).  The ten values an ASUS VS248 advertises
# match its manual exactly, which is good evidence this table is right.
_OSD_LANGUAGE = _values(
    {
        0x01: "Chinese (traditional)",
        0x02: "English",
        0x03: "French",
        0x04: "German",
        0x05: "Italian",
        0x06: "Japanese",
        0x07: "Korean",
        0x08: "Portuguese (Portugal)",
        0x09: "Russian",
        0x0A: "Spanish",
        0x0B: "Swedish",
        0x0C: "Turkish",
        0x0D: "Chinese (simplified)",
        0x0E: "Portuguese (Brazil)",
        0x0F: "Arabic",
        0x10: "Bulgarian",
        0x11: "Croatian",
        0x12: "Czech",
        0x13: "Danish",
        0x14: "Dutch",
        0x15: "Estonian",
        0x16: "Finnish",
        0x17: "Greek",
        0x18: "Hebrew",
        0x19: "Hindi",
        0x1A: "Hungarian",
        0x1B: "Latvian",
        0x1C: "Lithuanian",
        0x1D: "Norwegian",
        0x1E: "Polish",
        0x1F: "Romanian",
        0x20: "Serbian",
        0x21: "Slovak",
        0x22: "Slovenian",
        0x23: "Thai",
        0x24: "Ukrainian",
        0x25: "Vietnamese",
    }
)


def _c(
    code: int,
    key: str,
    label: str,
    category: FeatureCategory,
    tooltip: str,
) -> VcpFeature:
    """Shorthand for a continuous feature."""
    return VcpFeature(
        code=code,
        key=key,
        label=label,
        kind=FeatureKind.CONTINUOUS,
        category=category,
        tooltip=tooltip,
    )


def _nc(
    code: int,
    key: str,
    label: str,
    category: FeatureCategory,
    tooltip: str,
    values: Mapping[int, str],
) -> VcpFeature:
    """Shorthand for a non-continuous feature."""
    return VcpFeature(
        code=code,
        key=key,
        label=label,
        kind=FeatureKind.NON_CONTINUOUS,
        category=category,
        tooltip=tooltip,
        values=values,
    )


_ALL: tuple[VcpFeature, ...] = (
    # ---- Brightness & contrast ------------------------------------------ #
    _c(
        0x10,
        "brightness",
        "Brightness",
        FeatureCategory.LUMINANCE,
        "Panel luminance. The most widely supported control of all. On an LED "
        "backlight this drives the backlight, not the image data, so it does "
        "not crush shadows the way a contrast change does.",
    ),
    _c(
        0x12,
        "contrast",
        "Contrast",
        FeatureCategory.LUMINANCE,
        "Distance between black and white. Too high clips highlight detail; "
        "check the near-white patches in the calibration panel after changing it.",
    ),
    _c(
        0x13,
        "backlight",
        "Backlight level",
        FeatureCategory.LUMINANCE,
        "Direct backlight control, separate from Brightness. Rare: most monitors "
        "expose only Brightness and drive the backlight from it.",
    ),
    # ---- Image ----------------------------------------------------------- #
    _c(
        0x87,
        "sharpness",
        "Sharpness",
        FeatureCategory.IMAGE,
        "Edge enhancement applied by the scaler. At native resolution the "
        "neutral setting is usually best; higher values add halos around edges.",
    ),
    _c(
        0x8A,
        "color_saturation",
        "Colour saturation",
        FeatureCategory.IMAGE,
        "Overall colour intensity, all hues at once. For per-hue control use "
        "the Six-Axis Saturation group.",
    ),
    _c(
        0x90,
        "hue",
        "Hue",
        FeatureCategory.IMAGE,
        "Rotates every colour around the wheel at once. For per-hue control use "
        "the Six-Axis Hue group.",
    ),
    _nc(
        0x86,
        "display_scaling",
        "Aspect / scaling",
        FeatureCategory.IMAGE,
        "How a signal that does not match the panel's native resolution is "
        "stretched to fill it.",
        _SCALING,
    ),
    _nc(
        0xDC,
        "picture_mode",
        "Picture mode",
        FeatureCategory.IMAGE,
        "Preset picture profile. Vendors often map their own menu (ASUS "
        "Splendid, for example) onto this code, so the names shown are the "
        "MCCS standard ones and may not match the monitor's own labels.",
        _PICTURE_MODE,
    ),
    # ---- Colour temperature ---------------------------------------------- #
    _nc(
        0x14,
        "color_preset",
        "Colour preset",
        FeatureCategory.COLOR_TEMPERATURE,
        "Factory white-point preset. Selecting a preset usually overrides any "
        "manual gain you set in White Balance.",
        _COLOR_PRESETS,
    ),
    _c(
        0x0C,
        "color_temperature",
        "Colour temperature (fine)",
        FeatureCategory.COLOR_TEMPERATURE,
        "Fine colour temperature, in steps above 3000 K. The step size comes "
        "from VCP 0x0B and is typically 50 K, so a value of 90 means "
        "3000 + 90 x 50 = 7500 K.",
    ),
    # ---- White balance: gain (bright end) -------------------------------- #
    _c(
        0x16,
        "red_gain",
        "Red gain",
        FeatureCategory.WHITE_BALANCE,
        "Red channel gain. Raises or lowers red at the bright end of the scale. "
        "Adjust against the white and light-grey patches.",
    ),
    _c(
        0x18,
        "green_gain",
        "Green gain",
        FeatureCategory.WHITE_BALANCE,
        "Green channel gain, at the bright end of the scale.",
    ),
    _c(
        0x1A,
        "blue_gain",
        "Blue gain",
        FeatureCategory.WHITE_BALANCE,
        "Blue channel gain, at the bright end of the scale.",
    ),
    # ---- Black level: offset (dark end) ---------------------------------- #
    _c(
        0x6C,
        "red_black_level",
        "Red black level",
        FeatureCategory.BLACK_LEVEL,
        "Red channel offset. Shifts red at the dark end of the scale. Adjust "
        "against the near-black patches until they stay neutral grey.",
    ),
    _c(
        0x6E,
        "green_black_level",
        "Green black level",
        FeatureCategory.BLACK_LEVEL,
        "Green channel offset, at the dark end of the scale.",
    ),
    _c(
        0x70,
        "blue_black_level",
        "Blue black level",
        FeatureCategory.BLACK_LEVEL,
        "Blue channel offset, at the dark end of the scale.",
    ),
    # ---- Six-axis saturation --------------------------------------------- #
    _c(
        0x59,
        "saturation_red",
        "Red saturation",
        FeatureCategory.SATURATION,
        "Intensity of red only, leaving other hues alone.",
    ),
    _c(
        0x5A,
        "saturation_yellow",
        "Yellow saturation",
        FeatureCategory.SATURATION,
        "Intensity of yellow only.",
    ),
    _c(
        0x5B,
        "saturation_green",
        "Green saturation",
        FeatureCategory.SATURATION,
        "Intensity of green only.",
    ),
    _c(
        0x5C,
        "saturation_cyan",
        "Cyan saturation",
        FeatureCategory.SATURATION,
        "Intensity of cyan only.",
    ),
    _c(
        0x5D,
        "saturation_blue",
        "Blue saturation",
        FeatureCategory.SATURATION,
        "Intensity of blue only.",
    ),
    _c(
        0x5E,
        "saturation_magenta",
        "Magenta saturation",
        FeatureCategory.SATURATION,
        "Intensity of magenta only.",
    ),
    # ---- Six-axis hue ----------------------------------------------------- #
    _c(
        0x9B,
        "hue_red",
        "Red hue",
        FeatureCategory.HUE,
        "Rotates red towards magenta or yellow without touching other hues.",
    ),
    _c(
        0x9C,
        "hue_yellow",
        "Yellow hue",
        FeatureCategory.HUE,
        "Rotates yellow towards red or green.",
    ),
    _c(
        0x9D,
        "hue_green",
        "Green hue",
        FeatureCategory.HUE,
        "Rotates green towards yellow or cyan.",
    ),
    _c(
        0x9E,
        "hue_cyan",
        "Cyan hue",
        FeatureCategory.HUE,
        "Rotates cyan towards green or blue.",
    ),
    _c(
        0x9F,
        "hue_blue",
        "Blue hue",
        FeatureCategory.HUE,
        "Rotates blue towards cyan or magenta.",
    ),
    _c(
        0xA0,
        "hue_magenta",
        "Magenta hue",
        FeatureCategory.HUE,
        "Rotates magenta towards blue or red.",
    ),
    # ---- Input ------------------------------------------------------------ #
    _nc(
        0x60,
        "input_source",
        "Input source",
        FeatureCategory.INPUT,
        "Active video input. Switching to a port with no signal will blank the "
        "screen until you switch back with the monitor's own buttons. The names "
        "are the MCCS standard ones and do not always match the physical ports.",
        _INPUT_SOURCES,
    ),
    # ---- Audio ------------------------------------------------------------ #
    _c(
        0x62,
        "volume",
        "Volume",
        FeatureCategory.AUDIO,
        "Built-in speaker volume. Some monitors answer this control even with "
        "no speakers fitted.",
    ),
    _nc(
        0x8D,
        "audio_mute",
        "Mute",
        FeatureCategory.AUDIO,
        "Silences the built-in speaker.",
        _MUTE,
    ),
    _c(
        0x8F,
        "audio_treble",
        "Treble",
        FeatureCategory.AUDIO,
        "Built-in speaker treble.",
    ),
    _c(
        0x91,
        "audio_bass",
        "Bass",
        FeatureCategory.AUDIO,
        "Built-in speaker bass.",
    ),
    _c(
        0x93,
        "audio_balance",
        "Balance",
        FeatureCategory.AUDIO,
        "Left/right balance of the built-in speakers.",
    ),
    # ---- Geometry (analogue only) ----------------------------------------- #
    _nc(
        0x1E,
        "auto_setup",
        "Auto setup",
        FeatureCategory.GEOMETRY,
        "Runs the monitor's automatic image alignment. Analogue inputs only; "
        "the screen may flicker for a second while it runs.",
        _AUTO_SETUP,
    ),
    _c(
        0x20,
        "horizontal_position",
        "Horizontal position",
        FeatureCategory.GEOMETRY,
        "Moves the image left or right. Analogue inputs only.",
    ),
    _c(
        0x30,
        "vertical_position",
        "Vertical position",
        FeatureCategory.GEOMETRY,
        "Moves the image up or down. Analogue inputs only.",
    ),
    _c(
        0x22,
        "horizontal_size",
        "Horizontal size",
        FeatureCategory.GEOMETRY,
        "Stretches or compresses the image horizontally. Analogue inputs only.",
    ),
    _c(
        0x32,
        "vertical_size",
        "Vertical size",
        FeatureCategory.GEOMETRY,
        "Stretches or compresses the image vertically. Analogue inputs only.",
    ),
    _c(
        0x0E,
        "clock",
        "Pixel clock",
        FeatureCategory.GEOMETRY,
        "Pixel sampling frequency. Wrong values show vertical banding. "
        "Analogue inputs only.",
    ),
    _c(
        0x3E,
        "clock_phase",
        "Clock phase",
        FeatureCategory.GEOMETRY,
        "Phase of the pixel sampling clock. Wrong values show shimmer on fine "
        "detail. Analogue inputs only.",
    ),
    # ---- OSD --------------------------------------------------------------- #
    _nc(
        0xCC,
        "osd_language",
        "OSD language",
        FeatureCategory.OSD,
        "Language of the monitor's own menu. Changing this makes the physical "
        "menu harder to read if you pick a language you do not know — it is "
        "reversible from here.",
        _OSD_LANGUAGE,
    ),
    _nc(
        0xAA,
        "screen_orientation",
        "Screen orientation",
        FeatureCategory.OSD,
        "Rotates the image inside the monitor. This does not tell Windows, so "
        "the desktop layout will not follow.",
        _ORIENTATION,
    ),
)

FEATURES: Mapping[str, VcpFeature] = MappingProxyType({f.key: f for f in _ALL})
FEATURES_BY_CODE: Mapping[int, VcpFeature] = MappingProxyType({f.code: f for f in _ALL})

if len(FEATURES) != len(_ALL) or len(FEATURES_BY_CODE) != len(_ALL):
    raise AssertionError("duplicate key or code in the VCP feature registry")


def feature_by_code(code: int) -> VcpFeature | None:
    """Look up a feature by its VCP code, or ``None`` if not in the registry."""
    return FEATURES_BY_CODE.get(code)
