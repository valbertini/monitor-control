"""Platform-independent backend interface.

Keeping the OS calls behind this interface is what makes the safety policy, the
capabilities parser and the service layer testable without hardware, and what
would let a Linux (``/dev/i2c-*``) backend drop in later.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

from .edid import EdidInfo

__all__ = ["DdcBackend", "PhysicalMonitor", "VcpReading"]


@dataclass(frozen=True, slots=True)
class PhysicalMonitor:
    """One physical display as seen by the backend.

    Attributes:
        token: Opaque backend-owned handle.  Callers must treat it as unusable
            after :meth:`DdcBackend.release`.
        index: Enumeration order, used only to disambiguate identical models.
        description: Backend-supplied description, often the generic driver
            name (e.g. "Generic PnP Monitor") and therefore rarely useful alone.
        device: OS device path, e.g. ``\\\\.\\DISPLAY1`` on Windows.
        primary: Whether the OS considers this the primary display.
        edid: Identity reported by the monitor itself, or ``None`` when the
            backend could not obtain a valid EDID.  Unlike ``device`` it does
            not change when the OS renumbers displays.
    """

    token: object
    index: int
    description: str
    device: str
    primary: bool
    edid: EdidInfo | None = None


@dataclass(frozen=True, slots=True)
class VcpReading:
    """Result of reading one VCP feature.

    Attributes:
        current: Current raw value.
        maximum: Maximum raw value the monitor reports.  Not necessarily 100.
    """

    current: int
    maximum: int


class DdcBackend(abc.ABC):
    """Abstract DDC/CI transport.

    Implementations are not required to be thread-safe; :class:`~monitorctl.
    ddc.service.MonitorService` serialises all access behind a single lock
    because the display bus itself is not reentrant.
    """

    @abc.abstractmethod
    def enumerate(self) -> list[PhysicalMonitor]:
        """Discover attached displays and open handles to them."""

    @abc.abstractmethod
    def get_vcp(self, monitor: PhysicalMonitor, code: int) -> VcpReading:
        """Read one VCP feature.

        Raises:
            DdcUnsupportedFeature: The monitor has no such feature.
            DdcTransientError: Recoverable bus glitch.
            DdcMonitorGone: The handle is stale.
            DdcBackendError: Anything else.
        """

    @abc.abstractmethod
    def set_vcp(self, monitor: PhysicalMonitor, code: int, value: int) -> None:
        """Write one VCP feature.  Same exception contract as :meth:`get_vcp`."""

    @abc.abstractmethod
    def read_capabilities(self, monitor: PhysicalMonitor) -> str:
        """Return the raw capabilities string, or ``""`` if unavailable."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release every handle this backend still owns.  Must be idempotent."""
