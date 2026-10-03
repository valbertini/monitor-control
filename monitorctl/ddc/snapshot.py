"""Saved values of monitors, and how a saved entry finds its monitor again.

Calibrating a monitor is a long series of small, individually irreversible
steps.  Nothing in DDC/CI records what a control held before you moved it, and
the monitor's own "restore factory defaults" is both blocked here
(``ddc/policy.py``) and far too blunt: it discards the entire calibration, not
the last five minutes of fiddling.

A :class:`Snapshot` is one value per restorable control per monitor: what
Restore puts back and what the keeper maintains.  Since profiles
(``ddc/profiles.py``) it is a *view* - the active profile of every monitor -
rather than something stored on its own.  Three properties matter:

* **Saving writes nothing to a monitor.**  It takes the values the service
  holds in its cache - or reads them again, from the tray (D24) - so no write
  budget is spent and no EEPROM is touched.
* **Restoring writes only what drifted.**  A value already equal to the saved
  one is left alone, so a restore is a handful of writes rather than one per
  feature.  The EEPROM wear argument behind ``policy.WriteBudget`` applies here
  exactly as it does to a slider drag.
* **The file on disk is not trusted.**  Everything read back still goes through
  the normal write path, so a corrupted or hand-edited file cannot reach the
  hardware with a value the policy would refuse.

The JSON shape below is that of ``snapshot.json``, the single-snapshot file of
versions before profiles; it is read once, to carry those values over.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, TypeVar

from ..fileio import state_directory

__all__ = [
    "NON_RESTORABLE_KEYS",
    "SCHEMA_VERSION",
    "Identity",
    "MonitorSnapshot",
    "Snapshot",
    "legacy_snapshot_path",
    "pair_monitors",
    "parse_values",
]

_log = logging.getLogger(__name__)

#: Bumped only when the on-disk shape changes incompatibly.  A file written by
#: a newer version is ignored rather than guessed at.
SCHEMA_VERSION = 1

#: Features that are commands rather than stored state, so there is nothing
#: meaningful to capture or put back.  ``auto_setup`` is momentary: it reads as
#: "idle" and writing the other value triggers a calibration pass.
NON_RESTORABLE_KEYS: frozenset[str] = frozenset({"auto_setup"})

#: The wire format carries a 16-bit value; nothing wider can be expressed.
_MAX_VALUE = 0xFFFF

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def legacy_snapshot_path() -> Path:
    """The single-snapshot file of versions before profiles."""
    return state_directory() / "snapshot.json"


class Identity(Protocol):
    """What identifies a monitor, saved or connected: see :func:`pair_monitors`."""

    @property
    def id(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def device(self) -> str: ...
    @property
    def hardware_id(self) -> str: ...


S = TypeVar("S", bound=Identity)
P = TypeVar("P", bound=Identity)


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    """The saved values of one monitor.

    Several identifiers are stored because none of them is reliable alone.  The
    hardware id comes from the monitor's EDID and is the only one that follows
    the panel rather than the port, but it is missing for monitors whose EDID
    could not be read and for snapshots written before it existed.  The model
    name is shared by identical panels.  The device name, and the id derived
    from it, are positions: Windows renumbers displays between boots.  See
    ``pair_monitors`` below for how they are weighed.

    Attributes:
        id: Monitor id at the time of saving.
        name: Human-readable name at the time of saving.
        device: OS device name, e.g. ``\\\\.\\DISPLAY1``.
        values: Feature key -> raw value.
        hardware_id: EDID identity at the time of saving, or ``""``.
    """

    id: str
    name: str
    device: str
    values: Mapping[str, int]
    hardware_id: str = ""

    def __post_init__(self) -> None:
        # Defensive copy: the caller's dict must not be able to mutate a saved
        # snapshot after the fact.
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything captured by one press of Save.

    Attributes:
        saved_at: Unix timestamp.  Wall clock rather than monotonic, because it
            is shown to a person and has to survive a reboot.
        monitors: One entry per monitor present when the snapshot was taken.
    """

    saved_at: float
    monitors: tuple[MonitorSnapshot, ...]

    @property
    def value_count(self) -> int:
        """How many individual settings the snapshot holds."""
        return sum(len(m.values) for m in self.monitors)

    @classmethod
    def from_payload(cls, payload: object) -> Snapshot:
        """Rebuild a snapshot from parsed JSON, rejecting anything malformed.

        Only the *shape* is checked here: key spelling, integer type and the
        protocol's value range.  Whether a value is legal for a given feature is
        the write policy's decision, taken later against the live monitor, so a
        file that predates a registry change still loads and simply skips what
        no longer applies.

        Raises:
            ValueError: If the payload is not a snapshot this version can read.
        """
        if not isinstance(payload, dict):
            raise ValueError("snapshot must be a JSON object")

        version = payload.get("version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported snapshot version {version!r} "
                f"(this build reads version {SCHEMA_VERSION})"
            )

        saved_at = payload.get("savedAt")
        if isinstance(saved_at, bool) or not isinstance(saved_at, int | float):
            raise ValueError("savedAt must be a number")

        raw_monitors = payload.get("monitors")
        if not isinstance(raw_monitors, list):
            raise ValueError("monitors must be a list")

        monitors: list[MonitorSnapshot] = []
        for entry in raw_monitors:
            if not isinstance(entry, dict):
                raise ValueError("each monitor entry must be a JSON object")
            monitors.append(
                MonitorSnapshot(
                    id=_require_str(entry, "id"),
                    name=_require_str(entry, "name"),
                    device=_require_str(entry, "device"),
                    values=parse_values(entry.get("values")),
                    # Absent from files written before it was recorded; those
                    # still load and match on the remaining identifiers.
                    hardware_id=_require_str(entry, "hardwareId"),
                )
            )
        return cls(saved_at=float(saved_at), monitors=tuple(monitors))


def _require_str(entry: dict[str, object], field: str) -> str:
    value = entry.get(field, "")
    if not isinstance(value, str):
        raise ValueError(f"monitor {field!r} must be a string")
    return value


def parse_values(raw: object) -> dict[str, int]:
    """Check a parsed ``{feature key: value}`` object: shape and range only."""
    if not isinstance(raw, dict):
        raise ValueError("monitor values must be a JSON object")
    values: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise ValueError(f"invalid feature key: {key!r}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"value for {key!r} must be an integer")
        if not 0 <= value <= _MAX_VALUE:
            raise ValueError(f"value for {key!r} is out of range: {value}")
        values[key] = value
    return values


def pair_monitors(saved: Sequence[S], present: Sequence[P]) -> list[tuple[S, P | None]]:
    """Match each saved monitor to a connected one, or to nothing.

    Identifiers are tried in order of how much they are worth trusting:

    1. the EDID hardware id, which belongs to the panel;
    2. the model name, which belongs to the model;
    3. the OS device name, and the id derived from it, which belong to a
       *position*.  Windows renumbers ``\\\\.\\DISPLAYn`` between boots, so
       trusting these first would hand one monitor's calibration to another.

    An identifier decides only when its value is unique on *both* sides: one
    saved entry and one connected monitor.  Unique among the connected monitors
    alone is not enough - with two identical panels saved and one unplugged,
    the survivor would take whichever entry happened to be listed first.

    Two different hardware ids are proof of two different monitors, so such a
    pair is never made, whatever the other identifiers say: the monitor now on
    ``DISPLAY1`` is not the one saved from ``DISPLAY1`` just because it is
    plugged into the same socket.

    Matching runs one identifier at a time across *all* entries, strongest
    first, so an entry that can only be placed by its device name never takes a
    monitor that another entry would have recognised by its EDID.  A monitor
    claimed in an earlier pass is out of the running.
    """

    def same_hardware_or_unknown(entry: S, state: P) -> bool:
        return not (
            entry.hardware_id
            and state.hardware_id
            and entry.hardware_id != state.hardware_id
        )

    matches: dict[int, P] = {}
    unclaimed = list(present)
    for attribute in ("hardware_id", "name", "device", "id"):
        for position, entry in enumerate(saved):
            wanted = getattr(entry, attribute)
            if position in matches or not wanted:
                continue
            if sum(1 for other in saved if getattr(other, attribute) == wanted) != 1:
                continue
            candidates = [
                s
                for s in unclaimed
                if getattr(s, attribute) == wanted and same_hardware_or_unknown(entry, s)
            ]
            if len(candidates) == 1:
                matches[position] = candidates[0]
                unclaimed.remove(candidates[0])
    return [(entry, matches.get(position)) for position, entry in enumerate(saved)]
