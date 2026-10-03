"""Tests for the orchestration layer."""

from __future__ import annotations

import time
import unittest

from monitorctl.ddc.errors import (
    DdcBackendError,
    DdcTransientError,
    DdcUnsupportedFeature,
    FeatureNotFound,
    MonitorNotFound,
    PolicyViolation,
    RateLimited,
)
from monitorctl.ddc.policy import WriteBudget
from monitorctl.ddc.service import MonitorService, Pacing
from monitorctl.ddc.vcp import FEATURES
from monitorctl.gpu import GpuController

from .fakes import FakeBackend, FakeMonitor

# Pacing is disabled in tests so the suite does not spend seconds sleeping;
# the pacing behaviour itself is covered separately in TestPacing.
NO_PACING = Pacing(
    between_io=0.0, after_write=0.0, after_capabilities=0.0, retry_backoff=0.0
)

CAPS = (
    "(prot(monitor)type(LCD)model(TestDisplay)cmds(01 02 03)"
    "vcp(10 12 60(0F 11) 14(05 08))mccs_ver(2.1))"
)

#: What the fake implements: brightness, contrast, input source, colour preset.
IMPLEMENTED = {"brightness", "contrast", "input_source", "color_preset"}


def make_monitor(**overrides) -> FakeMonitor:
    defaults = {
        "capabilities": CAPS,
        "values": {0x10: 50, 0x12: 70, 0x60: 0x11, 0x14: 0x05},
        "maxima": {0x10: 100, 0x12: 100, 0x60: 0x12, 0x14: 0x0D},
    }
    defaults.update(overrides)
    return FakeMonitor(**defaults)


def make_service(monitors, **kwargs) -> MonitorService:
    kwargs.setdefault("pacing", NO_PACING)
    # No GPU backends: these tests cover the DDC path and must not depend on
    # whatever graphics card the machine happens to have.
    kwargs.setdefault("gpu", GpuController([]))
    return MonitorService(FakeBackend(monitors), **kwargs)


class TestEnumeration(unittest.TestCase):
    def test_discovers_monitors_and_parses_model(self) -> None:
        with make_service([make_monitor()]) as service:
            monitors = service.list_monitors()
            self.assertEqual(len(monitors), 1)
            self.assertEqual(monitors[0].name, "TestDisplay")
            self.assertEqual(monitors[0].mccs_version, "2.1")

    def test_every_registry_feature_is_returned(self) -> None:
        """Unsupported features stay in the list so the UI can grey them out."""
        with make_service([make_monitor()]) as service:
            state = service.list_monitors()[0]
            self.assertEqual(len(state.features), len(FEATURES))
            self.assertEqual({f.key for f in state.features}, set(FEATURES))

    def test_supported_features_reflect_the_hardware(self) -> None:
        with make_service([make_monitor()]) as service:
            state = service.list_monitors()[0]
            self.assertEqual({f.key for f in state.supported_features}, IMPLEMENTED)

    def test_unsupported_features_carry_no_value_and_an_explanation(self) -> None:
        with make_service([make_monitor()]) as service:
            volume = service.list_monitors()[0].feature("volume")
            self.assertIsNotNone(volume)
            self.assertFalse(volume.supported)
            self.assertFalse(volume.writable)
            self.assertIsNone(volume.value)
            self.assertIn("does not implement", volume.error)

    def test_features_are_probed_even_when_not_advertised(self) -> None:
        """The capabilities string is a floor, not a ceiling."""
        monitor = make_monitor(
            capabilities="(prot(monitor)vcp(10))",  # only brightness advertised
            values={0x10: 50, 0x62: 30},  # but volume answers too
            maxima={0x10: 100, 0x62: 100},
        )
        with make_service([monitor]) as service:
            volume = service.list_monitors()[0].feature("volume")
            self.assertTrue(volume.supported)
            self.assertEqual(volume.value, 30)

    def test_each_feature_carries_its_category_and_tooltip(self) -> None:
        with make_service([make_monitor()]) as service:
            brightness = service.list_monitors()[0].feature("brightness")
            self.assertEqual(brightness.category, "luminance")
            self.assertTrue(brightness.tooltip)

    def test_falls_back_to_direct_probing_without_capabilities(self) -> None:
        monitor = make_monitor(capabilities="")
        with make_service([monitor]) as service:
            state = service.list_monitors()[0]
            self.assertEqual({f.key for f in state.supported_features}, IMPLEMENTED)
            self.assertTrue(any("direct probing" in w for w in state.warnings))

    def test_ids_are_unique_for_identical_devices(self) -> None:
        monitors = [make_monitor(), make_monitor()]
        with make_service(monitors) as service:
            ids = [m.id for m in service.list_monitors()]
            self.assertEqual(len(set(ids)), 2)

    def test_percent_is_computed_from_the_reported_maximum(self) -> None:
        monitor = make_monitor(values={0x10: 90}, maxima={0x10: 180})
        with make_service([monitor]) as service:
            brightness = service.list_monitors()[0].feature("brightness")
            self.assertEqual(brightness.value, 90)
            self.assertEqual(brightness.maximum, 180)
            self.assertEqual(brightness.percent, 50)

    def test_options_come_from_the_capabilities_string(self) -> None:
        with make_service([make_monitor()]) as service:
            source = service.list_monitors()[0].feature("input_source")
            self.assertEqual([o.value for o in source.options], [0x0F, 0x11])
            self.assertEqual(
                [o.label for o in source.options], ["DisplayPort 1", "HDMI 1"]
            )

    def test_refresh_re_enumerates(self) -> None:
        backend = FakeBackend([make_monitor()])
        with MonitorService(backend, pacing=NO_PACING, gpu=GpuController([])) as service:
            service.list_monitors()
            service.refresh()
            self.assertEqual(backend.enumerate_calls, 2)


class TestLightweightEnumeration(unittest.TestCase):
    """`probe=False` exists to keep the bus quiet; verify it really is quiet."""

    def _counting_backend(self, monitors):
        backend = FakeBackend(monitors)
        backend.get_calls = 0
        original = backend.get_vcp

        def counting(m, code):
            backend.get_calls += 1
            return original(m, code)

        backend.get_vcp = counting  # type: ignore[method-assign]
        return backend

    def test_unprobed_enumeration_issues_no_vcp_reads(self) -> None:
        backend = self._counting_backend([make_monitor()])
        with MonitorService(backend, pacing=NO_PACING, gpu=GpuController([])) as service:
            states = service.refresh(probe=False)
        self.assertEqual(backend.get_calls, 0)
        self.assertEqual(len(states), 1)

    def test_probed_enumeration_reads_every_registry_feature(self) -> None:
        backend = self._counting_backend([make_monitor()])
        with MonitorService(backend, pacing=NO_PACING, gpu=GpuController([])) as service:
            service.refresh(probe=True)
        self.assertEqual(backend.get_calls, len(FEATURES))

    def test_unprobed_features_carry_metadata_but_no_value(self) -> None:
        with make_service([make_monitor()]) as service:
            state = service.refresh(probe=False)[0]
            brightness = state.feature("brightness")
            self.assertIsNotNone(brightness)
            self.assertIsNone(brightness.value)
            self.assertIsNone(brightness.maximum)
            self.assertTrue(brightness.writable)

    def test_unprobed_non_continuous_still_lists_options(self) -> None:
        with make_service([make_monitor()]) as service:
            state = service.refresh(probe=False)[0]
            source = state.feature("input_source")
            self.assertEqual([o.value for o in source.options], [0x0F, 0x11])

    def test_write_after_unprobed_enumeration_reads_the_maximum_first(self) -> None:
        monitor = make_monitor()
        with make_service([monitor]) as service:
            state = service.refresh(probe=False)[0]
            outcome = service.write_feature(state.id, "brightness", 9999)
            # The clamp proves the maximum was fetched on demand.
            self.assertEqual(outcome.applied, 100)
            self.assertEqual(monitor.values[0x10], 100)

    def test_unprobed_write_of_a_valid_value_works(self) -> None:
        monitor = make_monitor()
        with make_service([monitor]) as service:
            state = service.refresh(probe=False)[0]
            outcome = service.write_feature(state.id, "brightness", 42)
            self.assertEqual(outcome.confirmed, 42)


class TestReads(unittest.TestCase):
    def test_read_feature_returns_current_value(self) -> None:
        with make_service([make_monitor()]) as service:
            monitor_id = service.list_monitors()[0].id
            state = service.read_feature(monitor_id, "brightness")
            self.assertEqual(state.value, 50)

    def test_reading_an_unsupported_feature_raises(self) -> None:
        with make_service([make_monitor()]) as service:
            monitor_id = service.list_monitors()[0].id
            with self.assertRaises(DdcUnsupportedFeature):
                service.read_feature(monitor_id, "volume")

    def test_unknown_monitor_raises(self) -> None:
        with (
            make_service([make_monitor()]) as service,
            self.assertRaises(MonitorNotFound),
        ):
            service.get_monitor("nope")

    def test_unknown_feature_raises_with_a_helpful_list(self) -> None:
        with make_service([make_monitor()]) as service:
            monitor_id = service.list_monitors()[0].id
            with self.assertRaises(FeatureNotFound) as ctx:
                service.read_feature(monitor_id, "nonsense")
            self.assertIn("brightness", str(ctx.exception))


class TestWrites(unittest.TestCase):
    def test_write_applies_and_confirms(self) -> None:
        monitor = make_monitor()
        with make_service([monitor]) as service:
            monitor_id = service.list_monitors()[0].id
            outcome = service.write_feature(monitor_id, "brightness", 75)
            self.assertEqual(outcome.applied, 75)
            self.assertEqual(outcome.confirmed, 75)
            self.assertEqual(outcome.warnings, ())
            self.assertEqual(monitor.values[0x10], 75)

    def test_cached_state_is_updated_after_a_write(self) -> None:
        with make_service([make_monitor()]) as service:
            monitor_id = service.list_monitors()[0].id
            service.write_feature(monitor_id, "brightness", 33)
            cached = service.get_monitor(monitor_id).feature("brightness")
            self.assertEqual(cached.value, 33)
            self.assertEqual(cached.percent, 33)

    def test_out_of_range_value_is_clamped_and_reported(self) -> None:
        monitor = make_monitor()
        with make_service([monitor]) as service:
            monitor_id = service.list_monitors()[0].id
            outcome = service.write_feature(monitor_id, "brightness", 999)
            self.assertEqual(outcome.requested, 999)
            self.assertEqual(outcome.applied, 100)
            self.assertTrue(any("clamped" in w for w in outcome.warnings))

    def test_firmware_rounding_is_surfaced_as_a_warning(self) -> None:
        monitor = make_monitor(clamp_at={0x10: 80})
        with make_service([monitor]) as service:
            monitor_id = service.list_monitors()[0].id
            outcome = service.write_feature(monitor_id, "brightness", 95)
            self.assertEqual(outcome.confirmed, 80)
            self.assertTrue(any("instead of" in w for w in outcome.warnings))

    def test_invalid_enumerated_value_never_reaches_the_backend(self) -> None:
        monitor = make_monitor()
        with make_service([monitor]) as service:
            monitor_id = service.list_monitors()[0].id
            before = len(monitor.writes)
            with self.assertRaises(PolicyViolation):
                service.write_feature(monitor_id, "input_source", 0x99)
            self.assertEqual(len(monitor.writes), before)

    def test_rejected_write_does_not_consume_budget(self) -> None:
        monitor = make_monitor()
        budget = WriteBudget(capacity=2, refill_per_second=0.0001)
        with make_service([monitor], budget=budget) as service:
            monitor_id = service.list_monitors()[0].id
            with self.assertRaises(PolicyViolation):
                service.write_feature(monitor_id, "input_source", 0x99)
            self.assertGreaterEqual(budget.available(), 2)

    def test_budget_exhaustion_stops_writes(self) -> None:
        monitor = make_monitor()
        budget = WriteBudget(capacity=2, refill_per_second=0.0001)
        with make_service([monitor], budget=budget) as service:
            monitor_id = service.list_monitors()[0].id
            service.write_feature(monitor_id, "brightness", 10)
            service.write_feature(monitor_id, "brightness", 20)
            with self.assertRaises(RateLimited):
                service.write_feature(monitor_id, "brightness", 30)
            self.assertEqual(monitor.values[0x10], 20)

    def test_writing_an_unsupported_feature_raises(self) -> None:
        with make_service([make_monitor()]) as service:
            monitor_id = service.list_monitors()[0].id
            with self.assertRaises(DdcUnsupportedFeature):
                service.write_feature(monitor_id, "volume", 10)

    def test_unsupported_write_never_reaches_the_backend(self) -> None:
        monitor = make_monitor()
        with make_service([monitor]) as service:
            monitor_id = service.list_monitors()[0].id
            with self.assertRaises(DdcUnsupportedFeature):
                service.write_feature(monitor_id, "volume", 10)
            self.assertEqual(monitor.writes, [])

    def test_non_continuous_write_selects_an_advertised_value(self) -> None:
        monitor = make_monitor()
        with make_service([monitor]) as service:
            monitor_id = service.list_monitors()[0].id
            outcome = service.write_feature(monitor_id, "input_source", 0x0F)
            self.assertEqual(outcome.confirmed, 0x0F)

    def test_verification_can_be_disabled(self) -> None:
        monitor = make_monitor()
        with make_service([monitor], verify_writes=False) as service:
            monitor_id = service.list_monitors()[0].id
            outcome = service.write_feature(monitor_id, "brightness", 60)
            self.assertIsNone(outcome.confirmed)
            self.assertEqual(monitor.values[0x10], 60)


class TestRetry(unittest.TestCase):
    def test_transient_read_failure_is_retried_once(self) -> None:
        monitor = make_monitor(transient_failures={0x10: 1})
        with make_service([monitor]) as service:
            brightness = service.list_monitors()[0].feature("brightness")
            self.assertTrue(brightness.supported)
            self.assertEqual(brightness.value, 50)

    def test_persistent_transient_failure_is_reported_not_retried_forever(self) -> None:
        monitor = make_monitor(transient_failures={0x10: 99})
        with make_service([monitor]) as service:
            brightness = service.list_monitors()[0].feature("brightness")
            self.assertIsNotNone(brightness)
            self.assertFalse(brightness.writable)
            self.assertIn("transient", brightness.error)

    def test_retry_happens_exactly_once(self) -> None:
        calls = {"n": 0}
        monitor = make_monitor()
        backend = FakeBackend([monitor])
        original = backend.get_vcp

        def counting_get(m, code):
            if code == 0x10:
                calls["n"] += 1
                raise DdcTransientError("always fails")
            return original(m, code)

        backend.get_vcp = counting_get  # type: ignore[method-assign]
        with MonitorService(backend, pacing=NO_PACING, gpu=GpuController([])) as service:
            service.list_monitors()
        self.assertEqual(calls["n"], 2)


class TestPacing(unittest.TestCase):
    def test_the_next_operation_waits_for_the_configured_gap(self) -> None:
        monitor = FakeMonitor(
            capabilities="(vcp(10))", values={0x10: 50}, maxima={0x10: 100}
        )
        # The gap is enforced *before* the following operation, so timing the
        # first one proves nothing; time the second.
        pacing = Pacing(
            between_io=0.2,
            after_write=0.2,
            after_capabilities=0.2,
            retry_backoff=0.0,
        )
        with MonitorService(
            FakeBackend([monitor]), pacing=pacing, gpu=GpuController([])
        ) as service:
            state = service.refresh(probe=False)[0]
            started = time.monotonic()
            service.read_feature(state.id, "brightness")
            elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.1)


class TestLifecycle(unittest.TestCase):
    def test_close_is_idempotent_and_closes_the_backend(self) -> None:
        backend = FakeBackend([make_monitor()])
        service = MonitorService(backend, pacing=NO_PACING, gpu=GpuController([]))
        service.list_monitors()
        service.close()
        service.close()
        self.assertTrue(backend.closed)

    def test_using_a_closed_service_raises(self) -> None:
        service = MonitorService(
            FakeBackend([make_monitor()]), pacing=NO_PACING, gpu=GpuController([])
        )
        service.close()
        with self.assertRaises(DdcBackendError):
            service.list_monitors()


if __name__ == "__main__":
    unittest.main()
