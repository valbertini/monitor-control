"""Exception hierarchy and Windows DDC/CI error decoding.

The Windows Monitor Configuration API reports DDC/CI problems through
``GetLastError`` using a dedicated block of ``ERROR_GRAPHICS_*`` codes defined in
``winerror.h``.  Most wrappers collapse them into a single boolean failure, which
throws away the one piece of information that matters: whether the failure is a
transient bus glitch worth retrying, a permanent "this monitor has no such
feature", or a sign that the monitor is simply gone.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "DdcBackendError",
    "DdcError",
    "DdcMonitorGone",
    "DdcTransientError",
    "DdcUnsupportedFeature",
    "FeatureNotFound",
    "MonitorNotFound",
    "PolicyViolation",
    "ProfileError",
    "RateLimited",
    "SnapshotError",
    "SnapshotMissing",
    "WindowsDdcStatus",
    "classify_windows_error",
    "raise_for_status",
]


class DdcError(Exception):
    """Base class for every error raised by this package."""


class DdcBackendError(DdcError):
    """A platform call failed for a reason we could not classify further."""


class DdcTransientError(DdcBackendError):
    """A recoverable I2C/DDC glitch.  Retrying once is appropriate."""


class DdcUnsupportedFeature(DdcBackendError):
    """The monitor reported that it does not implement this VCP code."""


class DdcMonitorGone(DdcBackendError):
    """The monitor handle is stale; the display list must be rebuilt."""


class PolicyViolation(DdcError):
    """A write was refused by the safety policy before touching the hardware."""


class RateLimited(DdcError):
    """A write was refused because the write budget is exhausted."""


class MonitorNotFound(DdcError):
    """No monitor matches the supplied identifier."""


class FeatureNotFound(DdcError):
    """No known feature matches the supplied key."""


class SnapshotMissing(DdcError):
    """A restore was requested but nothing has been saved yet."""


class SnapshotError(DdcError):
    """The saved profiles could not be written to disk."""


class ProfileError(DdcError):
    """A profile operation was refused: a bad or duplicate name, an unknown
    profile, or deleting the one in use.  The message is meant for a person."""


class WindowsDdcStatus(Enum):
    """Semantic classification of a Windows DDC/CI error code."""

    OK = "ok"
    NOT_SUPPORTED = "not_supported"
    DATA_INVALID = "data_invalid"
    MESSAGE_INVALID = "message_invalid"
    TRANSMISSION_FAILED = "transmission_failed"
    MONITOR_GONE = "monitor_gone"
    UNKNOWN = "unknown"

    @property
    def transient(self) -> bool:
        """Whether a single immediate retry is worth attempting.

        ``MESSAGE_INVALID`` and ``TRANSMISSION_FAILED`` both mean the bytes on
        the wire were mangled rather than the request being wrong, so the same
        request may well succeed on a second attempt.
        """
        return self in (
            WindowsDdcStatus.MESSAGE_INVALID,
            WindowsDdcStatus.TRANSMISSION_FAILED,
        )


# From winerror.h.  These are HRESULT-shaped values in the graphics facility.
ERROR_GRAPHICS_I2C_ERROR_TRANSMITTING_DATA = 0xC0262582
ERROR_GRAPHICS_I2C_ERROR_RECEIVING_DATA = 0xC0262583
ERROR_GRAPHICS_DDCCI_VCP_NOT_SUPPORTED = 0xC0262584
ERROR_GRAPHICS_DDCCI_INVALID_DATA = 0xC0262585
ERROR_GRAPHICS_DDCCI_MONITOR_RETURNED_INVALID_TIMING_STATUS_BYTE = 0xC0262586
ERROR_GRAPHICS_MCA_INVALID_CAPABILITIES_STRING = 0xC0262587
ERROR_GRAPHICS_DDCCI_INVALID_MESSAGE_COMMAND = 0xC0262589
ERROR_GRAPHICS_DDCCI_INVALID_MESSAGE_LENGTH = 0xC026258A
ERROR_GRAPHICS_DDCCI_INVALID_MESSAGE_CHECKSUM = 0xC026258B
ERROR_GRAPHICS_INVALID_PHYSICAL_MONITOR_HANDLE = 0xC026258C
ERROR_GRAPHICS_MONITOR_NO_LONGER_EXISTS = 0xC026258D

_STATUS_BY_CODE: dict[int, WindowsDdcStatus] = {
    ERROR_GRAPHICS_I2C_ERROR_TRANSMITTING_DATA: WindowsDdcStatus.TRANSMISSION_FAILED,
    # The read-side twin: a garbled reply is as worth one retry as a garbled
    # request.
    ERROR_GRAPHICS_I2C_ERROR_RECEIVING_DATA: WindowsDdcStatus.TRANSMISSION_FAILED,
    ERROR_GRAPHICS_DDCCI_VCP_NOT_SUPPORTED: WindowsDdcStatus.NOT_SUPPORTED,
    ERROR_GRAPHICS_DDCCI_INVALID_DATA: WindowsDdcStatus.DATA_INVALID,
    ERROR_GRAPHICS_DDCCI_MONITOR_RETURNED_INVALID_TIMING_STATUS_BYTE: (
        WindowsDdcStatus.MESSAGE_INVALID
    ),
    ERROR_GRAPHICS_MCA_INVALID_CAPABILITIES_STRING: WindowsDdcStatus.DATA_INVALID,
    ERROR_GRAPHICS_DDCCI_INVALID_MESSAGE_COMMAND: WindowsDdcStatus.MESSAGE_INVALID,
    ERROR_GRAPHICS_DDCCI_INVALID_MESSAGE_LENGTH: WindowsDdcStatus.MESSAGE_INVALID,
    ERROR_GRAPHICS_DDCCI_INVALID_MESSAGE_CHECKSUM: WindowsDdcStatus.MESSAGE_INVALID,
    ERROR_GRAPHICS_INVALID_PHYSICAL_MONITOR_HANDLE: WindowsDdcStatus.MONITOR_GONE,
    ERROR_GRAPHICS_MONITOR_NO_LONGER_EXISTS: WindowsDdcStatus.MONITOR_GONE,
}

_HUMAN_READABLE: dict[WindowsDdcStatus, str] = {
    WindowsDdcStatus.NOT_SUPPORTED: "monitor does not implement this VCP feature",
    WindowsDdcStatus.DATA_INVALID: "monitor returned malformed data",
    WindowsDdcStatus.MESSAGE_INVALID: (
        "DDC message was rejected (command/length/checksum)"
    ),
    WindowsDdcStatus.TRANSMISSION_FAILED: "I2C transmission error on the display bus",
    WindowsDdcStatus.MONITOR_GONE: "monitor is no longer connected",
    WindowsDdcStatus.UNKNOWN: "unclassified Monitor Configuration API failure",
}


def classify_windows_error(error_code: int) -> tuple[WindowsDdcStatus, str]:
    """Map a ``GetLastError`` value to a status and a human-readable reason.

    Args:
        error_code: Raw value from ``ctypes.get_last_error()``.  Signed or
            unsigned, both are accepted.

    Returns:
        The semantic status and a short description suitable for logging or for
        showing to a user.
    """
    unsigned = error_code & 0xFFFFFFFF
    if unsigned == 0:
        return WindowsDdcStatus.OK, "no error"
    status = _STATUS_BY_CODE.get(unsigned, WindowsDdcStatus.UNKNOWN)
    return status, f"{_HUMAN_READABLE[status]} (0x{unsigned:08X})"


def raise_for_status(status: WindowsDdcStatus, message: str) -> None:
    """Translate a classified status into the matching exception type."""
    if status is WindowsDdcStatus.OK:
        return
    if status is WindowsDdcStatus.NOT_SUPPORTED:
        raise DdcUnsupportedFeature(message)
    if status is WindowsDdcStatus.MONITOR_GONE:
        raise DdcMonitorGone(message)
    if status.transient:
        raise DdcTransientError(message)
    raise DdcBackendError(message)
