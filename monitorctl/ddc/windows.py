"""Windows backend built on the Monitor Configuration API (``dxva2.dll``).

Windows does not expose raw I2C to user mode.  DDC/CI goes through the WDDM
display driver via ``dxva2.dll``, which validates packet framing for us and, more
usefully, reports failures through a dedicated set of ``ERROR_GRAPHICS_DDCCI_*``
codes that :mod:`monitorctl.ddc.errors` decodes.

Two deliberate choices, both learned from reading other implementations:

* Every imported function declares ``argtypes`` and ``restype``.  Without them
  ctypes marshals pointer-sized values as 32-bit ``int``, which silently
  truncates handles on x64 and produces failures that look like hardware faults.
* Writes use the low-level ``SetVCPFeature`` rather than the high-level
  ``SetMonitorBrightness``.  The latter is documented by the Monitorian project
  to return success even when the write did not happen.

Each monitor's EDID is read from the registry key of the PnP device that is
attached *right now* to its ``\\\\.\\DISPLAYn``, found through
``EnumDisplayDevicesW``.  Scanning ``Enum\\DISPLAY`` directly would be simpler
and wrong: it keeps an entry for every port a monitor was ever plugged into.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes

if sys.platform == "win32":
    import winreg

from .backend import DdcBackend, PhysicalMonitor, VcpReading
from .edid import EdidInfo, parse_edid
from .errors import (
    DdcBackendError,
    DdcUnsupportedFeature,
    classify_windows_error,
    raise_for_status,
)

__all__ = ["WindowsDdcBackend", "is_supported"]

_log = logging.getLogger(__name__)

_CCHDEVICENAME = 32
_MONITORINFOF_PRIMARY = 0x00000001
_EDD_GET_DEVICE_INTERFACE_NAME = 0x00000001
_DISPLAY_DEVICE_ACTIVE = 0x00000001

#: Bounds the EnumDisplayDevicesW loop.  A display adapter output drives one
#: monitor, a few in clone mode; this is only a guard against a driver that
#: never reports the end of the list.
_MAX_MONITORS_PER_OUTPUT = 16

#: Device interface names look like
#: ``\\?\DISPLAY#TCL1003#5&2a0761ac&0&UID4353#{e6f07b5f-...}``.
_INTERFACE_PREFIX = "\\\\?\\"
_ENUM_KEY = r"SYSTEM\CurrentControlSet\Enum"

#: Real capabilities strings are a few hundred bytes.  This bounds the
#: allocation in case a malfunctioning monitor reports an absurd length.
_MAX_CAPABILITIES_BYTES = 65536


def is_supported() -> bool:
    """Whether this backend can run on the current platform."""
    return sys.platform == "win32"


class _PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
    ]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * _CCHDEVICENAME),
    ]


class _DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HMONITOR,
    wintypes.HDC,
    ctypes.POINTER(wintypes.RECT),
    wintypes.LPARAM,
)


class _Api:
    """Lazily bound Win32 entry points with explicit signatures."""

    _instance: _Api | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        if not is_supported():
            raise DdcBackendError(
                f"Windows backend unavailable on platform {sys.platform!r}"
            )
        # use_last_error keeps GetLastError isolated from unrelated ctypes calls.
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)

        self.user32.EnumDisplayMonitors.argtypes = [
            wintypes.HDC,
            ctypes.POINTER(wintypes.RECT),
            _MONITORENUMPROC,
            wintypes.LPARAM,
        ]
        self.user32.EnumDisplayMonitors.restype = wintypes.BOOL

        self.user32.GetMonitorInfoW.argtypes = [
            wintypes.HMONITOR,
            ctypes.POINTER(_MONITORINFOEXW),
        ]
        self.user32.GetMonitorInfoW.restype = wintypes.BOOL

        self.user32.EnumDisplayDevicesW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(_DISPLAY_DEVICEW),
            wintypes.DWORD,
        ]
        self.user32.EnumDisplayDevicesW.restype = wintypes.BOOL

        self.dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
            wintypes.HMONITOR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL

        self.dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [
            wintypes.HMONITOR,
            wintypes.DWORD,
            ctypes.POINTER(_PHYSICAL_MONITOR),
        ]
        self.dxva2.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL

        self.dxva2.DestroyPhysicalMonitor.argtypes = [wintypes.HANDLE]
        self.dxva2.DestroyPhysicalMonitor.restype = wintypes.BOOL

        self.dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [
            wintypes.HANDLE,
            ctypes.c_ubyte,  # Win32 BYTE is unsigned; be explicit
            ctypes.POINTER(wintypes.DWORD),  # MC_VCP_CODE_TYPE, unused here
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.dxva2.GetVCPFeatureAndVCPFeatureReply.restype = wintypes.BOOL

        self.dxva2.SetVCPFeature.argtypes = [
            wintypes.HANDLE,
            ctypes.c_ubyte,  # Win32 BYTE is unsigned; be explicit
            wintypes.DWORD,
        ]
        self.dxva2.SetVCPFeature.restype = wintypes.BOOL

        self.dxva2.GetCapabilitiesStringLength.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.dxva2.GetCapabilitiesStringLength.restype = wintypes.BOOL

        self.dxva2.CapabilitiesRequestAndCapabilitiesReply.argtypes = [
            wintypes.HANDLE,
            ctypes.c_char_p,
            wintypes.DWORD,
        ]
        self.dxva2.CapabilitiesRequestAndCapabilitiesReply.restype = wintypes.BOOL

    @classmethod
    def get(cls) -> _Api:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance


class _Handle:
    """Owns one physical monitor handle and guarantees a single destroy.

    Deliberately not released from ``__del__``: interpreter shutdown ordering
    makes ctypes calls from finalisers unreliable, and a leaked handle is a far
    milder failure than a crash in a destructor.
    """

    __slots__ = ("_closed", "_lock", "_raw")

    def __init__(self, raw: int) -> None:
        self._raw = raw
        self._closed = False
        self._lock = threading.Lock()

    @property
    def raw(self) -> int:
        if self._closed:
            raise DdcBackendError("monitor handle has already been released")
        return self._raw

    def close(self, api: _Api) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if not api.dxva2.DestroyPhysicalMonitor(wintypes.HANDLE(self._raw)):
                _, reason = classify_windows_error(ctypes.get_last_error())
                _log.warning("DestroyPhysicalMonitor failed: %s", reason)


def _instance_path(interface: str) -> str | None:
    r"""Turn a device interface name into its PnP instance path.

    ``\\?\DISPLAY#TCL1003#5&2a&0&UID4353#{guid}`` becomes
    ``DISPLAY\TCL1003\5&2a&0&UID4353``.
    """
    if not interface.startswith(_INTERFACE_PREFIX):
        return None
    parts = interface[len(_INTERFACE_PREFIX) :].split("#")
    if len(parts) != 4 or not all(parts[:3]):
        return None
    # The pieces become a registry path; a separator inside one would walk out
    # of the device's own key.
    if any("\\" in part or "/" in part for part in parts[:3]):
        return None
    return "\\".join(parts[:3])


def _read_edid(interface: str) -> EdidInfo | None:
    """Read and parse the EDID the driver stored for one device interface."""
    instance = _instance_path(interface)
    if instance is None:
        _log.debug("unrecognised display interface name: %r", interface)
        return None
    key_path = rf"{_ENUM_KEY}\{instance}\Device Parameters"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            raw, kind = winreg.QueryValueEx(key, "EDID")
    except OSError as exc:
        _log.debug("no EDID for %s: %s", instance, exc)
        return None
    if kind != winreg.REG_BINARY or not isinstance(raw, bytes):
        return None
    edid = parse_edid(raw)
    if edid is None:
        _log.info("ignoring an invalid EDID block for %s", instance)
    return edid


def _fail(operation: str) -> None:
    """Classify the pending Win32 error and raise the matching exception."""
    status, reason = classify_windows_error(ctypes.get_last_error())
    raise_for_status(status, f"{operation}: {reason}")
    # raise_for_status only returns for OK, which means the call reported
    # failure without setting an error code.  Treat that as a hard failure.
    raise DdcBackendError(f"{operation}: failed without setting an error code")


class WindowsDdcBackend(DdcBackend):
    """DDC/CI over the Windows Monitor Configuration API."""

    def __init__(self) -> None:
        self._api = _Api.get()
        self._handles: list[_Handle] = []

    def enumerate(self) -> list[PhysicalMonitor]:
        self.close()
        monitors: list[PhysicalMonitor] = []
        for index, (hmonitor, device, primary) in enumerate(self._enum_hmonitors()):
            monitors.extend(
                self._physical_from_hmonitor(hmonitor, device, primary, index)
            )
        return monitors

    def _enum_hmonitors(self) -> list[tuple[int, str, bool]]:
        found: list[tuple[int, str, bool]] = []

        def callback(hmonitor, hdc, lprect, lparam):
            del hdc, lprect, lparam
            info = _MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
            device = ""
            primary = False
            if self._api.user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                device = info.szDevice
                primary = bool(info.dwFlags & _MONITORINFOF_PRIMARY)
            else:
                _log.debug("GetMonitorInfoW failed for HMONITOR %s", hmonitor)
            found.append((int(hmonitor), device, primary))
            return True

        # The thunk must outlive the call; keeping it in a local is sufficient
        # because EnumDisplayMonitors is synchronous.
        thunk = _MONITORENUMPROC(callback)
        if not self._api.user32.EnumDisplayMonitors(None, None, thunk, 0):
            _fail("EnumDisplayMonitors")
        return found

    def _physical_from_hmonitor(
        self, hmonitor: int, device: str, primary: bool, hmonitor_index: int
    ) -> list[PhysicalMonitor]:
        count = wintypes.DWORD()
        if not self._api.dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
            wintypes.HMONITOR(hmonitor), ctypes.byref(count)
        ):
            # A display without DDC/CI support is a normal condition, not a bug.
            _, reason = classify_windows_error(ctypes.get_last_error())
            _log.info("No physical monitors for %s: %s", device or hmonitor, reason)
            return []

        if count.value == 0:
            return []

        array = (_PHYSICAL_MONITOR * count.value)()
        if not self._api.dxva2.GetPhysicalMonitorsFromHMONITOR(
            wintypes.HMONITOR(hmonitor), count.value, array
        ):
            _, reason = classify_windows_error(ctypes.get_last_error())
            _log.warning("GetPhysicalMonitorsFromHMONITOR failed: %s", reason)
            return []

        edids = self._edids_for(device) if device else []
        result: list[PhysicalMonitor] = []
        for offset, entry in enumerate(array):
            # The value in hPhysicalMonitor is not a pointer: on current Windows
            # builds it is a small index, and the first monitor legitimately
            # gets the value 0.  ctypes surfaces a NULL c_void_p as None, so it
            # must be normalised back to 0 rather than treated as a failure.
            # Verified on this machine: handle 0 reads VCP 0x10 successfully.
            raw = entry.hPhysicalMonitor
            handle = _Handle(0 if raw is None else int(raw))
            self._handles.append(handle)
            result.append(
                PhysicalMonitor(
                    token=handle,
                    # Unique across outputs: no output has more monitors.
                    index=hmonitor_index * _MAX_MONITORS_PER_OUTPUT + offset,
                    description=entry.szPhysicalMonitorDescription,
                    device=device,
                    primary=primary,
                    # dxva2 and EnumDisplayDevicesW list the monitors of one
                    # output in the same order.  When the counts disagree the
                    # pairing is unknowable, so nobody gets an identity rather
                    # than someone getting the wrong one.
                    edid=edids[offset] if len(edids) == count.value else None,
                )
            )
        return result

    def _edids_for(self, device: str) -> list[EdidInfo | None]:
        """The EDID of each active monitor on one display output, in order."""
        found: list[EdidInfo | None] = []
        for number in range(_MAX_MONITORS_PER_OUTPUT):
            info = _DISPLAY_DEVICEW()
            info.cb = ctypes.sizeof(_DISPLAY_DEVICEW)
            if not self._api.user32.EnumDisplayDevicesW(
                device, number, ctypes.byref(info), _EDD_GET_DEVICE_INTERFACE_NAME
            ):
                break
            if info.StateFlags & _DISPLAY_DEVICE_ACTIVE:
                found.append(_read_edid(info.DeviceID))
        return found

    @staticmethod
    def _handle_of(monitor: PhysicalMonitor) -> _Handle:
        token = monitor.token
        if not isinstance(token, _Handle):
            raise DdcBackendError("monitor does not belong to this backend")
        return token

    def get_vcp(self, monitor: PhysicalMonitor, code: int) -> VcpReading:
        if not 0 <= code <= 0xFF:
            raise ValueError(f"VCP code out of range: {code}")
        handle = self._handle_of(monitor)
        code_type = wintypes.DWORD()
        current = wintypes.DWORD()
        maximum = wintypes.DWORD()
        ctypes.set_last_error(0)
        ok = self._api.dxva2.GetVCPFeatureAndVCPFeatureReply(
            wintypes.HANDLE(handle.raw),
            ctypes.c_ubyte(code),
            ctypes.byref(code_type),
            ctypes.byref(current),
            ctypes.byref(maximum),
        )
        if not ok:
            _fail(f"GetVCPFeatureAndVCPFeatureReply(0x{code:02X})")

        # Some firmwares answer with an all-zero packet instead of a proper
        # "unsupported" result code.  A maximum of zero is never meaningful for
        # a real feature, so treat it as unsupported rather than reporting a
        # slider with a zero-width range.
        if maximum.value == 0:
            raise DdcUnsupportedFeature(
                f"VCP 0x{code:02X}: monitor replied with a maximum of zero "
                "(most likely unsupported)"
            )
        return VcpReading(current=int(current.value), maximum=int(maximum.value))

    def set_vcp(self, monitor: PhysicalMonitor, code: int, value: int) -> None:
        if not 0 <= code <= 0xFF:
            raise ValueError(f"VCP code out of range: {code}")
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"VCP value outside the 16-bit range: {value}")
        handle = self._handle_of(monitor)
        ctypes.set_last_error(0)
        ok = self._api.dxva2.SetVCPFeature(
            wintypes.HANDLE(handle.raw),
            ctypes.c_ubyte(code),
            wintypes.DWORD(value),
        )
        if not ok:
            _fail(f"SetVCPFeature(0x{code:02X}, {value})")

    def read_capabilities(self, monitor: PhysicalMonitor) -> str:
        handle = self._handle_of(monitor)
        length = wintypes.DWORD()
        ctypes.set_last_error(0)
        # Failures raise like every other operation, so that a garbled
        # transfer - this is the longest one - gets its retry, and the caller
        # can say why there is no string instead of only that there is none.
        if not self._api.dxva2.GetCapabilitiesStringLength(
            wintypes.HANDLE(handle.raw), ctypes.byref(length)
        ):
            _fail("GetCapabilitiesStringLength")

        size = int(length.value)
        if size <= 0:
            return ""
        if size > _MAX_CAPABILITIES_BYTES:
            # A buffer shorter than the reported length would only make the
            # request fail; a monitor reporting this much is malfunctioning.
            raise DdcBackendError(
                f"monitor reported a {size}-byte capabilities string "
                f"(limit {_MAX_CAPABILITIES_BYTES}); not requested"
            )

        buffer = ctypes.create_string_buffer(size)
        ctypes.set_last_error(0)
        if not self._api.dxva2.CapabilitiesRequestAndCapabilitiesReply(
            wintypes.HANDLE(handle.raw), buffer, wintypes.DWORD(size)
        ):
            _fail("CapabilitiesRequestAndCapabilitiesReply")

        return buffer.value.decode("ascii", errors="replace")

    def close(self) -> None:
        for handle in self._handles:
            handle.close(self._api)
        self._handles.clear()
