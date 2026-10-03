"""Write-safety policy.

Every write to a monitor passes through :func:`check_write` before a single byte
reaches the display bus.  The policy encodes the risks identified during research
(``docs/PESQUISA-DDC-CI.md`` section 3):

1. Manufacturer-reserved codes ``0xE0``-``0xFF`` can put some monitors into
   factory/service modes.  This is the only documented path to permanent damage,
   so the range is refused unconditionally, with no override flag.
2. A handful of standard codes are destructive or known to strand monitors
   (factory resets, EEPROM save, DPMS power-off, OSD lock).  Also refused.
3. EEPROM cells survive on the order of 100k writes.  A burst-prone UI can burn
   through that, so writes are metered by a token bucket.
4. Values outside the monitor's advertised range are clamped rather than sent.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .errors import PolicyViolation, RateLimited
from .vcp import FeatureKind, VcpFeature

__all__ = [
    "BLOCKED_CODES",
    "MANUFACTURER_RESERVED_END",
    "MANUFACTURER_RESERVED_START",
    "WriteBudget",
    "check_write",
    "clamp",
]

MANUFACTURER_RESERVED_START = 0xE0
MANUFACTURER_RESERVED_END = 0xFF

#: Standard VCP codes this application refuses to write, with the reason.
#: Refusing them costs nothing: none is needed to adjust a picture.
BLOCKED_CODES: dict[int, str] = {
    0x01: "Degauss applies only to CRTs; on an LCD the behaviour is undefined.",
    0x04: "Restore Factory Defaults wipes the monitor's entire calibration.",
    0x05: "Restore Factory Brightness/Contrast Defaults discards your settings.",
    0x06: "Restore Factory Geometry Defaults discards your settings.",
    0x08: "Restore Color Defaults discards your colour calibration.",
    0xB0: (
        "Settings (store/restore) writes explicitly to the monitor's EEPROM, "
        "which has a limited number of write cycles."
    ),
    0xCA: (
        "OSD / button control can lock the monitor's physical buttons. If DDC/CI "
        "then becomes unavailable you lose both ways of controlling the display."
    ),
    0xD6: (
        "Power Mode (DPMS) over DDC/CI: monitors are documented to enter a deep "
        "power-off state they never wake from, needing a mains power cycle."
    ),
}


def _reserved_reason(code: int) -> str:
    return (
        f"VCP 0x{code:02X} is in the manufacturer-reserved range "
        f"(0x{MANUFACTURER_RESERVED_START:02X}-0x{MANUFACTURER_RESERVED_END:02X}). "
        "Writing to that range is the only documented way to permanently damage "
        "a monitor over DDC/CI."
    )


def clamp(value: int, minimum: int, maximum: int) -> int:
    """Constrain ``value`` to ``[minimum, maximum]``.

    Raises:
        ValueError: If the bounds are inverted, which would silently produce
            nonsense instead of failing loudly.
    """
    if minimum > maximum:
        raise ValueError(f"inverted bounds: minimum={minimum} > maximum={maximum}")
    return max(minimum, min(maximum, value))


@dataclass(slots=True)
class WriteBudget:
    """Thread-safe token bucket metering writes to the display bus.

    Defaults allow a comfortable interactive session (40 writes immediately,
    then 30 per minute sustained) while making a runaway loop impossible: a bug
    that tries to write continuously is capped at roughly 43k writes per day
    instead of millions.

    Attributes:
        capacity: Maximum tokens held, i.e. the largest allowed burst.
        refill_per_second: Sustained write rate once the burst is spent.
    """

    capacity: int = 40
    refill_per_second: float = 0.5
    _tokens: float = field(init=False, repr=False, default=0.0)
    _updated_at: float = field(init=False, repr=False, default=0.0)
    _lock: threading.Lock = field(init=False, repr=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        if self.refill_per_second <= 0:
            raise ValueError("refill_per_second must be positive")
        self._tokens = float(self.capacity)
        self._updated_at = time.monotonic()

    def _refill_locked(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated_at)
        self._updated_at = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)

    def consume(self, tokens: int = 1) -> None:
        """Spend ``tokens`` from the bucket.

        Raises:
            RateLimited: If the budget is exhausted.  The message states how
                long to wait, so the caller can surface something actionable.
        """
        if tokens <= 0:
            raise ValueError("tokens must be positive")
        with self._lock:
            now = time.monotonic()
            self._refill_locked(now)
            if self._tokens < tokens:
                deficit = tokens - self._tokens
                wait = deficit / self.refill_per_second
                raise RateLimited(
                    "Write budget exhausted (EEPROM wear protection). "
                    f"Try again in {wait:.1f}s."
                )
            self._tokens -= tokens

    def available(self) -> float:
        """Current token count, for diagnostics."""
        with self._lock:
            self._refill_locked(time.monotonic())
            return self._tokens


def check_write(
    feature: VcpFeature,
    value: int,
    *,
    maximum: int,
    allowed_values: frozenset[int] | None = None,
) -> int:
    """Validate and normalise a write request.

    Args:
        feature: The registry entry being written.
        value: Requested raw VCP value.
        maximum: Maximum the monitor reported for this feature.
        allowed_values: For non-continuous features, the values the monitor
            advertised in its capabilities string.  ``None`` means the monitor
            did not enumerate them, in which case the standard value map is
            used as the allowlist.

    Returns:
        The value to actually send, clamped into range for continuous features.

    Raises:
        PolicyViolation: If the code is blocked, the value is not an integer in
            range, or a non-continuous value is not one the monitor accepts.
    """
    code = feature.code

    if MANUFACTURER_RESERVED_START <= code <= MANUFACTURER_RESERVED_END:
        raise PolicyViolation(_reserved_reason(code))

    if code in BLOCKED_CODES:
        raise PolicyViolation(f"VCP 0x{code:02X} is blocked: {BLOCKED_CODES[code]}")

    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyViolation(f"value must be an integer, got {type(value).__name__}")

    # The wire format carries a 16-bit value; nothing wider can be expressed.
    if not 0 <= value <= 0xFFFF:
        raise PolicyViolation(
            f"value {value} is outside the protocol's representable range (0..65535)"
        )

    if feature.kind is FeatureKind.CONTINUOUS:
        if maximum <= 0:
            raise PolicyViolation(
                f"monitor reported an invalid maximum ({maximum}) for {feature.key}"
            )
        return clamp(value, 0, maximum)

    permitted = allowed_values if allowed_values else frozenset(feature.values)
    if value not in permitted:
        readable = ", ".join(f"0x{v:02X}" for v in sorted(permitted))
        raise PolicyViolation(
            f"value 0x{value:02X} is not accepted by {feature.key}; permitted: {readable}"
        )
    return value
