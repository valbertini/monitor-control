"""Tests for the policy that decides when saved settings are pushed again.

What is pinned here is as much about what does *not* happen as what does: the
keeper runs every minute for as long as the machine is on, so a check that
writes when nothing changed, or reads a monitor that has no reason to be read,
is a defect even if the screen looks right.
"""

from __future__ import annotations

import unittest

from monitorctl.app.keeper import RESCAN_WHILE_LOST_SECONDS, Event, Keeper
from monitorctl.app.settings import AppSettings, RefreshMode
from monitorctl.ddc.edid import EdidInfo
from monitorctl.ddc.profiles import ProfileStore
from monitorctl.ddc.service import MonitorService, Pacing
from monitorctl.gpu import GpuController

from .fakes import FakeBackend, FakeMonitor

NO_PACING = Pacing(
    between_io=0.0, after_write=0.0, after_capabilities=0.0, retry_backoff=0.0
)
CAPS = "(prot(monitor)type(LCD)model(TestDisplay)vcp(10 12 16)mccs_ver(2.1))"
TCL = EdidInfo("TCL", 0x1003, 0xFFFFFFFF, "2E675AA021124", "25G64")
STEADY = EdidInfo("ABC", 0x0001, 0, "SERIAL-A", "Steady")


def make_monitor(edid: EdidInfo, device: str) -> FakeMonitor:
    return FakeMonitor(
        device=device,
        capabilities=CAPS,
        values={0x10: 40, 0x12: 70, 0x16: 60},
        maxima={0x10: 100, 0x12: 100, 0x16: 100},
        edid=edid,
        ignores_same_value=frozenset({0x16}) if edid is TCL else frozenset(),
    )


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class KeeperTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tcl = make_monitor(TCL, r"\\.\DISPLAY1")
        self.steady = make_monitor(STEADY, r"\\.\DISPLAY2")
        self.monitors = [self.tcl, self.steady]
        self.backend = FakeBackend(self.monitors)
        self.service = MonitorService(
            self.backend,
            pacing=NO_PACING,
            gpu=GpuController([]),
            profiles=ProfileStore(),
        )
        self.addCleanup(self.service.close)
        self.service.save_snapshot()
        self.settings = AppSettings()
        self.suspended = False
        self.clock = Clock()
        self.keeper = Keeper(
            self.service,
            lambda: self.settings,
            suspended=lambda: self.suspended,
            clock=self.clock,
        )
        self.clear_traffic()

    def clear_traffic(self) -> None:
        for monitor in self.monitors:
            monitor.writes.clear()
            monitor.reads.clear()


class TestDetectMode(KeeperTestCase):
    def test_a_quiet_check_writes_nothing(self) -> None:
        self.assertIsNone(self.keeper.tick())
        self.assertEqual(self.tcl.writes, [])
        self.assertEqual(self.steady.writes, [])

    def test_a_check_reads_only_the_forgetful_monitor_once(self) -> None:
        self.keeper.tick()
        self.assertEqual(len(self.tcl.reads), 1)
        self.assertEqual(self.steady.reads, [])

    def test_a_monitor_that_comes_back_gets_its_settings_through_the_pivot(self) -> None:
        self.tcl.gone = True
        self.assertIsNone(self.keeper.tick())
        self.assertEqual(self.keeper.lost, {TCL.hardware_id})

        self.tcl.gone = False
        result = self.keeper.tick()

        self.assertIsNotNone(result)
        self.assertIn("came back", result.reason)
        self.assertEqual(self.tcl.writes, [(0x16, 50), (0x16, 60)])
        self.assertEqual(self.steady.writes, [])
        self.assertEqual(self.keeper.lost, frozenset())

    def test_it_is_pushed_once_not_on_every_later_check(self) -> None:
        self.tcl.gone = True
        self.keeper.tick()
        self.tcl.gone = False
        self.keeper.tick()
        self.clear_traffic()
        self.assertIsNone(self.keeper.tick())
        self.assertEqual(self.tcl.writes, [])

    def test_no_forgetful_monitor_means_no_traffic_at_all(self) -> None:
        self.monitors.remove(self.tcl)
        self.service.refresh(probe=False)
        self.clear_traffic()
        self.assertIsNone(self.keeper.tick())
        self.assertEqual(self.steady.reads, [])

    def test_a_monitor_that_left_the_list_is_found_by_a_slow_rescan(self) -> None:
        self.keeper.tick()  # learns about the TCL
        self.monitors.remove(self.tcl)
        self.keeper.handle_event(Event.DISPLAY_CHANGED)
        self.assertEqual(self.keeper.lost, {TCL.hardware_id})

        self.monitors.insert(0, self.tcl)
        self.clear_traffic()
        # Too soon: a monitor that is simply off is not worth re-enumerating
        # every minute.
        self.clock.now += RESCAN_WHILE_LOST_SECONDS - 1
        self.assertIsNone(self.keeper.tick())
        self.assertEqual(self.tcl.writes, [])

        self.clock.now += 1
        result = self.keeper.tick()
        self.assertTrue(result.rescanned)
        self.assertEqual(self.tcl.writes, [(0x16, 50), (0x16, 60)])

    def test_a_dead_handle_is_renewed_by_the_slow_rescan(self) -> None:
        """Still listed, but its handle died with the power cycle: pinging
        that handle forever would never see the monitor come back."""
        self.keeper.tick()
        self.backend.drop_handles()
        self.assertIsNone(self.keeper.tick())
        self.assertEqual(self.keeper.lost, {TCL.hardware_id})

        self.clock.now += RESCAN_WHILE_LOST_SECONDS
        result = self.keeper.tick()

        self.assertTrue(result.rescanned)
        self.assertIn("came back", result.reason)
        self.assertEqual(self.tcl.writes, [(0x16, 50), (0x16, 60)])
        self.assertEqual(self.keeper.lost, frozenset())

    def test_a_rescan_does_not_misdirect_a_push_to_a_renumbered_id(self) -> None:
        other = EdidInfo("TCL", 0x1003, 0xFFFFFFFF, "OTHER-SERIAL", "25G64")
        second = make_monitor(other, r"\\.\DISPLAY3")
        second.ignores_same_value = frozenset({0x16})
        self.monitors.append(second)
        self.service.refresh(probe=True)
        self.service.save_snapshot()
        self.keeper.tick()  # learns both forgetful monitors

        # The first one stops answering; the second leaves the list.
        self.tcl.gone = True
        self.keeper.tick()
        self.monitors.remove(second)
        self.keeper.handle_event(Event.DISPLAY_CHANGED)
        self.assertEqual(self.keeper.lost, {TCL.hardware_id, other.hardware_id})

        # Both return, and Windows hands the first one's device name to the
        # second: the id the first was known by now names another monitor.
        self.tcl.gone = False
        self.tcl.device, second.device = r"\\.\DISPLAY3", r"\\.\DISPLAY1"
        self.monitors.append(second)
        self.clear_traffic()
        self.clock.now += RESCAN_WHILE_LOST_SECONDS
        self.keeper.tick()

        self.assertEqual(self.tcl.writes, [(0x16, 50), (0x16, 60)])
        self.assertEqual(second.writes, [(0x16, 50), (0x16, 60)])
        self.assertEqual(self.keeper.lost, frozenset())

    def test_a_monitor_still_off_during_an_event_is_pushed_when_it_answers(
        self,
    ) -> None:
        """Off, but kept in the list by its connection: the event's push
        fails, and must not count as done."""
        self.keeper.tick()
        self.tcl.gone = True
        self.keeper.tick()

        self.keeper.handle_event(Event.SESSION_UNLOCKED)
        self.assertEqual(self.keeper.lost, {TCL.hardware_id})
        self.assertEqual(self.tcl.writes, [])

        self.tcl.gone = False
        result = self.keeper.tick()
        self.assertIn("came back", result.reason)
        self.assertEqual(self.tcl.writes, [(0x16, 50), (0x16, 60)])


class TestRewriteMode(KeeperTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = AppSettings(mode=RefreshMode.REWRITE)

    def test_every_check_pushes_through_an_invisible_neighbour(self) -> None:
        for _ in range(2):
            self.keeper.tick()
        self.assertEqual(self.tcl.writes, [(0x16, 59), (0x16, 60)] * 2)

    def test_a_monitor_that_keeps_its_settings_is_never_written(self) -> None:
        self.steady.values[0x10] = 99  # drifted, but not the keeper's business
        self.keeper.tick()
        self.assertEqual(self.steady.writes, [])

    def test_a_dead_handle_is_renewed_after_the_slow_interval(self) -> None:
        """Otherwise every later check fails on the same dead handle."""
        self.backend.drop_handles()
        result = self.keeper.tick()
        self.assertTrue(result.report.failed)
        self.assertFalse(result.rescanned)  # too soon

        self.clock.now += RESCAN_WHILE_LOST_SECONDS
        self.assertTrue(self.keeper.tick().rescanned)
        self.clear_traffic()
        self.keeper.tick()
        self.assertEqual(self.tcl.writes, [(0x16, 59), (0x16, 60)])


class TestIdenticalPanels(KeeperTestCase):
    """Two units of one model without a serial share their EDID identity."""

    def setUp(self) -> None:
        super().setUp()
        self.twin = make_monitor(TCL, r"\\.\DISPLAY3")
        self.monitors.append(self.twin)
        self.service.refresh(probe=True)
        self.service.save_snapshot()
        self.keeper.tick()
        self.clear_traffic()

    def test_one_answering_is_not_taken_for_the_other_coming_back(self) -> None:
        self.twin.gone = True
        for _ in range(3):
            self.keeper.tick()
        self.assertEqual(self.tcl.writes, [])

    def test_values_are_not_carried_from_one_unit_to_the_other(self) -> None:
        self.tcl.values[0x10] = 11
        self.twin.values[0x10] = 22
        self.service.refresh(probe=True)
        self.tcl.device, self.twin.device = self.twin.device, self.tcl.device
        states = self.service.refresh(probe=False)
        twins = [s for s in states if s.hardware_id == TCL.hardware_id]
        self.assertEqual(len(twins), 2)
        self.assertTrue(all(not s.probed for s in twins))  # read afresh, not swapped
        steady = next(s for s in states if s.hardware_id == STEADY.hardware_id)
        self.assertTrue(steady.probed)  # a unique identity still carries over


class TestWhenTheKeeperStaysOut(KeeperTestCase):
    def test_paused(self) -> None:
        self.settings = AppSettings(paused=True, mode=RefreshMode.REWRITE)
        self.assertIsNone(self.keeper.tick())
        result = self.keeper.handle_event(Event.RESUMED)
        self.assertIsNone(result.report)
        self.assertEqual(self.tcl.writes, [])

    def test_while_the_user_is_editing(self) -> None:
        """A timer pushing saved values would undo every slider they move."""
        self.settings = AppSettings(mode=RefreshMode.REWRITE)
        self.suspended = True
        self.assertIsNone(self.keeper.tick())
        result = self.keeper.handle_event(Event.DISPLAY_CHANGED)
        self.assertIsNone(result.report)
        self.assertEqual(self.tcl.writes, [])

    def test_an_event_still_renews_the_handles_while_idle(self) -> None:
        """Writing nothing is the point of pausing; holding handles that died
        with the display change is not."""
        self.suspended = True
        self.backend.drop_handles()
        result = self.keeper.handle_event(Event.DISPLAY_CHANGED)
        self.assertTrue(result.rescanned)
        self.assertTrue(self.service.ping(self.service.list_monitors()[0].id))

    def test_with_nothing_saved(self) -> None:
        service = MonitorService(
            FakeBackend([make_monitor(TCL, r"\\.\DISPLAY1")]),
            pacing=NO_PACING,
            gpu=GpuController([]),
        )
        self.addCleanup(service.close)
        keeper = Keeper(service, lambda: AppSettings(mode=RefreshMode.REWRITE))
        self.assertIsNone(keeper.tick())
        self.assertIsNone(keeper.handle_event(Event.STARTUP))

    def test_startup_respects_its_own_setting(self) -> None:
        self.settings = AppSettings(apply_on_start=False)
        self.assertIsNone(self.keeper.handle_event(Event.STARTUP))
        self.assertEqual(self.tcl.writes, [])


class TestEvents(KeeperTestCase):
    def test_an_event_rescans_and_pushes_through_the_pivot(self) -> None:
        result = self.keeper.handle_event(Event.MONITOR_ARRIVED)
        self.assertTrue(result.rescanned)
        self.assertEqual(result.reason, Event.MONITOR_ARRIVED.value)
        self.assertEqual(self.tcl.writes, [(0x16, 50), (0x16, 60)])
        self.assertEqual(self.steady.writes, [])

    def test_startup_pushes_when_enabled(self) -> None:
        self.keeper.handle_event(Event.STARTUP)
        self.assertEqual(self.tcl.writes, [(0x16, 50), (0x16, 60)])

    def test_a_monitor_renumbered_by_windows_is_still_recognised(self) -> None:
        self.tcl.device, self.steady.device = self.steady.device, self.tcl.device
        self.keeper.handle_event(Event.DISPLAY_CHANGED)
        self.assertEqual(self.tcl.writes, [(0x16, 50), (0x16, 60)])
        self.assertEqual(self.steady.writes, [])


if __name__ == "__main__":
    unittest.main()
