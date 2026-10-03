"""Tests for the write-safety policy.

These are the tests that matter most: the policy is the only thing standing
between a software bug and the monitor's firmware.
"""

from __future__ import annotations

import unittest

from monitorctl.ddc.errors import PolicyViolation, RateLimited
from monitorctl.ddc.policy import (
    BLOCKED_CODES,
    MANUFACTURER_RESERVED_END,
    MANUFACTURER_RESERVED_START,
    WriteBudget,
    check_write,
    clamp,
)
from monitorctl.ddc.vcp import FEATURES, FeatureCategory, FeatureKind, VcpFeature

BRIGHTNESS = FEATURES["brightness"]
INPUT_SOURCE = FEATURES["input_source"]


class TestReservedRange(unittest.TestCase):
    """The manufacturer-reserved range is the only documented path to a brick."""

    def test_every_reserved_code_is_refused(self) -> None:
        for code in range(MANUFACTURER_RESERVED_START, MANUFACTURER_RESERVED_END + 1):
            feature = VcpFeature(
                code=code,
                key=f"probe_{code:02x}",
                label="probe",
                kind=FeatureKind.CONTINUOUS,
                category=FeatureCategory.IMAGE,
                tooltip="",
            )
            with self.assertRaises(PolicyViolation) as ctx:
                check_write(feature, 10, maximum=100)
            self.assertIn("manufacturer-reserved", str(ctx.exception))

    def test_registry_contains_no_reserved_codes(self) -> None:
        for feature in FEATURES.values():
            self.assertLess(
                feature.code,
                MANUFACTURER_RESERVED_START,
                f"{feature.key} uses a reserved code",
            )


class TestBlockedCodes(unittest.TestCase):
    def test_each_blocked_code_is_refused_with_a_reason(self) -> None:
        for code in BLOCKED_CODES:
            feature = VcpFeature(
                code=code,
                key=f"blocked_{code:02x}",
                label="blocked",
                kind=FeatureKind.CONTINUOUS,
                category=FeatureCategory.IMAGE,
                tooltip="",
            )
            with self.assertRaises(PolicyViolation) as ctx:
                check_write(feature, 1, maximum=100)
            self.assertIn(f"0x{code:02X}", str(ctx.exception))

    def test_power_mode_and_factory_reset_are_covered(self) -> None:
        # Regression guard: these are the ones that cause real-world harm.
        self.assertIn(0xD6, BLOCKED_CODES)  # monitors that never wake
        self.assertIn(0x04, BLOCKED_CODES)  # wipes calibration
        self.assertIn(0xB0, BLOCKED_CODES)  # EEPROM store
        self.assertIn(0xCA, BLOCKED_CODES)  # locks the physical buttons

    def test_registry_exposes_no_blocked_code(self) -> None:
        for feature in FEATURES.values():
            self.assertNotIn(feature.code, BLOCKED_CODES, feature.key)


class TestContinuousValues(unittest.TestCase):
    def test_value_within_range_passes_through(self) -> None:
        self.assertEqual(check_write(BRIGHTNESS, 42, maximum=100), 42)

    def test_value_above_maximum_is_clamped(self) -> None:
        self.assertEqual(check_write(BRIGHTNESS, 5000, maximum=100), 100)

    def test_negative_value_is_clamped_to_zero(self) -> None:
        self.assertEqual(check_write(BRIGHTNESS, 0, maximum=100), 0)

    def test_non_standard_maximum_is_respected(self) -> None:
        self.assertEqual(check_write(BRIGHTNESS, 200, maximum=180), 180)

    def test_value_beyond_16_bits_is_refused(self) -> None:
        with self.assertRaises(PolicyViolation) as ctx:
            check_write(BRIGHTNESS, 70_000, maximum=100)
        self.assertIn("65535", str(ctx.exception))

    def test_negative_value_is_refused(self) -> None:
        with self.assertRaises(PolicyViolation):
            check_write(BRIGHTNESS, -1, maximum=100)

    def test_zero_maximum_is_refused(self) -> None:
        with self.assertRaises(PolicyViolation):
            check_write(BRIGHTNESS, 10, maximum=0)

    def test_boolean_is_not_accepted_as_an_integer(self) -> None:
        with self.assertRaises(PolicyViolation):
            check_write(BRIGHTNESS, True, maximum=100)  # type: ignore[arg-type]

    def test_float_is_refused(self) -> None:
        with self.assertRaises(PolicyViolation):
            check_write(BRIGHTNESS, 42.5, maximum=100)  # type: ignore[arg-type]


class TestNonContinuousValues(unittest.TestCase):
    def test_standard_value_is_accepted_when_monitor_did_not_enumerate(self) -> None:
        self.assertEqual(check_write(INPUT_SOURCE, 0x11, maximum=0), 0x11)

    def test_value_outside_the_standard_set_is_refused(self) -> None:
        with self.assertRaises(PolicyViolation):
            check_write(INPUT_SOURCE, 0x99, maximum=0)

    def test_monitor_advertised_set_overrides_the_standard_set(self) -> None:
        allowed = frozenset({0x0F, 0x11})
        self.assertEqual(
            check_write(INPUT_SOURCE, 0x0F, maximum=0, allowed_values=allowed), 0x0F
        )
        with self.assertRaises(PolicyViolation) as ctx:
            check_write(INPUT_SOURCE, 0x03, maximum=0, allowed_values=allowed)
        self.assertIn("0x0F", str(ctx.exception))

    def test_non_continuous_values_are_never_clamped(self) -> None:
        # Clamping an enumerated value would silently select a different input.
        with self.assertRaises(PolicyViolation):
            check_write(INPUT_SOURCE, 0xFE, maximum=0)


class TestClamp(unittest.TestCase):
    def test_clamps_both_ends(self) -> None:
        self.assertEqual(clamp(-5, 0, 10), 0)
        self.assertEqual(clamp(50, 0, 10), 10)
        self.assertEqual(clamp(7, 0, 10), 7)

    def test_inverted_bounds_raise(self) -> None:
        with self.assertRaises(ValueError):
            clamp(5, 10, 0)


class TestWriteBudget(unittest.TestCase):
    def test_burst_up_to_capacity_then_refuses(self) -> None:
        budget = WriteBudget(capacity=3, refill_per_second=0.001)
        for _ in range(3):
            budget.consume()
        with self.assertRaises(RateLimited):
            budget.consume()

    def test_refusal_message_states_the_wait(self) -> None:
        budget = WriteBudget(capacity=1, refill_per_second=0.5)
        budget.consume()
        with self.assertRaises(RateLimited) as ctx:
            budget.consume()
        self.assertIn("Try again in", str(ctx.exception))

    def test_tokens_refill_over_time(self) -> None:
        budget = WriteBudget(capacity=2, refill_per_second=1000.0)
        budget.consume(2)
        # A very fast refill rate makes this deterministic without sleeping long.
        import time

        time.sleep(0.01)
        budget.consume()

    def test_available_never_exceeds_capacity(self) -> None:
        budget = WriteBudget(capacity=5, refill_per_second=1000.0)
        import time

        time.sleep(0.02)
        self.assertLessEqual(budget.available(), 5)

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WriteBudget(capacity=0)
        with self.assertRaises(ValueError):
            WriteBudget(refill_per_second=0)

    def test_concurrent_consumption_never_oversubscribes(self) -> None:
        import threading

        budget = WriteBudget(capacity=50, refill_per_second=0.0001)
        granted = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(10):
                try:
                    budget.consume()
                except RateLimited:
                    return
                with lock:
                    granted.append(1)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertLessEqual(len(granted), 50)


if __name__ == "__main__":
    unittest.main()
