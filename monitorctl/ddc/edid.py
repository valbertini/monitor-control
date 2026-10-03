"""EDID parsing: who a monitor *is*, independent of where it is plugged in.

Every identifier the OS hands out for a display describes a position rather than
a panel.  ``\\\\.\\DISPLAY1`` is assigned in enumeration order and swaps between
boots; the PnP instance path encodes the connector.  The EDID block is the one
thing the monitor itself reports, so it is what a saved calibration should be
tied to.

Only the base 128-byte block is read.  It holds everything needed here - the
manufacturer, the product code, the serial number and the model name - and
extension blocks (CEA, DisplayID) add nothing to identity.

The bytes come from the registry, which the display driver fills from whatever
the monitor sent over DDC.  They are treated as untrusted: a block that fails
the header or checksum is rejected rather than half-parsed, because a garbled
identity is worse than none - it could match the wrong saved monitor.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["EdidInfo", "parse_edid"]

_HEADER = bytes((0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00))
_BLOCK_SIZE = 128

#: Offsets of the four 18-byte display descriptors in the base block.
_DESCRIPTOR_OFFSETS = (54, 72, 90, 108)
_TAG_SERIAL_TEXT = 0xFF
_TAG_NAME = 0xFC

#: Numeric serials that panels report when they have none.  0x01010101 is the
#: value ASUS ships in every VS248; all-ones is the "field unused" pattern.
_PLACEHOLDER_SERIALS = frozenset({0x00000000, 0x01010101, 0xFFFFFFFF})


@dataclass(frozen=True, slots=True)
class EdidInfo:
    """Identity fields from an EDID base block.

    Attributes:
        manufacturer: Three-letter PnP vendor id, e.g. ``"TCL"``.
        product_code: Vendor-assigned model number.
        serial_number: The numeric serial field, often a placeholder.
        serial_text: The serial-number descriptor, when present.  Usually the
            real serial; the numeric field frequently is not.
        name: The monitor-name descriptor, e.g. ``"25G64"``.
    """

    manufacturer: str
    product_code: int
    serial_number: int
    serial_text: str
    name: str

    @property
    def model_key(self) -> str:
        """Identifies the model, shared by every unit of it: ``"TCL-1003"``."""
        return f"{self.manufacturer}-{self.product_code:04X}"

    @property
    def hardware_id(self) -> str:
        """Identifies this unit, as far as the EDID allows.

        Two identical panels without a serial number get the same id.  That is
        not papered over: matching treats a shared id as ambiguous and falls
        back to other evidence rather than guessing.
        """
        if self.serial_text:
            return f"{self.model_key}-{self.serial_text}"
        if self.serial_number not in _PLACEHOLDER_SERIALS:
            return f"{self.model_key}-{self.serial_number:08X}"
        return self.model_key


def parse_edid(data: bytes) -> EdidInfo | None:
    """Extract identity fields, or ``None`` if ``data`` is not a valid EDID.

    Args:
        data: The raw block, at least 128 bytes.  Extension blocks after the
            first 128 bytes are ignored.
    """
    if len(data) < _BLOCK_SIZE:
        return None
    block = data[:_BLOCK_SIZE]
    if block[:8] != _HEADER:
        return None
    if sum(block) % 256 != 0:
        return None

    packed = int.from_bytes(block[8:10], "big")
    letters = [(packed >> shift) & 0x1F for shift in (10, 5, 0)]
    if packed & 0x8000 or not all(1 <= letter <= 26 for letter in letters):
        return None
    manufacturer = "".join(chr(ord("A") + letter - 1) for letter in letters)

    serial_text = ""
    name = ""
    for offset in _DESCRIPTOR_OFFSETS:
        descriptor = block[offset : offset + 18]
        # Display descriptors start with three zero bytes; anything else here is
        # a detailed timing descriptor and carries no text.
        if descriptor[:3] != b"\x00\x00\x00":
            continue
        tag = descriptor[3]
        if tag == _TAG_SERIAL_TEXT and not serial_text:
            serial_text = _descriptor_text(descriptor)
        elif tag == _TAG_NAME and not name:
            name = _descriptor_text(descriptor)

    return EdidInfo(
        manufacturer=manufacturer,
        product_code=int.from_bytes(block[10:12], "little"),
        serial_number=int.from_bytes(block[12:16], "little"),
        serial_text=serial_text,
        name=name,
    )


def _descriptor_text(descriptor: bytes) -> str:
    """The 13-byte text payload, cut at the line feed that terminates it.

    Only printable ASCII survives.  The text ends up in identifiers and in the
    UI, and nothing a monitor sends should be able to put a control character
    in either.
    """
    raw = descriptor[5:18].split(b"\x0a", 1)[0]
    text = "".join(chr(b) for b in raw if 0x20 <= b < 0x7F)
    return text.strip()
