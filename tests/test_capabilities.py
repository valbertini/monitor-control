"""Tests for the capabilities string parser.

The parser's contract is that it never raises, so most of these cases feed it
deliberately broken input of the kind a noisy I2C bus actually produces.
"""

from __future__ import annotations

import unittest

from monitorctl.ddc.capabilities import parse_capabilities

WELL_FORMED = (
    "(prot(monitor)type(LCD)model(VS248)cmds(01 02 03 07 0C E3 F3)"
    "vcp(02 04 06 08 10 12 14(05 08 0B) 16 18 1A 60(01 03 11) AC AE B6 C6 C8 DF)"
    "mswhql(1)asset_eep(40)mccs_ver(2.1))"
)


class TestWellFormed(unittest.TestCase):
    def setUp(self) -> None:
        self.caps = parse_capabilities(WELL_FORMED)

    def test_extracts_metadata(self) -> None:
        self.assertEqual(self.caps.model, "VS248")
        self.assertEqual(self.caps.display_type, "LCD")
        self.assertEqual(self.caps.protocol, "monitor")
        self.assertEqual(self.caps.mccs_version, "2.1")

    def test_extracts_commands(self) -> None:
        self.assertEqual(
            self.caps.commands, frozenset({0x01, 0x02, 0x03, 0x07, 0x0C, 0xE3, 0xF3})
        )

    def test_continuous_codes_have_no_values(self) -> None:
        self.assertEqual(self.caps.vcp[0x10], ())
        self.assertEqual(self.caps.vcp[0x12], ())

    def test_non_continuous_codes_keep_their_value_lists(self) -> None:
        self.assertEqual(self.caps.vcp[0x14], (0x05, 0x08, 0x0B))
        self.assertEqual(self.caps.vcp[0x60], (0x01, 0x03, 0x11))

    def test_supports_and_permitted_values(self) -> None:
        self.assertIn(0x10, self.caps.vcp)
        self.assertNotIn(0x62, self.caps.vcp)
        self.assertIsNone(self.caps.permitted_values(0x10))
        self.assertEqual(self.caps.permitted_values(0x60), frozenset({1, 3, 0x11}))

    def test_clean_input_produces_no_warnings(self) -> None:
        self.assertEqual(self.caps.warnings, ())

    def test_not_empty(self) -> None:
        self.assertFalse(self.caps.is_empty)


class TestMalformedInput(unittest.TestCase):
    """Every case here must return, never raise."""

    def test_empty_string(self) -> None:
        caps = parse_capabilities("")
        self.assertTrue(caps.is_empty)
        self.assertIn("empty", caps.warnings[0])

    def test_whitespace_only(self) -> None:
        self.assertTrue(parse_capabilities("   \n ").is_empty)

    def test_truncated_mid_vcp(self) -> None:
        caps = parse_capabilities("(prot(monitor)vcp(10 12 14(05 08")
        # Everything readable before the truncation must survive.
        self.assertIn(0x10, caps.vcp)
        self.assertIn(0x12, caps.vcp)
        self.assertTrue(caps.warnings)

    def test_unbalanced_outer_paren(self) -> None:
        caps = parse_capabilities("(vcp(10 12)")
        self.assertIn(0x10, caps.vcp)
        self.assertTrue(any("unclosed" in w for w in caps.warnings))

    def test_missing_leading_paren(self) -> None:
        caps = parse_capabilities("vcp(10 12)")
        self.assertIn(0x10, caps.vcp)
        self.assertTrue(any("does not start" in w for w in caps.warnings))

    def test_non_ascii_bytes_are_dropped_and_reported(self) -> None:
        caps = parse_capabilities("(vcp(10 \x00\xff 12))")
        self.assertIn(0x10, caps.vcp)
        self.assertIn(0x12, caps.vcp)
        self.assertTrue(any("non-ASCII" in w for w in caps.warnings))

    def test_garbage_tokens_are_reported_not_fatal(self) -> None:
        caps = parse_capabilities("(vcp(10 ZZ 12))")
        self.assertIn(0x10, caps.vcp)
        self.assertIn(0x12, caps.vcp)
        self.assertTrue(any("invalid token" in w for w in caps.warnings))

    def test_stray_closing_paren_is_ignored(self) -> None:
        caps = parse_capabilities("(vcp(10 ) 12))")
        self.assertIn(0x10, caps.vcp)

    def test_value_list_without_a_code(self) -> None:
        caps = parse_capabilities("(vcp((01 02) 10))")
        self.assertIn(0x10, caps.vcp)
        self.assertTrue(any("no VCP code" in w for w in caps.warnings))

    def test_oversized_input_is_capped(self) -> None:
        caps = parse_capabilities("(vcp(10 " + "12 " * 50_000 + "))")
        self.assertTrue(any("truncated" in w for w in caps.warnings))

    def test_deeply_nested_garbage_terminates(self) -> None:
        caps = parse_capabilities("(" * 200 + "vcp(10)" + ")" * 200)
        self.assertIsNotNone(caps)

    def test_an_outer_paren_closed_early_does_not_drop_the_rest(self) -> None:
        """One lost '(' must not silently cost the whole vcp list."""
        caps = parse_capabilities("(prot(monitor)type LCD)model(X)vcp(10 12 14(05 08)))")
        self.assertEqual(caps.model, "X")
        self.assertEqual(caps.vcp[0x14], (0x05, 0x08))
        self.assertIn(0x10, caps.vcp)
        self.assertTrue(any("after the outer parenthesis" in w for w in caps.warnings))

    def test_junk_before_a_segment_name_is_not_part_of_it(self) -> None:
        caps = parse_capabilities("(type LCD model(X))")
        self.assertEqual(caps.model, "X")

    def test_trailing_code_without_values_is_kept(self) -> None:
        caps = parse_capabilities("(vcp(10 12 60))")
        self.assertEqual(caps.vcp[0x60], ())


class TestRealWorldQuirks(unittest.TestCase):
    """Cases captured from actual monitors rather than from the standard."""

    #: Verbatim from a TCL 25G64.  Note the missing opening parenthesis and the
    #: run-together value list in ``60(11120F)``.
    TCL_25G64 = (
        "vcp(02 0B 0C 10 12 14(05 06 08 0B) 16 18 1A AC AE C8 DF 62 8D "
        "60(11120F)FF)mswhql(1)mccs_ver(2.0)asset_eep(32)mpu_ver(01))"
    )

    def setUp(self) -> None:
        self.caps = parse_capabilities(self.TCL_25G64)

    def test_concatenated_value_list_is_split_into_bytes(self) -> None:
        self.assertEqual(self.caps.vcp[0x60], (0x11, 0x12, 0x0F))

    def test_permitted_inputs_are_recovered(self) -> None:
        self.assertEqual(self.caps.permitted_values(0x60), frozenset({0x11, 0x12, 0x0F}))

    def test_missing_outer_paren_does_not_lose_data(self) -> None:
        self.assertEqual(self.caps.mccs_version, "2.0")
        self.assertIn(0x10, self.caps.vcp)
        self.assertEqual(self.caps.vcp[0x14], (0x05, 0x06, 0x08, 0x0B))

    def test_no_invalid_token_warning_remains(self) -> None:
        self.assertFalse(
            [w for w in self.caps.warnings if "invalid token" in w],
            self.caps.warnings,
        )

    def test_reserved_code_advertised_by_the_monitor_is_still_parsed(self) -> None:
        # The monitor advertises 0xFF, which lives in the manufacturer-reserved
        # range.  Parsing it is fine; the write policy is what refuses it.
        self.assertIn(0xFF, self.caps.vcp)

    def test_concatenated_codes_at_top_level_are_split(self) -> None:
        caps = parse_capabilities("(vcp(10121A 60(0F)))")
        self.assertEqual(set(caps.vcp), {0x10, 0x12, 0x1A, 0x60})
        self.assertEqual(caps.vcp[0x60], (0x0F,))
        self.assertFalse([w for w in caps.warnings if "invalid token" in w])

    def test_odd_length_run_is_still_reported_as_invalid(self) -> None:
        caps = parse_capabilities("(vcp(10 ABC))")
        self.assertTrue(any("invalid token" in w for w in caps.warnings))


class TestCaseInsensitivity(unittest.TestCase):
    def test_uppercase_segment_names(self) -> None:
        caps = parse_capabilities("(PROT(monitor)VCP(10 12)MODEL(X1))")
        self.assertEqual(caps.model, "X1")
        self.assertIn(0x10, caps.vcp)

    def test_lowercase_hex_digits(self) -> None:
        caps = parse_capabilities("(vcp(ac ae df))")
        self.assertEqual(set(caps.vcp), {0xAC, 0xAE, 0xDF})


if __name__ == "__main__":
    unittest.main()
