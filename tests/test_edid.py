"""Tests for EDID identity parsing and the registry path derived for it.

The two real blocks below were read from this machine's registry.  They pin the
behaviour that matters in practice: the TCL reports an all-ones numeric serial
and the ASUS reports 0x01010101 for every unit, so the identity has to come from
the text descriptor, not the numeric field.
"""

from __future__ import annotations

import unittest

from monitorctl.ddc.edid import EdidInfo, parse_edid
from monitorctl.ddc.quirks import QUIRKS, quirks_for
from monitorctl.ddc.vcp import FEATURES, FeatureKind
from monitorctl.ddc.windows import _instance_path

TCL_25G64 = bytes.fromhex(
    "00FFFFFFFFFFFF00506C0310FFFFFFFF16240104B5361F783B0230AF4F38B2270D5054"
    "A56B00D1C0B3000101010181008180814081C0023A801871382D40582C45001C362100"
    "001E000000FF0032453637354141303231313234000000FD0E302DFFFF6C010A202020"
    "202020000000FC0032354736340A2020202020202002CC"
)

ASUS_VS248 = bytes.fromhex(
    "00FFFFFFFFFFFF0004699824010101011D1B010380351E78EA9265A655559F280D5054"
    "BFEF00714F818081409500A940B300D1C00101023A801871382D40582C4500132B2100"
    "001E000000FD00324C1E5311000A202020202020000000FC0056533234380A20202020"
    "202020000000FF0048374C4D51533034353735370A018F"
)


def with_checksum(block: bytearray) -> bytes:
    """Fix byte 127 so the block sums to zero modulo 256."""
    block[127] = (-sum(block[:127])) % 256
    return bytes(block)


class TestParseEdid(unittest.TestCase):
    def test_real_tcl_block(self) -> None:
        edid = parse_edid(TCL_25G64)
        self.assertEqual(
            edid,
            EdidInfo(
                manufacturer="TCL",
                product_code=0x1003,
                serial_number=0xFFFFFFFF,
                serial_text="2E675AA021124",
                name="25G64",
            ),
        )
        self.assertEqual(edid.model_key, "TCL-1003")
        self.assertEqual(edid.hardware_id, "TCL-1003-2E675AA021124")

    def test_real_asus_block(self) -> None:
        edid = parse_edid(ASUS_VS248)
        self.assertEqual(edid.manufacturer, "ACI")
        self.assertEqual(edid.product_code, 0x2498)
        self.assertEqual(edid.name, "VS248")
        self.assertEqual(edid.hardware_id, "ACI-2498-H7LMQS045757")

    def test_extension_blocks_are_ignored(self) -> None:
        self.assertEqual(parse_edid(TCL_25G64 + bytes(128)), parse_edid(TCL_25G64))

    def test_short_block_is_rejected(self) -> None:
        self.assertIsNone(parse_edid(TCL_25G64[:127]))
        self.assertIsNone(parse_edid(b""))

    def test_bad_header_is_rejected(self) -> None:
        block = bytearray(TCL_25G64)
        block[1] = 0x00
        self.assertIsNone(parse_edid(with_checksum(block)))

    def test_bad_checksum_is_rejected(self) -> None:
        """A garbled identity could match the wrong saved monitor."""
        block = bytearray(TCL_25G64)
        block[127] ^= 0xFF
        self.assertIsNone(parse_edid(bytes(block)))

    def test_invalid_manufacturer_letters_are_rejected(self) -> None:
        block = bytearray(TCL_25G64)
        block[8:10] = b"\x00\x00"  # letter value 0 is outside A..Z
        self.assertIsNone(parse_edid(with_checksum(block)))

    def test_numeric_serial_is_used_when_there_is_no_text_serial(self) -> None:
        block = bytearray(TCL_25G64)
        block[12:16] = (0x12345678).to_bytes(4, "little")
        block[75] = 0x10  # retag the serial descriptor as "dummy"
        edid = parse_edid(with_checksum(block))
        self.assertEqual(edid.serial_text, "")
        self.assertEqual(edid.hardware_id, "TCL-1003-12345678")

    def test_placeholder_serial_leaves_only_the_model(self) -> None:
        """Identical serial-less panels share an id; matching treats it as ambiguous."""
        block = bytearray(TCL_25G64)
        block[75] = 0x10
        edid = parse_edid(with_checksum(block))
        self.assertEqual(edid.hardware_id, "TCL-1003")

    def test_control_characters_never_reach_the_name(self) -> None:
        block = bytearray(TCL_25G64)
        # Name descriptor payload starts at 108 + 5.
        block[113:118] = b"A\x1b[2J"
        edid = parse_edid(with_checksum(block))
        self.assertEqual(edid.name, "A[2J")


class TestInstancePath(unittest.TestCase):
    def test_interface_name_maps_to_the_enum_key(self) -> None:
        self.assertEqual(
            _instance_path(
                "\\\\?\\DISPLAY#TCL1003#5&2a0761ac&0&UID4353#"
                "{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"
            ),
            "DISPLAY\\TCL1003\\5&2a0761ac&0&UID4353",
        )

    def test_unexpected_shapes_are_refused(self) -> None:
        for name in (
            "",
            "DISPLAY#TCL1003#5&2a&0&UID1#{guid}",  # no \\?\ prefix
            "\\\\?\\DISPLAY#TCL1003#{guid}",  # too few parts
            "\\\\?\\DISPLAY#..\\..\\SAM#x#{guid}",  # walks out of the key
            "\\\\?\\DISPLAY##x#{guid}",  # empty part
        ):
            with self.subTest(name=name):
                self.assertIsNone(_instance_path(name))


class TestQuirks(unittest.TestCase):
    def test_tcl_25g64_is_recognised(self) -> None:
        quirks = quirks_for(parse_edid(TCL_25G64))
        self.assertIsNotNone(quirks)
        self.assertIn("red_gain", quirks.volatile)

    def test_other_monitors_have_no_quirks(self) -> None:
        self.assertIsNone(quirks_for(parse_edid(ASUS_VS248)))
        self.assertIsNone(quirks_for(None))

    def test_every_volatile_control_is_a_known_continuous_feature(self) -> None:
        """Restoring one steps to a neighbouring value; a choice has no safe one."""
        for quirk in QUIRKS.values():
            for key in quirk.volatile:
                with self.subTest(model=quirk.model, key=key):
                    self.assertIn(key, FEATURES)
                    self.assertIs(FEATURES[key].kind, FeatureKind.CONTINUOUS)


if __name__ == "__main__":
    unittest.main()
