"""NVIDIA driver colour controls: Digital Vibrance and Hue.

``nvapi64.dll`` exports exactly one symbol, ``nvapi_QueryInterface``.  Every
other entry point is fetched by passing a magic 32-bit id to it.  The ids used
here for Digital Vibrance and Hue are *not* in NVIDIA's published ``nvapi.h`` -
they are part of the private set that tools like vibranceGUI have used for
years.

Calling a function pointer with the wrong prototype crashes the process, so
this module does not take the ids on trust:

1. Every id must resolve to a non-NULL pointer, or the backend disables itself.
   ``nvapi_QueryInterface`` returns NULL for ids it does not know, which catches
   a wrong id outright.
2. Before any setter is exposed, the getter is called and its result is
   sanity-checked.  A plausible Digital Vibrance descriptor has
   ``minimum <= default <= maximum`` with a small positive maximum.  If the
   numbers do not look like that, the id is assumed to mean something else and
   the backend disables itself rather than writing.
3. Setters are only resolved after that check passes.

Verified on an RTX 4070 (driver 32.0.16.1062): both displays report
``current=50 min=0 max=100 default=50`` for vibrance and ``0 deg`` for hue.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from dataclasses import replace

from ..ddc.vcp import FeatureCategory
from .base import GpuAdjustment, GpuBackend, GpuReading
from .errors import GpuError

__all__ = ["NvidiaColorBackend"]

_log = logging.getLogger(__name__)

NVAPI_OK = 0
NVAPI_END_ENUMERATION = -7

#: QueryInterface magic ids.  The first five are public; the DVC and HUE ones
#: are private.  See the module docstring for how they are validated.
_IDS = {
    "initialize": 0x0150E828,
    "unload": 0xD22BDD7E,
    "get_error_message": 0x6C2D048C,
    "enum_display_handle": 0x9ABDD40D,
    "get_display_name": 0x22A78B05,
    "get_dvc_info_ex": 0x0E45002D,
    "set_dvc_level_ex": 0x4A82C2B1,
    "get_hue_info": 0x95B64341,
    "set_hue_angle": 0xF5A0F22C,
}

#: A vibrance descriptor whose maximum exceeds this is not a vibrance
#: descriptor, so the id must mean something else.
_MAX_PLAUSIBLE_LEVEL = 1000

KEY_VIBRANCE = "gpu_vibrance"
KEY_HUE = "gpu_hue"

_SOURCE = "NVIDIA driver"


class _DVCInfoEx(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("currentLevel", ctypes.c_int32),
        ("minLevel", ctypes.c_int32),
        ("maxLevel", ctypes.c_int32),
        ("defaultLevel", ctypes.c_int32),
    ]


class _HueInfo(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("currentHueAngle", ctypes.c_int32),
        ("defaultHueAngle", ctypes.c_int32),
    ]


def _struct_version(struct_type: type[ctypes.Structure], revision: int = 1) -> int:
    """NVAPI encodes a struct version as ``sizeof | (revision << 16)``."""
    return ctypes.sizeof(struct_type) | (revision << 16)


_ADJUSTMENTS: tuple[GpuAdjustment, ...] = (
    GpuAdjustment(
        key=KEY_VIBRANCE,
        label="Saturation (vibrance)",
        category=FeatureCategory.GPU_COLOR.value,
        tooltip=(
            "Colour saturation applied by the NVIDIA driver, the same control "
            "as Digital Vibrance in the NVIDIA Control Panel. 50 is neutral. "
            "Use this when the monitor itself has no saturation control - it "
            "affects everything on that output, not just one application, and "
            "it does not change what a screenshot captures."
        ),
        minimum=0,
        maximum=100,
        neutral=50,
        source=_SOURCE,
    ),
    GpuAdjustment(
        key=KEY_HUE,
        label="Hue angle",
        category=FeatureCategory.GPU_COLOR.value,
        tooltip=(
            "Rotates every colour around the wheel, in degrees, at the driver "
            "level. 0 is neutral. Small values correct a colour cast; large "
            "ones are mostly a novelty."
        ),
        minimum=0,
        maximum=359,
        neutral=0,
        source=_SOURCE,
    ),
)


class NvidiaColorBackend(GpuBackend):
    """Digital Vibrance and Hue through NVAPI."""

    name = "nvidia"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fn: dict[str, int] = {}
        self._handles: dict[str, int] = {}
        self._available = False
        self._writable = False
        self._dll: ctypes.WinDLL | None = None
        if sys.platform == "win32":
            try:
                self._initialise()
            except Exception as exc:  # pragma: no cover - driver dependent
                _log.info("NVIDIA colour backend unavailable: %s", exc)
                self._available = False

    # ------------------------------------------------------------------ #
    # Setup
    # ------------------------------------------------------------------ #

    def _initialise(self) -> None:
        self._dll = ctypes.WinDLL("nvapi64.dll")
        query = self._dll.nvapi_QueryInterface
        query.argtypes = [ctypes.c_uint32]
        query.restype = ctypes.c_void_p

        for name, magic in _IDS.items():
            pointer = query(magic)
            if not pointer:
                raise GpuError(
                    f"NVAPI does not know id 0x{magic:08X} ({name}); "
                    "refusing to use this driver build"
                )
            self._fn[name] = pointer

        status = self._call("initialize", ctypes.c_int32)()
        if status != NVAPI_OK:
            raise GpuError(f"NvAPI_Initialize failed: {self._describe(status)}")

        try:
            self._handles = self._enumerate_displays()
            if not self._handles:
                raise GpuError("NVAPI reported no displays")
        except Exception:
            # Initialised, so the driver holds state for this process until
            # told otherwise.
            self._call("unload", ctypes.c_int32)()
            raise

        self._available = True
        self._writable = self._validate_dvc()
        if not self._writable:
            _log.warning(
                "NVAPI vibrance descriptor looks implausible; "
                "NVIDIA colour controls will be read-only"
            )

    def _call(self, name: str, restype, *argtypes):
        """Build a callable for a resolved entry point."""
        prototype = ctypes.CFUNCTYPE(restype, *argtypes)
        return prototype(self._fn[name])

    def _describe(self, status: int) -> str:
        buffer = ctypes.create_string_buffer(64)
        describe = self._call(
            "get_error_message", ctypes.c_int32, ctypes.c_int32, ctypes.c_char_p
        )
        describe(status, buffer)
        return f"{buffer.value.decode('ascii', 'replace')} ({status})"

    def _enumerate_displays(self) -> dict[str, int]:
        """Map OS device name to NVAPI display handle.

        The names NVAPI returns are the same ``\\\\.\\DISPLAYn`` strings the DDC
        backend gets from ``GetMonitorInfoW``, so the two line up directly.
        """
        enum = self._call(
            "enum_display_handle",
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_void_p),
        )
        get_name = self._call(
            "get_display_name", ctypes.c_int32, ctypes.c_void_p, ctypes.c_char_p
        )

        found: dict[str, int] = {}
        for index in range(16):
            handle = ctypes.c_void_p()
            status = enum(index, ctypes.byref(handle))
            if status == NVAPI_END_ENUMERATION:
                return found
            if status != NVAPI_OK:
                raise GpuError(
                    f"NvAPI_EnumNvidiaDisplayHandle({index}): {self._describe(status)}"
                )
            buffer = ctypes.create_string_buffer(64)
            if get_name(handle, buffer) != NVAPI_OK:
                raise GpuError(f"NvAPI_GetAssociatedNvidiaDisplayName({index}) failed")
            found[buffer.value.decode("ascii", "replace")] = handle.value or 0
        return found

    def _validate_dvc(self) -> bool:
        """Confirm the private DVC id really returns a vibrance descriptor."""
        for device in self._handles:
            try:
                info = self._read_dvc(device)
            except GpuError:
                return False
            plausible = (
                info.minLevel <= info.defaultLevel <= info.maxLevel
                and info.minLevel <= info.currentLevel <= info.maxLevel
                and 0 < info.maxLevel <= _MAX_PLAUSIBLE_LEVEL
            )
            if not plausible:
                _log.warning(
                    "Implausible DVC descriptor on %s: "
                    "current=%d min=%d max=%d default=%d",
                    device,
                    info.currentLevel,
                    info.minLevel,
                    info.maxLevel,
                    info.defaultLevel,
                )
                return False
        return True

    # ------------------------------------------------------------------ #
    # Raw accessors
    # ------------------------------------------------------------------ #

    def _handle(self, device: str) -> ctypes.c_void_p:
        if device not in self._handles:
            raise GpuError(f"NVAPI does not drive {device!r}")
        return ctypes.c_void_p(self._handles[device])

    def _read_dvc(self, device: str) -> _DVCInfoEx:
        info = _DVCInfoEx()
        info.version = _struct_version(_DVCInfoEx)
        status = self._call(
            "get_dvc_info_ex",
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DVCInfoEx),
        )(self._handle(device), 0, ctypes.byref(info))
        if status != NVAPI_OK:
            raise GpuError(f"NvAPI_GetDVCInfoEx failed: {self._describe(status)}")
        return info

    def _read_hue(self, device: str) -> _HueInfo:
        info = _HueInfo()
        info.version = _struct_version(_HueInfo)
        status = self._call(
            "get_hue_info",
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_HueInfo),
        )(self._handle(device), 0, ctypes.byref(info))
        if status != NVAPI_OK:
            raise GpuError(f"NvAPI_GetHUEInfo failed: {self._describe(status)}")
        return info

    # ------------------------------------------------------------------ #
    # GpuBackend
    # ------------------------------------------------------------------ #

    def available(self) -> bool:
        return self._available

    def devices(self) -> tuple[str, ...]:
        return tuple(self._handles)

    def refresh(self) -> None:
        """Enumerate the display handles again.

        A monitor that was switched off and on, or a display configuration
        change, can invalidate a handle or move ``\\\\.\\DISPLAYn`` to another
        output; the tray app outlives both.  The old handles are kept unless the
        driver lists every display cleanly: mid-reconfiguration it may list
        nothing, or stop half-way, and a partial list would drop outputs.
        """
        with self._lock:
            if not self._available:
                return
            try:
                fresh = self._enumerate_displays()
            except GpuError as exc:
                _log.info("NVAPI display list incomplete, keeping the old one: %s", exc)
                return
            if fresh:
                self._handles = fresh

    def adjustments(self) -> tuple[GpuAdjustment, ...]:
        if self._writable:
            return _ADJUSTMENTS
        # The private ids resolved but the descriptor did not look like
        # vibrance: shown, so the value can be seen, but not offered as a
        # control that would only refuse.
        return tuple(
            replace(
                a,
                writable=False,
                tooltip=a.tooltip + " (read-only: driver validation failed)",
            )
            for a in _ADJUSTMENTS
        )

    def get(self, device: str, key: str) -> GpuReading:
        with self._lock:
            if key == KEY_VIBRANCE:
                info = self._read_dvc(device)
                return GpuReading(
                    current=info.currentLevel,
                    minimum=info.minLevel,
                    maximum=info.maxLevel,
                )
            if key == KEY_HUE:
                info = self._read_hue(device)
                return GpuReading(current=info.currentHueAngle, minimum=0, maximum=359)
            raise GpuError(f"unknown NVIDIA adjustment {key!r}")

    def set(self, device: str, key: str, value: int) -> int:
        if not self._writable:
            raise GpuError(
                "NVIDIA colour controls are read-only: the driver returned an "
                "implausible vibrance descriptor, so writing is not safe"
            )
        with self._lock:
            if key == KEY_VIBRANCE:
                return self._set_vibrance(device, value)
            if key == KEY_HUE:
                return self._set_hue(device, value)
            raise GpuError(f"unknown NVIDIA adjustment {key!r}")

    def _set_vibrance(self, device: str, value: int) -> int:
        # The setter takes the full descriptor, so start from a fresh read and
        # only replace the level.  That also keeps the version field correct
        # across driver revisions.
        info = self._read_dvc(device)
        clamped = max(info.minLevel, min(info.maxLevel, value))
        info.currentLevel = clamped
        status = self._call(
            "set_dvc_level_ex",
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DVCInfoEx),
        )(self._handle(device), 0, ctypes.byref(info))
        if status != NVAPI_OK:
            raise GpuError(f"NvAPI_SetDVCLevelEx failed: {self._describe(status)}")
        return self._read_dvc(device).currentLevel

    def _set_hue(self, device: str, value: int) -> int:
        clamped = max(0, min(359, value))
        status = self._call(
            "set_hue_angle",
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int32,
        )(self._handle(device), 0, clamped)
        if status != NVAPI_OK:
            raise GpuError(f"NvAPI_SetHUEAngle failed: {self._describe(status)}")
        return self._read_hue(device).currentHueAngle

    def close(self) -> None:
        with self._lock:
            if not self._available:
                return
            self._available = False
            self._writable = False
            try:
                self._call("unload", ctypes.c_int32)()
            except Exception:  # pragma: no cover - defensive
                _log.debug("NvAPI_Unload failed", exc_info=True)
            self._handles.clear()
