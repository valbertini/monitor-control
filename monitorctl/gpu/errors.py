"""Errors raised by the GPU-side adjustment backends."""

from __future__ import annotations

from ..ddc.errors import DdcError

__all__ = ["GpuError", "GpuUnavailable"]


class GpuError(DdcError):
    """A GPU-side adjustment failed.

    Subclasses :class:`~monitorctl.ddc.errors.DdcError` so that callers which
    already handle "a display adjustment went wrong" keep working without
    knowing which mechanism was used.
    """


class GpuUnavailable(GpuError):
    """No GPU backend can drive this display."""
