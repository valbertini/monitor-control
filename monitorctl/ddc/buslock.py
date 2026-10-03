"""A lock on the display bus shared by every monitorctl process in the session.

:class:`~monitorctl.ddc.service.MonitorService` serialises DDC/CI traffic with a
lock, but a lock lives inside one process.  The tray app runs all day, and the
CLI is meant for poking at a monitor while it does; two processes talking to
the same monitor at once interleave I²C transactions, and a scrambled reply is
at best a failed read and at worst a write the monitor misreads.

A named Windows mutex closes that gap.  It is taken around each individual
operation - one read, one write - never around a batch, so the tray app's
minute-long probe does not lock the CLI out: the two simply take turns, one
transaction at a time.

``Local\\`` scopes the name to the logon session, which is the scope of the
display bus as far as this program can reach it anyway.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from types import TracebackType

__all__ = ["BusLock"]

_log = logging.getLogger(__name__)

_WAIT_OBJECT_0 = 0x000
_WAIT_ABANDONED = 0x080
_WAIT_TIMEOUT = 0x102

MUTEX_NAME = r"Local\monitorctl.ddc-bus"

#: Longer than any single DDC/CI operation (a capabilities read is the slowest,
#: well under a second).  Past this the other process is wedged, and going
#: ahead beats hanging forever.
TIMEOUT_MS = 10_000


class BusLock:
    """Re-entrant within a thread, exclusive across processes."""

    def __init__(self, name: str = MUTEX_NAME) -> None:
        if sys.platform != "win32":  # pragma: no cover
            raise OSError("BusLock is Windows-only")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self._kernel32
        k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.ReleaseMutex.argtypes = [wintypes.HANDLE]
        k.ReleaseMutex.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        self._handle = k.CreateMutexW(None, False, name)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._held: list[bool] = []

    def __enter__(self) -> None:
        result = self._kernel32.WaitForSingleObject(self._handle, TIMEOUT_MS)
        if result == _WAIT_ABANDONED:
            # The previous holder died mid-operation.  The bus itself is fine:
            # the monitor finished or dropped that transaction long ago.
            _log.info(
                "previous holder of the display-bus lock exited without releasing it"
            )
        elif result == _WAIT_TIMEOUT:
            _log.warning(
                "display bus busy for %d ms in another process; going ahead", TIMEOUT_MS
            )
            self._held.append(False)
            return
        elif result != _WAIT_OBJECT_0:
            _log.warning("could not take the display-bus lock (wait result %#x)", result)
            self._held.append(False)
            return
        self._held.append(True)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._held.pop():
            self._kernel32.ReleaseMutex(self._handle)

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
