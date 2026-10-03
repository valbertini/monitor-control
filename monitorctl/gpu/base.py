"""GPU-side adjustment interface.

Some controls a user thinks of as "monitor settings" are not reachable over
DDC/CI at all, because the monitor's firmware simply does not implement the
MCCS code.  Saturation is the common case: neither monitor surveyed for
``docs/monitors/`` answers VCP ``0x8A`` or the six-axis codes.

Those adjustments can still be made, one step earlier in the pipeline, on the
GPU.  This package provides that fallback.  It is a genuinely different
mechanism with different trade-offs, so it is kept in its own namespace and
labelled as such in the UI rather than pretending to be a monitor control:

* It applies to the *signal*, not the panel.  A screenshot is unaffected, but
  so is anything else displaying on that output.
* It is per-GPU-output, and resets when the driver reloads or the machine
  reboots.
* It costs nothing in EEPROM wear and does not touch the I2C bus, so none of
  the DDC pacing or write-budget machinery applies.
* Other software fights over the same knobs - f.lux, Night Light and the
  NVIDIA control panel all write the same state.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

__all__ = ["GpuAdjustment", "GpuBackend", "GpuReading"]


@dataclass(frozen=True, slots=True)
class GpuAdjustment:
    """Static metadata for one GPU-side control.

    Attributes:
        key: Stable identifier, namespaced with a ``gpu_`` prefix so it can
            never collide with a VCP feature key.
        label: Short human-readable name.
        category: Matching :class:`~monitorctl.ddc.vcp.FeatureCategory` value.
        tooltip: Longer explanation shown on hover.
        minimum: Lowest accepted value.
        maximum: Highest accepted value.
        neutral: The value that means "no adjustment".
        source: Which mechanism applies it, shown in the UI.
        writable: Whether it can be changed, or only read.
    """

    key: str
    label: str
    category: str
    tooltip: str
    minimum: int
    maximum: int
    neutral: int
    source: str
    writable: bool = True

    def __post_init__(self) -> None:
        if not self.key.startswith("gpu_"):
            raise ValueError(f"GPU adjustment key must start with 'gpu_': {self.key!r}")
        if self.minimum >= self.maximum:
            raise ValueError(f"{self.key}: minimum must be below maximum")
        if not self.minimum <= self.neutral <= self.maximum:
            raise ValueError(f"{self.key}: neutral is outside [minimum, maximum]")


@dataclass(frozen=True, slots=True)
class GpuReading:
    """Current value of one GPU-side control."""

    current: int
    minimum: int
    maximum: int


class GpuBackend(abc.ABC):
    """One mechanism for adjusting the signal on the GPU side.

    Implementations must be inert when the hardware or driver is absent:
    :meth:`available` returns ``False`` and nothing else is ever called.
    """

    #: Short identifier used in error messages and in the UI.
    name: str = "gpu"

    @abc.abstractmethod
    def available(self) -> bool:
        """Whether this backend can be used on this machine right now."""

    @abc.abstractmethod
    def devices(self) -> tuple[str, ...]:
        """OS device names this backend can drive, e.g. ``\\\\.\\DISPLAY1``."""

    def track(self, device: str) -> None:  # noqa: B027 - optional hook, no-op by default
        """Learn about ``device`` on first sight.

        For a backend that can drive any output it is asked about, rather than
        a fixed list found at start-up.  The default does nothing.
        """

    def refresh(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Displays were re-enumerated: renew whatever per-display state the
        backend holds.  The default does nothing."""

    @abc.abstractmethod
    def adjustments(self) -> tuple[GpuAdjustment, ...]:
        """The controls this backend offers."""

    @abc.abstractmethod
    def get(self, device: str, key: str) -> GpuReading:
        """Read one control.

        Raises:
            GpuError: If the read fails.
        """

    @abc.abstractmethod
    def set(self, device: str, key: str, value: int) -> int:
        """Write one control and return the value actually applied.

        Raises:
            GpuError: If the write fails or could not be confirmed.
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Release any driver resources.  Must be idempotent."""
