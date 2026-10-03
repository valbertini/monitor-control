"""Per-model firmware quirks that cannot be discovered by probing.

Everything else in this package learns about a monitor by asking it.  The
entries here exist because the monitor's answers are wrong in a way no probe can
detect, so the knowledge has to come from someone watching the screen.

The one quirk recorded so far is **volatile controls**: the firmware keeps the
value it was sent over DDC/CI in RAM, keeps *reporting* it after a power cycle,
but drives the panel from its factory default.  Two things follow:

* A read-back cannot tell whether the control is in effect, so a restore must
  write it whether or not the value appears to have drifted.
* Writing the value it already reports is ignored as a no-op, so a restore has
  to step away from it and back.

Entries are keyed by :attr:`~monitorctl.ddc.edid.EdidInfo.model_key`, which is
shared by every unit of a model and survives cable and port changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .edid import EdidInfo

__all__ = ["QUIRKS", "ModelQuirks", "quirks_for"]


@dataclass(frozen=True, slots=True)
class ModelQuirks:
    """What is known to be wrong with one monitor model.

    Attributes:
        model_key: EDID manufacturer and product code, e.g. ``"TCL-1003"``.
        model: Marketing name, for people reading this file.
        volatile: Feature keys the firmware forgets on power-off while still
            reporting the forgotten value.  Only continuous features belong
            here: restoring one means stepping to a neighbouring value, and a
            choice feature has no safe neighbour.
        evidence: How the quirk was established, so it can be re-checked.
    """

    model_key: str
    model: str
    volatile: frozenset[str]
    evidence: str


QUIRKS: Mapping[str, ModelQuirks] = MappingProxyType(
    {
        quirk.model_key: quirk
        for quirk in (
            ModelQuirks(
                model_key="TCL-1003",
                model="TCL 25G64",
                volatile=frozenset(
                    {
                        "red_gain",
                        "green_gain",
                        "blue_gain",
                        "red_black_level",
                        "green_black_level",
                        "blue_black_level",
                    }
                ),
                evidence=(
                    "2026-09-30, firmware reporting MCCS 2.0.  After the monitor's "
                    "power button, the panel returned to factory gains and offsets "
                    "while 0x16/0x18/0x1A/0x6C/0x6E/0x70 kept reading the values set "
                    "over DDC/CI.  Rewriting the same value changed nothing; stepping "
                    "down and back up restored the picture.  SaveCurrentSettings "
                    "(0xB0) returned success and did not persist anything, and "
                    "neither did opening and closing the OSD menu."
                ),
            ),
        )
    }
)


def quirks_for(edid: EdidInfo | None) -> ModelQuirks | None:
    """The quirks recorded for a monitor's model, if any."""
    if edid is None:
        return None
    return QUIRKS.get(edid.model_key)
