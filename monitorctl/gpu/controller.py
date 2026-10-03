"""Routes GPU adjustments to whichever backend can apply them.

Backends are independent and may cover different displays: NVAPI only knows
NVIDIA outputs, while the gamma ramp works on anything Windows can open a
device context for.  The controller hides that, exposing a single flat set of
adjustments per device.

Everything here degrades quietly.  A machine with no NVIDIA card simply has no
NVIDIA adjustments; nothing raises, and the DDC side is untouched.
"""

from __future__ import annotations

import logging

from .base import GpuAdjustment, GpuBackend, GpuReading
from .errors import GpuError, GpuUnavailable

__all__ = ["GpuController"]

_log = logging.getLogger(__name__)


class GpuController:
    """Aggregates the available :class:`GpuBackend` implementations."""

    def __init__(self, backends: list[GpuBackend] | None = None) -> None:
        """
        Args:
            backends: Explicit backend list, for tests.  ``None`` builds the
                real ones and keeps only those that report themselves usable.
        """
        self._backends: list[GpuBackend] = (
            backends if backends is not None else self._discover()
        )
        self._backends = [b for b in self._backends if b.available()]
        self._owner: dict[str, GpuBackend] = {}
        for backend in self._backends:
            for adjustment in backend.adjustments():
                self._owner[adjustment.key] = backend
        if self._backends:
            _log.info(
                "GPU adjustment backends active: %s",
                ", ".join(b.name for b in self._backends),
            )

    @staticmethod
    def _discover() -> list[GpuBackend]:
        from .gamma import GammaRampBackend
        from .nvapi import NvidiaColorBackend

        discovered: list[GpuBackend] = []
        for factory in (NvidiaColorBackend, GammaRampBackend):
            try:
                discovered.append(factory())
            except Exception:  # pragma: no cover - driver dependent
                _log.info("GPU backend %s could not start", factory.__name__)
        return discovered

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def adjustments_for(self, device: str) -> tuple[GpuAdjustment, ...]:
        """Every adjustment applicable to one OS device name.

        The gamma backend accepts any device it can open, so devices are
        registered with it on first sight rather than enumerated up front.
        """
        if not device:
            return ()
        found: list[GpuAdjustment] = []
        for backend in self._backends:
            backend.track(device)
            if device in backend.devices():
                found.extend(backend.adjustments())
        return tuple(found)

    def refresh(self) -> None:
        """Tell every backend the displays were re-enumerated.

        A backend that fails keeps what it had: GPU adjustments are a
        complement, never a reason for an enumeration to fail.
        """
        for backend in self._backends:
            try:
                backend.refresh()
            except Exception:
                _log.warning(
                    "GPU backend %s could not refresh", backend.name, exc_info=True
                )

    def _backend_for(self, key: str) -> GpuBackend:
        backend = self._owner.get(key)
        if backend is None:
            raise GpuUnavailable(f"no GPU backend provides {key!r}")
        return backend

    def get(self, device: str, key: str) -> GpuReading:
        """Read one adjustment.

        Raises:
            GpuError: If the backend is missing or the read fails.
        """
        return self._backend_for(key).get(device, key)

    def set(self, device: str, key: str, value: int) -> int:
        """Write one adjustment, returning what was actually applied.

        Raises:
            GpuError: If the backend is missing or the write fails.
        """
        if not isinstance(value, int) or isinstance(value, bool):
            raise GpuError(f"value must be an integer, got {type(value).__name__}")
        return self._backend_for(key).set(device, key, value)

    def close(self) -> None:
        """Release every backend.  Idempotent."""
        for backend in self._backends:
            try:
                backend.close()
            except Exception:  # pragma: no cover - defensive
                _log.exception("failed to close GPU backend %s", backend.name)
        self._backends.clear()
        self._owner.clear()
