"""ctypes bindings for the tray application: icon, hidden window, notifications.

Pure Win32 rather than a GUI toolkit, so the resident part of the app costs no
more than the interpreter: Tk is only loaded when the window is first opened.

Every imported function declares ``argtypes`` and ``restype``, for the reason
given in ``ddc/windows.py``: without them ctypes passes 64-bit handles and
``LPARAM`` values as 32-bit ints and truncates them.

The hidden window is a real top-level window that is never shown, not a
message-only window (``HWND_MESSAGE``).  Message-only windows do not receive
broadcasts, and two of the messages this app lives on are broadcasts:
``WM_DISPLAYCHANGE`` and the shell's ``TaskbarCreated``.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "GUID_CONSOLE_DISPLAY_STATE",
    "GUID_DEVINTERFACE_MONITOR",
    "MenuItem",
    "MessageWindow",
    "SingleInstance",
    "TrayIcon",
    "enable_dark_title_bar",
    "make_dpi_aware",
    "pump_messages",
    "trim_working_set",
]

_log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_CONTEXTMENU = 0x007B
WM_DISPLAYCHANGE = 0x007E
WM_POWERBROADCAST = 0x0218
WM_DEVICECHANGE = 0x0219
WM_WTSSESSION_CHANGE = 0x02B1
WM_LBUTTONDBLCLK = 0x0203
WM_USER = 0x0400
WM_APP = 0x8000

NIN_SELECT = WM_USER + 0
NIN_KEYSELECT = WM_USER + 1

PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_POWERSETTINGCHANGE = 0x8013

DBT_DEVICEARRIVAL = 0x8000
DBT_DEVTYP_DEVICEINTERFACE = 0x00000005

WTS_SESSION_UNLOCK = 0x8

_NIM_ADD = 0x0
_NIM_MODIFY = 0x1
_NIM_DELETE = 0x2
_NIM_SETVERSION = 0x4
_NIF_MESSAGE = 0x01
_NIF_ICON = 0x02
_NIF_TIP = 0x04
_NIF_SHOWTIP = 0x80
_NOTIFYICON_VERSION_4 = 4

_MF_STRING = 0x0000
_MF_GRAYED = 0x0001
_MF_CHECKED = 0x0008
_MF_SEPARATOR = 0x0800
_TPM_RIGHTBUTTON = 0x0002
_TPM_NONOTIFY = 0x0080
_TPM_RETURNCMD = 0x0100

_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x0010
_SM_CXSMICON = 49
_SM_CYSMICON = 50

_ERROR_ALREADY_EXISTS = 183
_DEVICE_NOTIFY_WINDOW_HANDLE = 0
_NOTIFY_FOR_THIS_SESSION = 0

_DWMWA_USE_IMMERSIVE_DARK_MODE = 20

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


# --------------------------------------------------------------------------- #
# Structures
# --------------------------------------------------------------------------- #


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> GUID:
        """Build a GUID from ``"6FE69556-704A-47A0-8F24-C28D936FDA47"``."""
        parts = text.strip("{}").split("-")
        tail = bytes.fromhex(parts[3] + parts[4])
        return cls(
            int(parts[0], 16),
            int(parts[1], 16),
            int(parts[2], 16),
            (ctypes.c_ubyte * 8)(*tail),
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, GUID) and bytes(self) == bytes(other)

    __hash__ = None  # type: ignore[assignment]


#: Display on / off / dimmed, as Windows' power manager sees it.
GUID_CONSOLE_DISPLAY_STATE = GUID.parse("6FE69556-704A-47A0-8F24-C28D936FDA47")
#: Device interface class of every monitor PnP device.
GUID_DEVINTERFACE_MONITOR = GUID.parse("E6F07B5F-EE97-4A90-B076-33F57BF4EAA7")


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class DEV_BROADCAST_HDR(ctypes.Structure):
    _fields_ = [
        ("dbch_size", wintypes.DWORD),
        ("dbch_devicetype", wintypes.DWORD),
        ("dbch_reserved", wintypes.DWORD),
    ]


class DEV_BROADCAST_DEVICEINTERFACE_W(ctypes.Structure):
    _fields_ = [
        ("dbcc_size", wintypes.DWORD),
        ("dbcc_devicetype", wintypes.DWORD),
        ("dbcc_reserved", wintypes.DWORD),
        ("dbcc_classguid", GUID),
        ("dbcc_name", wintypes.WCHAR * 1),
    ]


class POWERBROADCAST_SETTING(ctypes.Structure):
    _fields_ = [
        ("PowerSetting", GUID),
        ("DataLength", wintypes.DWORD),
        ("Data", wintypes.DWORD),
    ]


# --------------------------------------------------------------------------- #
# Function prototypes
# --------------------------------------------------------------------------- #


def _bind() -> tuple[ctypes.WinDLL, ...]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    def proto(fn, restype, *argtypes):
        fn.restype = restype
        fn.argtypes = list(argtypes)

    H, U, W, L = wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    proto(user32.RegisterClassExW, wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
    proto(user32.UnregisterClassW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
    proto(
        user32.CreateWindowExW,
        H,
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        H,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    )
    proto(user32.DestroyWindow, wintypes.BOOL, H)
    proto(user32.DefWindowProcW, LRESULT, H, U, W, L)
    proto(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), H, U, U)
    proto(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
    proto(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
    proto(user32.PostMessageW, wintypes.BOOL, H, U, W, L)
    proto(user32.PostQuitMessage, None, ctypes.c_int)
    proto(user32.RegisterWindowMessageW, U, wintypes.LPCWSTR)
    proto(user32.FindWindowW, H, wintypes.LPCWSTR, wintypes.LPCWSTR)
    proto(user32.SetForegroundWindow, wintypes.BOOL, H)
    proto(user32.CreatePopupMenu, wintypes.HMENU)
    proto(
        user32.AppendMenuW,
        wintypes.BOOL,
        wintypes.HMENU,
        U,
        ctypes.c_size_t,
        wintypes.LPCWSTR,
    )
    proto(user32.SetMenuDefaultItem, wintypes.BOOL, wintypes.HMENU, U, U)
    proto(
        user32.TrackPopupMenuEx,
        wintypes.BOOL,
        wintypes.HMENU,
        U,
        ctypes.c_int,
        ctypes.c_int,
        H,
        wintypes.LPVOID,
    )
    proto(user32.DestroyMenu, wintypes.BOOL, wintypes.HMENU)
    proto(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
    proto(
        user32.LoadImageW,
        wintypes.HANDLE,
        wintypes.HINSTANCE,
        wintypes.LPCWSTR,
        U,
        ctypes.c_int,
        ctypes.c_int,
        U,
    )
    proto(user32.DestroyIcon, wintypes.BOOL, wintypes.HICON)
    proto(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
    proto(
        user32.RegisterDeviceNotificationW,
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    proto(user32.UnregisterDeviceNotification, wintypes.BOOL, wintypes.HANDLE)
    proto(
        user32.RegisterPowerSettingNotification,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(GUID),
        wintypes.DWORD,
    )
    proto(user32.UnregisterPowerSettingNotification, wintypes.BOOL, wintypes.HANDLE)
    proto(
        shell32.Shell_NotifyIconW,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.POINTER(NOTIFYICONDATAW),
    )
    proto(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
    proto(
        kernel32.CreateMutexW,
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    proto(kernel32.CloseHandle, wintypes.BOOL, wintypes.HANDLE)
    proto(kernel32.GetCurrentProcess, wintypes.HANDLE)
    proto(kernel32.K32EmptyWorkingSet, wintypes.BOOL, wintypes.HANDLE)
    return user32, kernel32, shell32


if sys.platform == "win32":
    _user32, _kernel32, _shell32 = _bind()


def _last_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


# --------------------------------------------------------------------------- #
# Process-wide helpers
# --------------------------------------------------------------------------- #


def make_dpi_aware() -> None:
    """Opt in to system DPI awareness, before any window exists.

    Without it Windows bitmap-stretches the window on a scaled display and
    everything is blurry.  *System* rather than per-monitor awareness because
    Tk 8.6 does not rescale when a window moves between monitors; per-monitor
    awareness would leave it the wrong size on the second screen instead of
    merely soft.
    """
    try:
        # DPI_AWARENESS_CONTEXT_SYSTEM_AWARE
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-2))
    except (AttributeError, OSError):  # pragma: no cover - pre Windows 10 1703
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


def trim_working_set() -> None:
    """Hand memory the process is not using back to Windows.

    Called after the window is hidden.  It changes nothing about what the
    process *can* use - pages come back on demand - but an idle tray app that
    keeps the window's pages resident shows a working set several times its
    real need in Task Manager, and that is the number people judge it by.
    """
    _kernel32.K32EmptyWorkingSet(_kernel32.GetCurrentProcess())


def enable_dark_title_bar(hwnd: int, enabled: bool) -> None:
    """Ask DWM for a dark (or light) title bar.  No-op where unsupported."""
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        value = ctypes.c_int(1 if enabled else 0)
        dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(_DWMWA_USE_IMMERSIVE_DARK_MODE),
            ctypes.byref(value),
            wintypes.DWORD(ctypes.sizeof(value)),
        )
    except OSError:  # pragma: no cover - pre Windows 10 20H1
        pass


def pump_messages() -> int:
    """Run the thread's message loop until ``WM_QUIT``; returns its exit code."""
    msg = wintypes.MSG()
    while True:
        result = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if result == 0:
            return int(msg.wParam)
        if result == -1:  # pragma: no cover - invalid arguments only
            raise _last_error()
        _user32.TranslateMessage(ctypes.byref(msg))
        _user32.DispatchMessageW(ctypes.byref(msg))


def post_quit() -> None:
    _user32.PostQuitMessage(0)


# --------------------------------------------------------------------------- #
# Single instance
# --------------------------------------------------------------------------- #


class SingleInstance:
    """A named mutex held for the life of the process.

    ``Local\\`` scopes it to the logon session, so two users on one machine
    each get their own tray app, and a second launch in the same session finds
    the first instead of fighting it for the display bus.
    """

    def __init__(self, name: str) -> None:
        self._handle = _kernel32.CreateMutexW(None, False, f"Local\\{name}")
        error = ctypes.get_last_error()
        if not self._handle:
            raise _last_error()
        self.already_running = error == _ERROR_ALREADY_EXISTS

    def release(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None


def find_window(class_name: str) -> int | None:
    hwnd = _user32.FindWindowW(class_name, None)
    return int(hwnd) if hwnd else None


def post_message(hwnd: int, message: int, wparam: int = 0, lparam: int = 0) -> bool:
    """Thread-safe: queue a message for ``hwnd`` without waiting for it."""
    return bool(_user32.PostMessageW(hwnd, message, wparam, lparam))


# --------------------------------------------------------------------------- #
# Hidden window
# --------------------------------------------------------------------------- #

Handler = Callable[[int, int], int | None]


class MessageWindow:
    """An invisible top-level window whose messages go to Python handlers.

    A handler returns an ``int`` to answer the message itself, or ``None`` to
    let ``DefWindowProcW`` do so.  An exception in a handler is logged and the
    message falls through: an exception escaping a ctypes callback would be
    printed to a console that does not exist and the message answered with 0.
    """

    def __init__(self, class_name: str) -> None:
        self._handlers: dict[int, Handler] = {}
        self._class_name = class_name
        # The thunk must live as long as the window class; keep it on self.
        self._wndproc = WNDPROC(self._dispatch)
        self._hinstance = _kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = self._hinstance
        wc.lpszClassName = class_name
        if not _user32.RegisterClassExW(ctypes.byref(wc)):
            raise _last_error()
        hwnd = _user32.CreateWindowExW(
            0, class_name, "monitorctl", 0, 0, 0, 0, 0, None, None, self._hinstance, None
        )
        if not hwnd:
            error = _last_error()
            _user32.UnregisterClassW(class_name, self._hinstance)
            raise error
        self.hwnd = int(hwnd)
        self._notifications: list[tuple[str, int]] = []

    def on(self, message: int, handler: Handler) -> None:
        self._handlers[message] = handler

    def register_message(self, name: str) -> int:
        """A system-wide message id for ``name``, e.g. ``"TaskbarCreated"``."""
        return int(_user32.RegisterWindowMessageW(name))

    def _dispatch(self, hwnd, message, wparam, lparam):
        handler = self._handlers.get(message)
        if handler is not None:
            try:
                result = handler(int(wparam or 0), int(lparam or 0))
            except Exception:
                _log.exception("handler for message 0x%04X failed", message)
                result = None
            if result is not None:
                return result
        return _user32.DefWindowProcW(hwnd, message, wparam, lparam)

    # Notifications ------------------------------------------------------- #

    def watch_monitors(self) -> None:
        """Receive ``WM_DEVICECHANGE`` when a monitor device arrives."""
        flt = DEV_BROADCAST_DEVICEINTERFACE_W()
        flt.dbcc_size = ctypes.sizeof(DEV_BROADCAST_DEVICEINTERFACE_W)
        flt.dbcc_devicetype = DBT_DEVTYP_DEVICEINTERFACE
        flt.dbcc_classguid = GUID_DEVINTERFACE_MONITOR
        handle = _user32.RegisterDeviceNotificationW(
            self.hwnd, ctypes.byref(flt), _DEVICE_NOTIFY_WINDOW_HANDLE
        )
        if handle:
            self._notifications.append(("device", int(handle)))
        else:
            _log.warning("monitor arrival notifications unavailable: %s", _last_error())

    def watch_display_power(self) -> None:
        """Receive ``WM_POWERBROADCAST`` when Windows turns displays on or off."""
        guid = GUID_CONSOLE_DISPLAY_STATE
        handle = _user32.RegisterPowerSettingNotification(
            self.hwnd, ctypes.byref(guid), _DEVICE_NOTIFY_WINDOW_HANDLE
        )
        if handle:
            self._notifications.append(("power", int(handle)))
        else:
            _log.warning("display power notifications unavailable: %s", _last_error())

    def watch_session(self) -> None:
        """Receive ``WM_WTSSESSION_CHANGE``, for unlock."""
        try:
            wtsapi = ctypes.WinDLL("wtsapi32", use_last_error=True)
            wtsapi.WTSRegisterSessionNotification.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
            ]
            wtsapi.WTSRegisterSessionNotification.restype = wintypes.BOOL
            if wtsapi.WTSRegisterSessionNotification(self.hwnd, _NOTIFY_FOR_THIS_SESSION):
                self._notifications.append(("session", 0))
                return
        except OSError:  # pragma: no cover
            pass
        _log.warning("session notifications unavailable")

    def destroy(self) -> None:
        for kind, handle in self._notifications:
            try:
                if kind == "device":
                    _user32.UnregisterDeviceNotification(handle)
                elif kind == "power":
                    _user32.UnregisterPowerSettingNotification(handle)
                else:
                    wtsapi = ctypes.WinDLL("wtsapi32")
                    wtsapi.WTSUnRegisterSessionNotification(wintypes.HWND(self.hwnd))
            except OSError:  # pragma: no cover - best effort at shutdown
                pass
        self._notifications.clear()
        _user32.DestroyWindow(self.hwnd)
        _user32.UnregisterClassW(self._class_name, self._hinstance)


def is_monitor_arrival(wparam: int, lparam: int) -> bool:
    """Whether a ``WM_DEVICECHANGE`` reports a monitor device arriving."""
    if wparam != DBT_DEVICEARRIVAL or not lparam:
        return False
    header = DEV_BROADCAST_HDR.from_address(lparam)
    if header.dbch_devicetype != DBT_DEVTYP_DEVICEINTERFACE:
        return False
    iface = DEV_BROADCAST_DEVICEINTERFACE_W.from_address(lparam)
    return iface.dbcc_classguid == GUID_DEVINTERFACE_MONITOR


def display_power_state(lparam: int) -> int | None:
    """For ``PBT_POWERSETTINGCHANGE``: 0 off, 1 on, 2 dimmed; else ``None``."""
    if not lparam:
        return None
    setting = POWERBROADCAST_SETTING.from_address(lparam)
    if setting.PowerSetting != GUID_CONSOLE_DISPLAY_STATE or setting.DataLength < 4:
        return None
    return int(setting.Data)


# --------------------------------------------------------------------------- #
# Tray icon
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MenuItem:
    """One entry of the tray menu.  ``label=None`` is a separator."""

    label: str | None
    command: int = 0
    checked: bool = False
    enabled: bool = True
    default: bool = False


SEPARATOR = MenuItem(None)


class TrayIcon:
    """The notification-area icon, with version-4 callback semantics.

    Under ``NOTIFYICON_VERSION_4`` the callback message carries the event in
    ``LOWORD(lParam)`` and the anchor point in ``wParam``; selection by mouse
    and by keyboard both arrive as ``NIN_SELECT``/``NIN_KEYSELECT``, which is
    what makes the icon usable without a mouse.
    """

    ICON_ID = 1

    def __init__(
        self, window: MessageWindow, callback_message: int, icon_path: Path
    ) -> None:
        self._window = window
        self._callback = callback_message
        self._icon = _user32.LoadImageW(
            None,
            str(icon_path),
            _IMAGE_ICON,
            _user32.GetSystemMetrics(_SM_CXSMICON),
            _user32.GetSystemMetrics(_SM_CYSMICON),
            _LR_LOADFROMFILE,
        )
        if not self._icon:
            _log.warning("could not load %s: %s", icon_path, _last_error())
        self._tip = "monitorctl"
        self._added = False

    def _data(self, flags: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._window.hwnd
        data.uID = self.ICON_ID
        data.uFlags = flags
        data.uCallbackMessage = self._callback
        data.hIcon = self._icon
        data.szTip = self._tip[:127]
        return data

    def show(self) -> bool:
        """Add the icon.  Safe to call again after Explorer restarts."""
        data = self._data(_NIF_MESSAGE | _NIF_ICON | _NIF_TIP | _NIF_SHOWTIP)
        if not _shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(data)):
            # Already present (a duplicate TaskbarCreated): just refresh it.
            _shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data))
        data.uVersion = _NOTIFYICON_VERSION_4
        _shell32.Shell_NotifyIconW(_NIM_SETVERSION, ctypes.byref(data))
        self._added = True
        return True

    def set_tooltip(self, text: str) -> None:
        self._tip = text
        if self._added:
            data = self._data(_NIF_TIP | _NIF_SHOWTIP)
            _shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data))

    def remove(self) -> None:
        if self._added:
            data = self._data(0)
            _shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))
            self._added = False
        if self._icon:
            _user32.DestroyIcon(self._icon)
            self._icon = None

    def popup(
        self, items: list[MenuItem], x: int | None = None, y: int | None = None
    ) -> int:
        """Show a context menu and return the chosen command, or 0.

        The ``SetForegroundWindow`` / ``WM_NULL`` dance is the documented fix
        for a tray menu that does not close when the user clicks elsewhere.
        """
        if x is None or y is None:
            point = wintypes.POINT()
            _user32.GetCursorPos(ctypes.byref(point))
            x, y = point.x, point.y
        menu = _user32.CreatePopupMenu()
        try:
            for item in items:
                if item.label is None:
                    _user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
                    continue
                flags = _MF_STRING
                if item.checked:
                    flags |= _MF_CHECKED
                if not item.enabled:
                    flags |= _MF_GRAYED
                _user32.AppendMenuW(menu, flags, item.command, item.label)
                if item.default:
                    _user32.SetMenuDefaultItem(menu, item.command, False)
            _user32.SetForegroundWindow(self._window.hwnd)
            command = _user32.TrackPopupMenuEx(
                menu,
                _TPM_RIGHTBUTTON | _TPM_RETURNCMD | _TPM_NONOTIFY,
                x,
                y,
                self._window.hwnd,
                None,
            )
            _user32.PostMessageW(self._window.hwnd, WM_NULL, 0, 0)
            return int(command)
        finally:
            _user32.DestroyMenu(menu)


def loword(value: int) -> int:
    return value & 0xFFFF


def signed_xy(wparam: int) -> tuple[int, int]:
    """``GET_X_LPARAM``/``GET_Y_LPARAM``: signed, for monitors left of primary."""
    x = wparam & 0xFFFF
    y = (wparam >> 16) & 0xFFFF
    return (x - 0x10000 if x & 0x8000 else x, y - 0x10000 if y & 0x8000 else y)
