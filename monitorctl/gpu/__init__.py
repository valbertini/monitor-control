"""GPU-side adjustments, used where DDC/CI cannot reach.

Saturation and hue are the motivating case: they need the colour channels
mixed, which neither the MCCS codes a monitor may lack nor a per-channel gamma
ramp can do.  The NVIDIA driver can, so that path exists here alongside a
software tone-curve fallback.
"""

from .base import GpuAdjustment, GpuBackend, GpuReading
from .controller import GpuController
from .errors import GpuError, GpuUnavailable

__all__ = [
    "GpuAdjustment",
    "GpuBackend",
    "GpuController",
    "GpuError",
    "GpuReading",
    "GpuUnavailable",
]
