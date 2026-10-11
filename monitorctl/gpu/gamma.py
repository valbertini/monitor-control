"""Software brightness, contrast and gamma through the GDI gamma ramp.

``SetDeviceGammaRamp`` installs three 256-entry lookup tables, one per channel,
between the framebuffer and the output.  That is enough for tone controls -
brightness, contrast, gamma, per-channel white point - and fundamentally not
enough for saturation or hue, which need the channels mixed together.  For
those, see :mod:`monitorctl.gpu.nvapi`.

Two properties of this API drive the design:

* **It lies.**  Windows screens the ramp against a readability heuristic and,
  when the ramp fails it, returns success without applying anything.  Every
  write is therefore read back and compared, the same way DDC writes are.
* **It is shared state.**  f.lux, Night Light, the GPU control panel and this
  module all write the same three tables, last writer wins.  The ramp also
  resets on driver reload, resolution change and reboot.

The three controls are virtual: they are combined into one ramp, so the desired
values are kept here rather than inferred back out of the table.

**Neutral means "not ours".**  While all three sit at 50 this module writes no
ramp at all, so whatever else owns the device's ramp - an ICC profile's
calibration curve, Night Light, f.lux - is left alone; a saved profile at
neutral re-applied on every unlock must not flatten them.  The first move away
from neutral records the ramp found on the device, and the return to neutral
puts that one back instead of an identity curve.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes

from ..ddc.vcp import FeatureCategory
from .base import GpuAdjustment, GpuBackend, GpuReading
from .errors import GpuError

__all__ = ["SOFTWARE_LUT_KEYS", "GammaRampBackend"]

_log = logging.getLogger(__name__)

_RAMP_ENTRIES = 256
_RampArray = ctypes.c_uint16 * _RAMP_ENTRIES * 3

KEY_BRIGHTNESS = "gpu_sw_brightness"
KEY_CONTRAST = "gpu_sw_contrast"
KEY_GAMMA = "gpu_sw_gamma"

#: The three controls.  Their values live in the process that set them and
#: last only while it runs (``close``): a short-lived process such as the CLI
#: neither sees the tray app's values nor can keep its own.
SOFTWARE_LUT_KEYS = frozenset({KEY_BRIGHTNESS, KEY_CONTRAST, KEY_GAMMA})

_SOURCE = "software LUT"

#: All three controls run 0..100 with 50 meaning "no change".  The ranges the
#: endpoints map to are deliberately conservative: a ramp that drives a channel
#: to a flat line is rejected by Windows outright, and one that merely comes
#: close leaves the screen unreadable, which is a miserable thing to do to
#: somebody adjusting a slider.
_NEUTRAL = 50
_GAMMA_MIN, _GAMMA_MAX = 0.5, 2.0
_CONTRAST_MIN, _CONTRAST_MAX = 0.5, 1.5
_BRIGHTNESS_SWING = 0.25

_ADJUSTMENTS: tuple[GpuAdjustment, ...] = (
    GpuAdjustment(
        key=KEY_BRIGHTNESS,
        label="Brightness (software)",
        category=FeatureCategory.GPU_GAMMA.value,
        tooltip=(
            "Lifts or lowers the whole signal on the GPU. 50 is neutral. This "
            "is not backlight: lowering it makes blacks grey rather than "
            "darker, so prefer the monitor's own Brightness and keep this for "
            "going below what the panel allows."
        ),
        minimum=0,
        maximum=100,
        neutral=_NEUTRAL,
        source=_SOURCE,
    ),
    GpuAdjustment(
        key=KEY_CONTRAST,
        label="Contrast (software)",
        category=FeatureCategory.GPU_GAMMA.value,
        tooltip=(
            "Stretches the signal around mid-grey on the GPU. 50 is neutral. "
            "High values clip highlight and shadow detail - check the "
            "calibration patches after changing it."
        ),
        minimum=0,
        maximum=100,
        neutral=_NEUTRAL,
        source=_SOURCE,
    ),
    GpuAdjustment(
        key=KEY_GAMMA,
        label="Gamma (software)",
        category=FeatureCategory.GPU_GAMMA.value,
        tooltip=(
            "Bends the mid-tones without moving black or white. 50 is neutral "
            "(gamma 1.0), lower is darker mid-tones. Useful on monitors that "
            "expose no gamma control of their own - neither monitor tested "
            "answers VCP 0x72."
        ),
        minimum=0,
        maximum=100,
        neutral=_NEUTRAL,
        source=_SOURCE,
    ),
)


def _scale(value: int, low: float, high: float) -> float:
    """Map 0..100 onto ``low..high`` with 50 landing exactly in the middle."""
    if value <= _NEUTRAL:
        span = value / _NEUTRAL
        return low + (1.0 - low) * span
    span = (value - _NEUTRAL) / _NEUTRAL
    return 1.0 + (high - 1.0) * span


def _same_ramp(a: _RampArray, b: _RampArray) -> bool:
    """Equal within what Windows' own rounding of a ramp can change."""
    return all(
        abs(a[channel][probe] - b[channel][probe]) <= 256
        for channel in range(3)
        for probe in (0, 64, 128, 192, 255)
    )


def build_ramp(brightness: int, contrast: int, gamma: int) -> list[int]:
    """Compute the 256-entry curve for the three virtual controls.

    Exposed for testing: the maths is worth checking without a display device.

    Args:
        brightness: 0..100, 50 neutral.
        contrast: 0..100, 50 neutral.
        gamma: 0..100, 50 neutral.

    Returns:
        256 values in 0..65535, monotonically non-decreasing.
    """
    exponent = 1.0 / _scale(gamma, _GAMMA_MIN, _GAMMA_MAX)
    gain = _scale(contrast, _CONTRAST_MIN, _CONTRAST_MAX)
    offset = (brightness - _NEUTRAL) / _NEUTRAL * _BRIGHTNESS_SWING

    curve: list[int] = []
    for index in range(_RAMP_ENTRIES):
        x = index / (_RAMP_ENTRIES - 1)
        y = x**exponent
        y = (y - 0.5) * gain + 0.5
        y += offset
        y = min(1.0, max(0.0, y))
        curve.append(round(y * 65535))
    return curve


class GammaRampBackend(GpuBackend):
    """Tone controls via ``SetDeviceGammaRamp``."""

    name = "gamma"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: Devices seen so far; any device Windows can open qualifies, so they
        #: are registered on first sight (:meth:`track`).
        self._devices: list[str] = []
        #: Desired values per device; the ramp itself cannot be inverted back
        #: into these three numbers unambiguously.
        self._state: dict[str, dict[str, int]] = {}
        #: The ramp each device had before this module took it over, put back
        #: when its controls return to neutral and when the app exits.
        self._original: dict[str, _RampArray] = {}
        #: The ramp this module last wrote to each device: what tells whether
        #: the ramp there is still ours.
        self._written: dict[str, _RampArray] = {}
        self._available = sys.platform == "win32"
        self._gdi: ctypes.WinDLL | None = None
        if self._available:
            try:
                self._gdi = self._bind()
            except Exception as exc:  # pragma: no cover - defensive
                _log.info("gamma ramp backend unavailable: %s", exc)
                self._available = False

    @staticmethod
    def _bind() -> ctypes.WinDLL:
        gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        gdi.CreateDCW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
        ]
        gdi.CreateDCW.restype = wintypes.HDC
        gdi.DeleteDC.argtypes = [wintypes.HDC]
        gdi.DeleteDC.restype = wintypes.BOOL
        gdi.GetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
        gdi.GetDeviceGammaRamp.restype = wintypes.BOOL
        gdi.SetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
        gdi.SetDeviceGammaRamp.restype = wintypes.BOOL
        return gdi

    # ------------------------------------------------------------------ #
    # Device context helpers
    # ------------------------------------------------------------------ #

    def _open(self, device: str) -> wintypes.HDC:
        assert self._gdi is not None
        hdc = self._gdi.CreateDCW(device, device, None, None)
        if not hdc:
            raise GpuError(f"CreateDC failed for {device!r}")
        return hdc

    def _read_ramp(self, device: str):
        assert self._gdi is not None
        hdc = self._open(device)
        try:
            ramp = _RampArray()
            if not self._gdi.GetDeviceGammaRamp(hdc, ctypes.byref(ramp)):
                raise GpuError(f"GetDeviceGammaRamp failed for {device!r}")
            return ramp
        finally:
            self._gdi.DeleteDC(hdc)

    def _write_ramp(self, device: str, ramp: _RampArray) -> None:
        assert self._gdi is not None
        hdc = self._open(device)
        try:
            if not self._gdi.SetDeviceGammaRamp(hdc, ctypes.byref(ramp)):
                raise GpuError(
                    f"SetDeviceGammaRamp was rejected for {device!r}; the curve "
                    "is probably outside the range Windows allows"
                )
            # The call returns success even when the ramp was silently
            # discarded, so confirm by reading it back.
            applied = _RampArray()
            if not self._gdi.GetDeviceGammaRamp(hdc, ctypes.byref(applied)):
                raise GpuError(f"could not read back the ramp for {device!r}")
        finally:
            self._gdi.DeleteDC(hdc)

        if not _same_ramp(applied, ramp):
            raise GpuError(
                f"Windows reported success but did not apply the ramp for "
                f"{device!r}. This usually means the requested curve failed "
                "its readability check."
            )
        self._written[device] = ramp

    @staticmethod
    def _grey_ramp(curve: list[int]) -> _RampArray:
        """The same curve on all three channels."""
        ramp = _RampArray()
        for channel in range(3):
            for index, value in enumerate(curve):
                ramp[channel][index] = value
        return ramp

    # ------------------------------------------------------------------ #
    # GpuBackend
    # ------------------------------------------------------------------ #

    def available(self) -> bool:
        return self._available

    def devices(self) -> tuple[str, ...]:
        return tuple(self._devices)

    def track(self, device: str) -> None:
        """Register a device so it shows up in :meth:`devices`."""
        if device and device not in self._devices:
            self._devices.append(device)

    def adjustments(self) -> tuple[GpuAdjustment, ...]:
        return _ADJUSTMENTS

    def _values(self, device: str) -> dict[str, int]:
        return self._state.setdefault(
            device,
            {KEY_BRIGHTNESS: _NEUTRAL, KEY_CONTRAST: _NEUTRAL, KEY_GAMMA: _NEUTRAL},
        )

    def get(self, device: str, key: str) -> GpuReading:
        with self._lock:
            if key not in (KEY_BRIGHTNESS, KEY_CONTRAST, KEY_GAMMA):
                raise GpuError(f"unknown gamma adjustment {key!r}")
            # Confirm the device really has a ramp before claiming support.
            ramp = self._read_ramp(device)
            values = self._values(device)
            if any(v != _NEUTRAL for v in values.values()) and not self._ours(
                device, ramp
            ):
                # A driver reset, Night Light or a game replaced our ramp: the
                # adjustment is no longer on screen, so it must not read as
                # applied - a Restore comparing against it would call it
                # "already correct" and leave the screen without it.
                _log.info("the gamma ramp of %s is no longer ours", device)
                values.update(dict.fromkeys(values, _NEUTRAL))
                self._original.pop(device, None)
                self._written.pop(device, None)
            return GpuReading(current=values[key], minimum=0, maximum=100)

    def set(self, device: str, key: str, value: int) -> int:
        with self._lock:
            values = self._values(device)
            if key not in values:
                raise GpuError(f"unknown gamma adjustment {key!r}")
            clamped = max(0, min(100, value))
            previous = values[key]
            values[key] = clamped
            try:
                self._apply(device, values)
            except GpuError:
                values[key] = previous  # keep the cache honest
                raise
            return clamped

    def _ours(self, device: str, current: _RampArray | None = None) -> bool:
        """Whether the ramp on ``device`` is still the one this module wrote."""
        written = self._written.get(device)
        if written is None:
            return False
        if current is None:
            current = self._read_ramp(device)
        return _same_ramp(current, written)

    def _give_back(self, device: str) -> None:
        """Put the original ramp back, if the one there is still ours.

        Forgotten only once written: a failed write keeps it for the next try.
        If something else replaced our ramp meanwhile - a driver reset, Night
        Light, or another monitor now answering to this device name - theirs
        stays.
        """
        original = self._original.get(device)
        if original is not None and self._ours(device):
            self._write_ramp(device, original)
        self._original.pop(device, None)
        self._written.pop(device, None)

    def _apply(self, device: str, values: dict[str, int]) -> None:
        if all(v == _NEUTRAL for v in values.values()):
            # Never ours, or no longer needed: whatever else is there stays.
            self._give_back(device)
            return
        current = self._read_ramp(device)
        if device not in self._original or not self._ours(device, current):
            # First take-over, or the ramp was replaced behind our back: what
            # is there now is what to give back later.
            self._original[device] = current
        self._write_ramp(
            device,
            self._grey_ramp(
                build_ramp(
                    values[KEY_BRIGHTNESS], values[KEY_CONTRAST], values[KEY_GAMMA]
                )
            ),
        )

    def close(self) -> None:
        """Give every device its original ramp back.

        The adjustments are monitorctl's to keep applied, and only while it
        runs: left behind, the next start would read our ramp as the
        device's own and could never return to the real one.  Saved
        settings are put back at the next start anyway.  A ramp that is no
        longer ours is left alone.
        """
        with self._lock:
            for device in list(self._original):
                try:
                    self._give_back(device)
                except GpuError as exc:
                    _log.warning("could not give %s its ramp back: %s", device, exc)
            self._state.clear()
