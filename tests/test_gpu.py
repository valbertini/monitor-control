"""Tests for the GPU-side adjustment layer.

The NVAPI and gamma-ramp backends need a real driver, so the parts that can be
tested without hardware are: the gamma curve maths, the controller's routing
and degradation, and the way GPU adjustments surface through MonitorService.
"""

from __future__ import annotations

import ctypes
import unittest
from unittest import mock

from monitorctl.ddc.errors import DdcUnsupportedFeature, FeatureNotFound
from monitorctl.ddc.service import MonitorService, Pacing, Reassert
from monitorctl.ddc.vcp import CATEGORIES
from monitorctl.gpu import GpuAdjustment, GpuBackend, GpuController, GpuReading
from monitorctl.gpu.errors import GpuError, GpuUnavailable
from monitorctl.gpu.gamma import build_ramp

from .fakes import FakeBackend, FakeMonitor

NO_PACING = Pacing(
    between_io=0.0, after_write=0.0, after_capabilities=0.0, retry_backoff=0.0
)


class FakeGpuBackend(GpuBackend):
    """In-memory stand-in for a driver."""

    name = "fake"

    def __init__(
        self, devices=(r"\\.\DISPLAY1",), usable: bool = True, writable: bool = True
    ) -> None:
        self._devices = tuple(devices)
        self._usable = usable
        self._writable = writable
        self.values = {"gpu_fake_saturation": 50}
        self.closed = False
        self.refreshes = 0

    def refresh(self) -> None:
        self.refreshes += 1

    def available(self) -> bool:
        return self._usable

    def devices(self) -> tuple[str, ...]:
        return self._devices

    def adjustments(self) -> tuple[GpuAdjustment, ...]:
        return (
            GpuAdjustment(
                key="gpu_fake_saturation",
                label="Fake saturation",
                category="gpu_color",
                tooltip="test only",
                minimum=0,
                maximum=100,
                neutral=50,
                source="fake driver",
                writable=self._writable,
            ),
        )

    def get(self, device: str, key: str) -> GpuReading:
        if device not in self._devices:
            raise GpuError(f"unknown device {device!r}")
        return GpuReading(current=self.values[key], minimum=0, maximum=100)

    def set(self, device: str, key: str, value: int) -> int:
        if device not in self._devices:
            raise GpuError(f"unknown device {device!r}")
        applied = max(0, min(100, value))
        self.values[key] = applied
        return applied

    def close(self) -> None:
        self.closed = True


class TestGammaCurve(unittest.TestCase):
    """The curve maths is the part that can be got wrong silently."""

    def test_neutral_settings_produce_an_identity_ramp(self) -> None:
        curve = build_ramp(50, 50, 50)
        self.assertEqual(curve[0], 0)
        self.assertEqual(curve[-1], 65535)
        # Mid-point should land near half scale for a linear curve.
        self.assertAlmostEqual(curve[128] / 65535, 128 / 255, places=2)

    def test_curve_is_always_monotonic(self) -> None:
        for brightness in (0, 25, 50, 75, 100):
            for contrast in (0, 50, 100):
                for gamma in (0, 50, 100):
                    curve = build_ramp(brightness, contrast, gamma)
                    self.assertEqual(
                        curve,
                        sorted(curve),
                        f"non-monotonic at b={brightness} c={contrast} g={gamma}",
                    )

    def test_curve_always_stays_in_range(self) -> None:
        for value in (0, 100):
            for curve in (
                build_ramp(value, 50, 50),
                build_ramp(50, value, 50),
                build_ramp(50, 50, value),
            ):
                self.assertEqual(len(curve), 256)
                self.assertGreaterEqual(min(curve), 0)
                self.assertLessEqual(max(curve), 65535)

    def test_higher_gamma_lifts_midtones_without_moving_the_ends(self) -> None:
        low = build_ramp(50, 50, 20)
        high = build_ramp(50, 50, 80)
        self.assertLess(low[128], high[128])
        self.assertEqual(low[0], high[0])
        self.assertEqual(low[-1], high[-1])

    def test_brightness_shifts_the_whole_curve(self) -> None:
        dark = build_ramp(20, 50, 50)
        bright = build_ramp(80, 50, 50)
        self.assertLess(dark[128], bright[128])
        # A raised curve lifts black off zero; that is the giveaway that this
        # is not a backlight control.
        self.assertGreater(bright[0], 0)

    def test_extremes_never_produce_a_flat_curve(self) -> None:
        # A flat channel is what Windows rejects outright, so the conservative
        # ranges must never generate one.
        for curve in (build_ramp(0, 0, 0), build_ramp(100, 100, 100)):
            self.assertGreater(max(curve) - min(curve), 10000)


class FakeGdi:
    """The four gdi32 calls the gamma backend makes, over in-memory ramps."""

    def __init__(self, device: str, ramp) -> None:
        self.ramps = {device: ramp}
        self.writes = 0
        self._handles: dict[int, str] = {}

    def CreateDCW(self, _driver, device, _output, _init):
        handle = len(self._handles) + 1
        self._handles[handle] = device
        return handle

    def DeleteDC(self, _hdc):
        return True

    def GetDeviceGammaRamp(self, hdc, ref):
        ctypes.memmove(ref._obj, self.ramps[self._handles[hdc]], ctypes.sizeof(ref._obj))
        return True

    def SetDeviceGammaRamp(self, hdc, ref):
        target = self.ramps[self._handles[hdc]]
        ctypes.memmove(target, ref._obj, ctypes.sizeof(target))
        self.writes += 1
        return True


class TestGammaRampOwnership(unittest.TestCase):
    """The ramp is shared with ICC calibration, Night Light and f.lux."""

    DEVICE = r"\\.\DISPLAY2"

    def setUp(self) -> None:
        from monitorctl.gpu import gamma

        # Someone else's ramp: Night Light pulling blue down.
        self.foreign = gamma._RampArray()
        for channel in range(3):
            for index in range(256):
                scale = 0.6 if channel == 2 else 1.0
                self.foreign[channel][index] = round(index * 257 * scale)
        self.gdi = FakeGdi(self.DEVICE, gamma._RampArray.from_buffer_copy(self.foreign))
        self.backend = gamma.GammaRampBackend()
        self.backend._gdi = self.gdi
        self.backend._available = True
        self.key = gamma.KEY_BRIGHTNESS

    def device_ramp(self) -> list[int]:
        return list(self.gdi.ramps[self.DEVICE][2])

    def test_neutral_writes_nothing(self) -> None:
        """A profile saved at neutral, re-applied on every unlock."""
        self.backend.set(self.DEVICE, self.key, 50)
        self.assertEqual(self.gdi.writes, 0)
        self.assertEqual(self.device_ramp(), list(self.foreign[2]))

    def test_leaving_neutral_takes_the_ramp_over(self) -> None:
        self.backend.set(self.DEVICE, self.key, 70)
        self.assertEqual(self.gdi.writes, 1)
        self.assertNotEqual(self.device_ramp(), list(self.foreign[2]))

    def test_returning_to_neutral_gives_the_original_ramp_back(self) -> None:
        self.backend.set(self.DEVICE, self.key, 70)
        self.backend.set(self.DEVICE, self.key, 50)
        self.assertEqual(self.device_ramp(), list(self.foreign[2]))
        self.gdi.writes = 0
        self.backend.set(self.DEVICE, self.key, 50)  # and lets go of it
        self.assertEqual(self.gdi.writes, 0)

    def test_a_failed_give_back_keeps_the_original_for_the_next_try(self) -> None:
        self.backend.set(self.DEVICE, self.key, 70)
        real_set = self.gdi.SetDeviceGammaRamp
        self.gdi.SetDeviceGammaRamp = lambda hdc, ref: False
        with self.assertRaises(GpuError):
            self.backend.set(self.DEVICE, self.key, 50)
        self.gdi.SetDeviceGammaRamp = real_set
        self.backend.set(self.DEVICE, self.key, 50)
        self.assertEqual(self.device_ramp(), list(self.foreign[2]))

    def test_exiting_gives_the_original_ramp_back(self) -> None:
        """Left behind, the next start would take our ramp for the device's."""
        self.backend.set(self.DEVICE, self.key, 70)
        self.backend.close()
        self.assertEqual(self.device_ramp(), list(self.foreign[2]))

    def test_a_ramp_replaced_behind_our_back_is_the_new_original(self) -> None:
        """A driver reset, Night Light, or another monitor on this name."""
        from monitorctl.gpu import gamma

        self.backend.set(self.DEVICE, self.key, 70)
        reset = gamma._RampArray()
        for channel in range(3):
            for index in range(256):
                reset[channel][index] = index * 257
        ctypes.memmove(self.gdi.ramps[self.DEVICE], reset, ctypes.sizeof(reset))

        self.backend.set(self.DEVICE, self.key, 80)  # takes over again
        self.backend.set(self.DEVICE, self.key, 50)
        self.assertEqual(self.device_ramp(), list(reset[2]))

    def test_a_ramp_replaced_behind_our_back_no_longer_reads_as_applied(self) -> None:
        """Otherwise Restore compares against the remembered value, finds it
        "already correct" and leaves the screen without the adjustment."""
        self.backend.set(self.DEVICE, self.key, 70)
        self.assertEqual(self.backend.get(self.DEVICE, self.key).current, 70)
        ctypes.memmove(
            self.gdi.ramps[self.DEVICE], self.foreign, ctypes.sizeof(self.foreign)
        )
        self.assertEqual(self.backend.get(self.DEVICE, self.key).current, 50)
        self.gdi.writes = 0
        self.backend.close()  # theirs now: nothing to give back
        self.assertEqual(self.gdi.writes, 0)

    def test_a_ramp_no_longer_ours_is_left_alone_on_the_way_back(self) -> None:
        self.backend.set(self.DEVICE, self.key, 70)
        ctypes.memmove(
            self.gdi.ramps[self.DEVICE], self.foreign, ctypes.sizeof(self.foreign)
        )
        self.gdi.writes = 0
        self.backend.set(self.DEVICE, self.key, 50)
        self.assertEqual(self.gdi.writes, 0)


class TestController(unittest.TestCase):
    def test_unavailable_backends_are_dropped(self) -> None:
        controller = GpuController([FakeGpuBackend(usable=False)])
        self.assertEqual(controller.adjustments_for(r"\\.\DISPLAY1"), ())

    def test_adjustments_are_scoped_to_the_device(self) -> None:
        controller = GpuController([FakeGpuBackend(devices=(r"\\.\DISPLAY1",))])
        self.assertEqual(len(controller.adjustments_for(r"\\.\DISPLAY1")), 1)
        self.assertEqual(controller.adjustments_for(r"\\.\DISPLAY2"), ())

    def test_empty_device_name_yields_nothing(self) -> None:
        controller = GpuController([FakeGpuBackend()])
        self.assertEqual(controller.adjustments_for(""), ())

    def test_unknown_key_raises(self) -> None:
        controller = GpuController([FakeGpuBackend()])
        with self.assertRaises(GpuUnavailable):
            controller.get(r"\\.\DISPLAY1", "gpu_nonsense")

    def test_non_integer_write_is_refused(self) -> None:
        controller = GpuController([FakeGpuBackend()])
        for bad in (1.5, "50", True):
            with self.assertRaises(GpuError, msg=repr(bad)):
                controller.set(r"\\.\DISPLAY1", "gpu_fake_saturation", bad)

    def test_close_closes_every_backend_and_is_idempotent(self) -> None:
        backend = FakeGpuBackend()
        controller = GpuController([backend])
        controller.close()
        controller.close()
        self.assertTrue(backend.closed)


class TestRefreshAndReadOnly(unittest.TestCase):
    def make(self, backend: FakeGpuBackend) -> MonitorService:
        service = MonitorService(
            FakeBackend([FakeMonitor(device=r"\\.\DISPLAY1", values={0x10: 50})]),
            pacing=NO_PACING,
            gpu=GpuController([backend]),
        )
        self.addCleanup(service.close)
        return service

    def test_every_enumeration_renews_the_gpu_handles(self) -> None:
        """A power cycle invalidates NVAPI handles as much as DDC ones."""
        backend = FakeGpuBackend()
        service = self.make(backend)
        service.refresh(probe=False)
        service.refresh(probe=False)
        self.assertEqual(backend.refreshes, 2)

    def test_a_refresh_failure_does_not_fail_the_enumeration(self) -> None:
        backend = FakeGpuBackend()
        backend.refresh = mock.Mock(side_effect=RuntimeError("driver hiccup"))
        service = self.make(backend)
        with self.assertLogs("monitorctl.gpu.controller", level="WARNING"):
            states = service.refresh(probe=False)
        self.assertEqual(len(states), 1)

    def test_upkeep_leaves_a_neutral_adjustment_alone(self) -> None:
        """Saved at neutral means not ours: the NVIDIA panel's value stays."""
        backend = FakeGpuBackend()
        service = self.make(backend)
        service.save_snapshot()  # fake saturation at 50, its neutral
        backend.values["gpu_fake_saturation"] = 70  # set elsewhere since
        report = service.reassert_saved(reassert=Reassert.PIVOT)
        self.assertEqual(backend.values["gpu_fake_saturation"], 70)
        self.assertEqual(report.applied, ())

    def test_upkeep_still_reapplies_an_adjustment_the_user_made(self) -> None:
        backend = FakeGpuBackend()
        service = self.make(backend)
        monitor = service.list_monitors()[0]
        service.write_feature(monitor.id, "gpu_fake_saturation", 65)
        service.save_snapshot()
        backend.values["gpu_fake_saturation"] = 50  # driver reload reset it
        service.reassert_saved(reassert=Reassert.PIVOT)
        self.assertEqual(backend.values["gpu_fake_saturation"], 65)

    def test_a_read_only_adjustment_is_shown_but_not_writable(self) -> None:
        service = self.make(FakeGpuBackend(writable=False))
        monitor = service.list_monitors()[0]
        feature = monitor.feature("gpu_fake_saturation")
        self.assertTrue(feature.supported)
        self.assertFalse(feature.writable)
        with self.assertRaises(DdcUnsupportedFeature):
            service.write_feature(monitor.id, "gpu_fake_saturation", 60)


class TestServiceIntegration(unittest.TestCase):
    """GPU adjustments must appear alongside the DDC ones, clearly labelled."""

    def setUp(self) -> None:
        self.monitor = FakeMonitor(
            device=r"\\.\DISPLAY1",
            capabilities="(prot(monitor)model(TestDisplay)vcp(10))",
            values={0x10: 50},
            maxima={0x10: 100},
        )
        self.gpu = FakeGpuBackend(devices=(r"\\.\DISPLAY1",))
        self.service = MonitorService(
            FakeBackend([self.monitor]),
            pacing=NO_PACING,
            gpu=GpuController([self.gpu]),
        )
        self.addCleanup(self.service.close)

    def state(self):
        return self.service.list_monitors()[0]

    def test_gpu_adjustment_appears_as_a_feature(self) -> None:
        feature = self.state().feature("gpu_fake_saturation")
        self.assertIsNotNone(feature)
        self.assertTrue(feature.supported)
        self.assertEqual(feature.value, 50)
        self.assertEqual(feature.maximum, 100)

    def test_gpu_features_are_labelled_with_their_source(self) -> None:
        state = self.state()
        self.assertEqual(state.feature("gpu_fake_saturation").source, "fake driver")
        self.assertEqual(state.feature("brightness").source, "DDC/CI")

    def test_gpu_features_have_no_vcp_code(self) -> None:
        self.assertIsNone(self.state().feature("gpu_fake_saturation").code)
        self.assertEqual(self.state().feature("brightness").code, 0x10)

    def test_gpu_category_is_known_to_the_registry(self) -> None:
        category = self.state().feature("gpu_fake_saturation").category
        self.assertIn(category, CATEGORIES)

    def test_writing_a_gpu_feature_reaches_the_backend(self) -> None:
        monitor_id = self.state().id
        outcome = self.service.write_feature(monitor_id, "gpu_fake_saturation", 80)
        self.assertEqual(outcome.applied, 80)
        self.assertEqual(outcome.confirmed, 80)
        self.assertEqual(self.gpu.values["gpu_fake_saturation"], 80)

    def test_gpu_write_is_clamped_and_reported(self) -> None:
        monitor_id = self.state().id
        outcome = self.service.write_feature(monitor_id, "gpu_fake_saturation", 500)
        self.assertEqual(outcome.applied, 100)
        self.assertTrue(any("clamped" in w for w in outcome.warnings))

    def test_gpu_write_updates_the_cache(self) -> None:
        monitor_id = self.state().id
        self.service.write_feature(monitor_id, "gpu_fake_saturation", 30)
        self.assertEqual(self.state().feature("gpu_fake_saturation").value, 30)

    def test_gpu_writes_do_not_consume_the_ddc_write_budget(self) -> None:
        """They never touch the bus, so EEPROM protection does not apply."""
        monitor_id = self.state().id
        before = self.service.write_budget_available
        for value in range(10, 60, 10):
            self.service.write_feature(monitor_id, "gpu_fake_saturation", value)
        self.assertEqual(self.service.write_budget_available, before)

    def test_unknown_gpu_key_raises_feature_not_found(self) -> None:
        monitor_id = self.state().id
        with self.assertRaises(FeatureNotFound):
            self.service.write_feature(monitor_id, "gpu_nonsense", 1)

    def test_gpu_feature_absent_on_another_output_is_not_listed(self) -> None:
        other = FakeMonitor(
            device=r"\\.\DISPLAY9",
            capabilities="(prot(monitor)vcp(10))",
            values={0x10: 50},
            maxima={0x10: 100},
        )
        with MonitorService(
            FakeBackend([other]),
            pacing=NO_PACING,
            gpu=GpuController([FakeGpuBackend(devices=(r"\\.\DISPLAY1",))]),
        ) as service:
            state = service.list_monitors()[0]
            self.assertIsNone(state.feature("gpu_fake_saturation"))

    def test_read_feature_works_for_gpu_keys(self) -> None:
        monitor_id = self.state().id
        self.gpu.values["gpu_fake_saturation"] = 77
        fresh = self.service.read_feature(monitor_id, "gpu_fake_saturation")
        self.assertEqual(fresh.value, 77)

    def test_backend_failure_surfaces_as_unsupported_not_a_crash(self) -> None:
        broken = FakeGpuBackend(devices=(r"\\.\DISPLAY1",))

        def boom(device, key):
            raise GpuError("driver said no")

        broken.get = boom  # type: ignore[method-assign]
        with MonitorService(
            FakeBackend([self.monitor]),
            pacing=NO_PACING,
            gpu=GpuController([broken]),
        ) as service:
            feature = service.list_monitors()[0].feature("gpu_fake_saturation")
            self.assertFalse(feature.supported)
            self.assertIn("driver said no", feature.error)
            with self.assertRaises(DdcUnsupportedFeature):
                service.write_feature(
                    service.list_monitors()[0].id, "gpu_fake_saturation", 10
                )


if __name__ == "__main__":
    unittest.main()
