"""In-memory backend used to exercise the stack without real hardware."""

from __future__ import annotations

from dataclasses import dataclass, field

from monitorctl.ddc.backend import DdcBackend, PhysicalMonitor, VcpReading
from monitorctl.ddc.edid import EdidInfo
from monitorctl.ddc.errors import (
    DdcMonitorGone,
    DdcTransientError,
    DdcUnsupportedFeature,
)
from monitorctl.ddc.snapshot import SCHEMA_VERSION, Snapshot

__all__ = ["FakeBackend", "FakeMonitor", "legacy_payload"]


@dataclass
class FakeMonitor:
    """A simulated display.

    Attributes:
        device: OS device path reported to the service.
        description: Driver-supplied description.
        capabilities: Raw capabilities string to hand back.
        values: Current VCP values, keyed by code.
        maxima: Maximum per VCP code.
        primary: Whether this is the primary display.
        transient_failures: Codes that fail once with a transient error before
            succeeding, used to test the retry path.
        gone: When true, every operation raises :class:`DdcMonitorGone`.
        clamp_at: Optional per-code ceiling the firmware silently enforces,
            used to test read-back verification warnings.
        edid: Identity handed to the service, as the Windows backend would
            read it from the registry.
        ignores_same_value: Codes whose firmware drops a write of the value
            it already reports, like the TCL 25G64 after a power cycle.
    """

    device: str = r"\\.\DISPLAY1"
    description: str = "Generic PnP Monitor"
    capabilities: str = ""
    values: dict[int, int] = field(default_factory=dict)
    maxima: dict[int, int] = field(default_factory=dict)
    primary: bool = True
    transient_failures: dict[int, int] = field(default_factory=dict)
    gone: bool = False
    clamp_at: dict[int, int] = field(default_factory=dict)
    edid: EdidInfo | None = None
    ignores_same_value: frozenset[int] = frozenset()

    #: Populated by the backend so assertions can inspect the write history.
    writes: list[tuple[int, int]] = field(default_factory=list)
    #: Every VCP read attempted, answered or not, to assert on bus traffic.
    reads: list[int] = field(default_factory=list)


class FakeBackend(DdcBackend):
    """Deterministic backend over a list of :class:`FakeMonitor`."""

    def __init__(self, monitors: list[FakeMonitor]) -> None:
        self._monitors = monitors
        self._released: set[int] = set()
        self.enumerate_calls = 0
        self.closed = False

    def enumerate(self) -> list[PhysicalMonitor]:
        self.enumerate_calls += 1
        self._released.clear()
        return [
            PhysicalMonitor(
                token=index,
                index=index,
                description=monitor.description,
                device=monitor.device,
                primary=monitor.primary,
                edid=monitor.edid,
            )
            for index, monitor in enumerate(self._monitors)
        ]

    def _target(self, monitor: PhysicalMonitor) -> FakeMonitor:
        index = monitor.token
        if not isinstance(index, int) or index in self._released:
            raise DdcMonitorGone("handle liberado")
        target = self._monitors[index]
        if target.gone:
            raise DdcMonitorGone("monitor desconectado")
        return target

    def get_vcp(self, monitor: PhysicalMonitor, code: int) -> VcpReading:
        if isinstance(monitor.token, int) and monitor.token < len(self._monitors):
            self._monitors[monitor.token].reads.append(code)
        target = self._target(monitor)
        remaining = target.transient_failures.get(code, 0)
        if remaining > 0:
            target.transient_failures[code] = remaining - 1
            raise DdcTransientError(f"falha transiente simulada em 0x{code:02X}")
        if code not in target.values:
            raise DdcUnsupportedFeature(f"0x{code:02X} nao suportado")
        return VcpReading(
            current=target.values[code], maximum=target.maxima.get(code, 100)
        )

    def set_vcp(self, monitor: PhysicalMonitor, code: int, value: int) -> None:
        target = self._target(monitor)
        if code not in target.values:
            raise DdcUnsupportedFeature(f"0x{code:02X} nao suportado")
        if code in target.ignores_same_value and target.values[code] == value:
            return
        target.writes.append((code, value))
        ceiling = target.clamp_at.get(code)
        target.values[code] = min(value, ceiling) if ceiling is not None else value

    def read_capabilities(self, monitor: PhysicalMonitor) -> str:
        return self._target(monitor).capabilities

    def drop_handles(self) -> None:
        """Invalidate every handle handed out, as Windows does when a monitor
        is power-cycled; only the next :meth:`enumerate` hands out live ones."""
        self._released.update(range(len(self._monitors)))

    def close(self) -> None:
        self.closed = True
        self._released.clear()


def legacy_payload(snapshot: Snapshot) -> dict[str, object]:
    """``snapshot`` in the shape of the pre-profiles ``snapshot.json``.

    Nothing in the program writes that file any more; the tests write one to
    check that it is still read.
    """
    return {
        "version": SCHEMA_VERSION,
        "savedAt": snapshot.saved_at,
        "monitors": [
            {
                "id": m.id,
                "name": m.name,
                "device": m.device,
                "hardwareId": m.hardware_id,
                "values": dict(m.values),
            }
            for m in snapshot.monitors
        ],
    }
