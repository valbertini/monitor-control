"""Tests for Save/Restore.

Two things are being pinned here.  The first is that a restore is *cheap and
honest*: it writes only the values that actually drifted, it reports what it
could not put back instead of failing as a whole, and it never bypasses the
write policy just because the value came from a file this program wrote itself.

The second is that the file on disk is treated as untrusted input.  It survives
reboots, users edit it, and disks corrupt it, so every path that reads it has to
survive garbage without taking the application down with it.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from monitorctl.ddc.edid import EdidInfo
from monitorctl.ddc.errors import SnapshotMissing
from monitorctl.ddc.policy import WriteBudget
from monitorctl.ddc.profiles import ProfileStore
from monitorctl.ddc.service import MonitorService, Pacing, Reassert
from monitorctl.ddc.snapshot import (
    SCHEMA_VERSION,
    MonitorSnapshot,
    Snapshot,
)
from monitorctl.gpu import GpuController

from .fakes import FakeBackend, FakeMonitor, legacy_payload

NO_PACING = Pacing(
    between_io=0.0, after_write=0.0, after_capabilities=0.0, retry_backoff=0.0
)

# Advertises brightness, contrast, colour preset (values 05 and 08 only) and
# input source, which is enough to exercise both feature kinds.
CAPS = (
    "(prot(monitor)type(LCD)model(TestDisplay)"
    "vcp(10 12 14(05 08) 16 60(0F 11))mccs_ver(2.1))"
)

#: 0x10 brightness, 0x12 contrast, 0x14 colour preset, 0x16 red gain.
VALUES = {0x10: 50, 0x12: 70, 0x14: 0x05, 0x16: 60}
MAXIMA = {0x10: 100, 0x12: 100, 0x14: 0x0D, 0x16: 100}


def make_monitor(**overrides) -> FakeMonitor:
    defaults = {
        "capabilities": CAPS,
        "values": dict(VALUES),
        "maxima": dict(MAXIMA),
    }
    defaults.update(overrides)
    return FakeMonitor(**defaults)


def make_service(monitors, store: ProfileStore | None = None, **kwargs):
    kwargs.setdefault("pacing", NO_PACING)
    kwargs.setdefault("gpu", GpuController([]))
    return MonitorService(
        FakeBackend(monitors), profiles=store or ProfileStore(), **kwargs
    )


class TestSnapshotPayload(unittest.TestCase):
    """The on-disk shape is a contract; a bad file must not reach the hardware."""

    def round_trip(self, snapshot: Snapshot) -> Snapshot:
        return Snapshot.from_payload(json.loads(json.dumps(legacy_payload(snapshot))))

    def test_round_trip_preserves_everything(self) -> None:
        original = Snapshot(
            saved_at=1_700_000_000.5,
            monitors=(
                MonitorSnapshot(
                    id="display1",
                    name="TestDisplay",
                    device=r"\\.\DISPLAY1",
                    values={"brightness": 50, "contrast": 70},
                ),
            ),
        )
        self.assertEqual(self.round_trip(original), original)

    def test_values_are_frozen_against_the_caller(self) -> None:
        values = {"brightness": 50}
        entry = MonitorSnapshot(id="a", name="a", device="a", values=values)
        values["brightness"] = 99
        self.assertEqual(entry.values["brightness"], 50)

    def test_value_count_spans_every_monitor(self) -> None:
        snapshot = Snapshot(
            saved_at=0.0,
            monitors=(
                MonitorSnapshot("a", "a", "a", {"brightness": 1}),
                MonitorSnapshot("b", "b", "b", {"brightness": 1, "contrast": 2}),
            ),
        )
        self.assertEqual(snapshot.value_count, 3)

    def test_future_version_is_refused_rather_than_guessed_at(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Snapshot.from_payload(
                {"version": SCHEMA_VERSION + 1, "savedAt": 0, "monitors": []}
            )
        self.assertIn("unsupported snapshot version", str(ctx.exception))

    def test_malformed_payloads_are_refused(self) -> None:
        base = {"version": SCHEMA_VERSION, "savedAt": 0.0, "monitors": []}
        cases = {
            "not an object": [],
            "no version": {"savedAt": 0.0, "monitors": []},
            "bad savedAt": {**base, "savedAt": "yesterday"},
            "boolean savedAt": {**base, "savedAt": True},
            "monitors not a list": {**base, "monitors": {}},
            "entry not an object": {**base, "monitors": ["display1"]},
            "id not a string": {**base, "monitors": [{"id": 1, "values": {}}]},
            "values not an object": {**base, "monitors": [{"id": "a", "values": 1}]},
            "float value": {
                **base,
                "monitors": [{"id": "a", "values": {"brightness": 50.5}}],
            },
            "boolean value": {
                **base,
                "monitors": [{"id": "a", "values": {"brightness": True}}],
            },
            "value out of protocol range": {
                **base,
                "monitors": [{"id": "a", "values": {"brightness": 70000}}],
            },
            "negative value": {
                **base,
                "monitors": [{"id": "a", "values": {"brightness": -1}}],
            },
            "key is not a feature key": {
                **base,
                "monitors": [{"id": "a", "values": {"../../etc": 1}}],
            },
        }
        for label, payload in cases.items():
            with self.assertRaises(ValueError, msg=label):
                Snapshot.from_payload(payload)

    def test_missing_optional_strings_default_to_empty(self) -> None:
        snapshot = Snapshot.from_payload(
            {
                "version": SCHEMA_VERSION,
                "savedAt": 0.0,
                "monitors": [{"id": "display1", "values": {}}],
            }
        )
        self.assertEqual(snapshot.monitors[0].name, "")
        self.assertEqual(snapshot.monitors[0].device, "")


class TestSave(unittest.TestCase):
    def test_captures_only_what_can_be_put_back(self) -> None:
        with make_service([make_monitor()]) as service:
            snapshot = service.save_snapshot()
            saved = snapshot.monitors[0]
            self.assertEqual(
                dict(saved.values),
                {"brightness": 50, "contrast": 70, "color_preset": 5, "red_gain": 60},
            )
            # Everything the monitor does not implement is absent, not null.
            self.assertNotIn("volume", saved.values)

    def test_momentary_commands_are_never_captured(self) -> None:
        """auto_setup is a trigger, not a setting: there is no state to restore."""
        monitor = make_monitor(
            values={**VALUES, 0x1E: 0x01}, maxima={**MAXIMA, 0x1E: 0x02}
        )
        with make_service([monitor]) as service:
            snapshot = service.save_snapshot()
            self.assertIn("brightness", snapshot.monitors[0].values)
            self.assertNotIn("auto_setup", snapshot.monitors[0].values)

    def test_saving_costs_no_traffic_and_no_budget(self) -> None:
        monitor = make_monitor()
        with make_service([monitor]) as service:
            service.list_monitors()
            budget_before = service.write_budget_available
            monitor.writes.clear()
            service.save_snapshot()
            self.assertEqual(monitor.writes, [])
            self.assertEqual(service.write_budget_available, budget_before)

    def test_keys_kept_from_the_saved_profile_are_not_replaced(self) -> None:
        """The CLI cannot see the tray app's software LUT: it would save it as
        neutral, erasing the user's adjustment from the profile."""
        monitor = make_monitor()
        store = ProfileStore()
        with make_service([monitor], store=store) as service:
            service.save_snapshot()
            monitor.values[0x10] = 80
            monitor.values[0x12] = 10
            service.refresh()
            service.save_snapshot(keep_saved={"contrast"})
        values = store.load().monitors[0].values
        self.assertEqual((values["brightness"], values["contrast"]), (80, 70))

    def test_identity_is_recorded_for_later_matching(self) -> None:
        with make_service([make_monitor()]) as service:
            saved = service.save_snapshot().monitors[0]
            state = service.list_monitors()[0]
            self.assertEqual(saved.id, state.id)
            self.assertEqual(saved.name, state.name)
            self.assertEqual(saved.device, state.device)


class TestRestore(unittest.TestCase):
    def test_restore_without_a_snapshot_is_refused(self) -> None:
        with (
            make_service([make_monitor()]) as service,
            self.assertRaises(SnapshotMissing),
        ):
            service.restore_snapshot()

    def test_puts_a_changed_value_back(self) -> None:
        monitor = make_monitor()
        with make_service([monitor]) as service:
            service.save_snapshot()
            state = service.list_monitors()[0]
            service.write_feature(state.id, "brightness", 90)
            self.assertEqual(monitor.values[0x10], 90)

            report = service.restore_snapshot()

            self.assertEqual(monitor.values[0x10], 50)
            self.assertEqual(len(report.applied), 1)
            self.assertEqual(report.applied[0].key, "brightness")
            self.assertEqual(report.applied[0].confirmed, 50)
            self.assertEqual(report.failed, ())

    def test_only_drifted_values_are_written(self) -> None:
        """The EEPROM argument: a restore is not an excuse to rewrite everything."""
        monitor = make_monitor()
        with make_service([monitor]) as service:
            service.save_snapshot()
            state = service.list_monitors()[0]
            service.write_feature(state.id, "brightness", 90)
            monitor.writes.clear()

            report = service.restore_snapshot()

            self.assertEqual(monitor.writes, [(0x10, 50)])
            self.assertEqual(report.unchanged, 3)

    def test_restoring_an_untouched_monitor_writes_nothing(self) -> None:
        monitor = make_monitor()
        with make_service([monitor]) as service:
            service.save_snapshot()
            monitor.writes.clear()
            report = service.restore_snapshot()
            self.assertEqual(monitor.writes, [])
            self.assertEqual(report.applied, ())
            self.assertEqual(report.unchanged, 4)

    def test_coarse_controls_are_written_before_the_fine_ones_they_reset(self) -> None:
        """Order comes from the registry, not from the order of the saved file."""
        monitor = make_monitor()
        store = ProfileStore()
        store.save(
            Snapshot(
                saved_at=0.0,
                monitors=(
                    MonitorSnapshot(
                        id="display1",
                        name="TestDisplay",
                        device=r"\\.\DISPLAY1",
                        # Deliberately the wrong way round: gain first, then the
                        # preset that would wipe it, then brightness.
                        values={"red_gain": 10, "color_preset": 8, "brightness": 20},
                    ),
                ),
            )
        )
        with make_service([monitor], store=store) as service:
            service.restore_snapshot()
            self.assertEqual([code for code, _ in monitor.writes], [0x10, 0x14, 0x16])

    def test_a_disconnected_monitor_does_not_stop_the_others(self) -> None:
        store = ProfileStore()
        first = make_monitor(device=r"\\.\DISPLAY1")
        second = make_monitor(device=r"\\.\DISPLAY2", primary=False)
        with make_service([first, second], store=store) as service:
            service.save_snapshot()

        # Same store, but the second monitor is no longer plugged in.
        with make_service([first], store=store) as service:
            first.values[0x10] = 90
            report = service.restore_snapshot()

        self.assertEqual(first.values[0x10], 50)
        self.assertEqual(len(report.skipped), 1)
        self.assertEqual(report.skipped[0].key, "")
        self.assertIn("not connected", report.skipped[0].reason)

    def test_a_monitor_is_matched_by_device_when_its_id_changed(self) -> None:
        store = ProfileStore()
        store.save(
            Snapshot(
                saved_at=0.0,
                monitors=(
                    MonitorSnapshot(
                        id="stale-id-from-another-boot",
                        name="Some other name",
                        device=r"\\.\DISPLAY1",
                        values={"brightness": 20},
                    ),
                ),
            )
        )
        monitor = make_monitor()
        with make_service([monitor], store=store) as service:
            report = service.restore_snapshot()
        self.assertEqual(monitor.values[0x10], 20)
        self.assertEqual(report.skipped, ())

    def test_a_feature_the_monitor_no_longer_answers_is_skipped(self) -> None:
        monitor = make_monitor()
        store = ProfileStore()
        with make_service([monitor], store=store) as service:
            service.save_snapshot()

        # The monitor stops implementing contrast between the two sessions.
        del monitor.values[0x12]
        monitor.values[0x10] = 90
        with make_service([monitor], store=store) as service:
            report = service.restore_snapshot()

        self.assertEqual(monitor.values[0x10], 50)
        skipped = {issue.key for issue in report.skipped}
        self.assertEqual(skipped, {"contrast"})

    def test_a_key_this_build_does_not_know_is_reported_not_written(self) -> None:
        store = ProfileStore()
        store.save(
            Snapshot(
                saved_at=0.0,
                monitors=(
                    MonitorSnapshot(
                        "display1",
                        "TestDisplay",
                        r"\\.\DISPLAY1",
                        {"brightness": 20, "warp_drive": 9},
                    ),
                ),
            )
        )
        monitor = make_monitor()
        with make_service([monitor], store=store) as service:
            report = service.restore_snapshot()
        self.assertEqual(monitor.values[0x10], 20)
        self.assertEqual([i.key for i in report.skipped], ["warp_drive"])

    def test_a_hand_edited_value_still_goes_through_the_policy(self) -> None:
        """The file is this program's own output, and is still not trusted."""
        store = ProfileStore()
        store.save(
            Snapshot(
                saved_at=0.0,
                monitors=(
                    MonitorSnapshot(
                        "display1",
                        "TestDisplay",
                        r"\\.\DISPLAY1",
                        # The monitor advertises only 0x05 and 0x08 for 0x14.
                        {"color_preset": 0x99},
                    ),
                ),
            )
        )
        monitor = make_monitor()
        with make_service([monitor], store=store) as service:
            report = service.restore_snapshot()

        self.assertEqual(monitor.values[0x14], 0x05)  # untouched
        self.assertEqual(len(report.failed), 1)
        self.assertEqual(report.failed[0].key, "color_preset")
        self.assertIn("not accepted", report.failed[0].reason)

    def test_an_out_of_range_value_is_clamped_like_any_other_write(self) -> None:
        store = ProfileStore()
        store.save(
            Snapshot(
                saved_at=0.0,
                monitors=(
                    MonitorSnapshot(
                        "display1", "TestDisplay", r"\\.\DISPLAY1", {"brightness": 5000}
                    ),
                ),
            )
        )
        monitor = make_monitor()
        with make_service([monitor], store=store) as service:
            report = service.restore_snapshot()
        self.assertEqual(monitor.values[0x10], 100)
        self.assertEqual(report.applied[0].applied, 100)

    def test_an_exhausted_budget_stops_the_remaining_writes(self) -> None:
        """Every further write would fail identically; the bus is left alone."""
        monitor = make_monitor()
        store = ProfileStore()
        with make_service([monitor], store=store) as service:
            service.save_snapshot()

        for code in (0x10, 0x12, 0x14, 0x16):
            monitor.values[code] = 1
        with make_service(
            [monitor], store=store, budget=WriteBudget(capacity=2, refill_per_second=0.01)
        ) as service:
            monitor.writes.clear()
            report = service.restore_snapshot()

        self.assertEqual(len(report.applied), 2)
        self.assertEqual(len(monitor.writes), 2)
        self.assertEqual(len(report.failed), 2)
        for issue in report.failed:
            self.assertIn("budget", issue.reason)

    def test_the_report_names_when_the_snapshot_was_taken(self) -> None:
        with make_service([make_monitor()]) as service:
            snapshot = service.save_snapshot()
            self.assertEqual(service.restore_snapshot().saved_at, snapshot.saved_at)


class TestAnalogueOnlyControls(unittest.TestCase):
    """Geometry on a digital input reads noise: never saved or written (D32).

    The ASUS VS248 on HDMI ("DVI 2") read horizontal position 181, later 189,
    above its own maximum of 100, with nobody touching it.
    """

    VGA, HDMI = 0x01, 0x04

    def monitor(self, source: int | None) -> FakeMonitor:
        values = {**VALUES, 0x20: 181}
        maxima = {**MAXIMA, 0x20: 255}
        if source is not None:
            values[0x60], maxima[0x60] = source, 0x12
        caps = CAPS.replace("60(0F 11)", "60(01 04) 20")
        return make_monitor(capabilities=caps, values=values, maxima=maxima)

    def test_save_on_a_digital_input_leaves_geometry_out(self) -> None:
        with make_service([self.monitor(self.HDMI)]) as service:
            saved = service.save_snapshot().monitors[0].values
            self.assertNotIn("horizontal_position", saved)
            self.assertEqual(saved["input_source"], self.HDMI)

    def test_save_on_an_analogue_input_keeps_it(self) -> None:
        with make_service([self.monitor(self.VGA)]) as service:
            saved = service.save_snapshot().monitors[0].values
            self.assertEqual(saved["horizontal_position"], 181)

    def test_an_unknown_input_changes_nothing(self) -> None:
        with make_service([self.monitor(None)]) as service:
            saved = service.save_snapshot().monitors[0].values
            self.assertEqual(saved["horizontal_position"], 181)

    def test_restore_on_a_digital_input_does_not_write_geometry(self) -> None:
        """Decided on a fresh read: the input was changed on the monitor."""
        monitor = self.monitor(self.VGA)
        with make_service([monitor]) as service:
            service.save_snapshot()
            monitor.values[0x60] = self.HDMI  # OSD buttons; the cache says VGA
            monitor.values[0x20] = 189
            monitor.writes.clear()
            report = service.restore_snapshot()
            self.assertNotIn(0x20, [code for code, _ in monitor.writes])
            self.assertEqual(report.skipped, ())
            self.assertEqual(report.failed, ())

    def test_restore_on_an_analogue_input_puts_geometry_back(self) -> None:
        monitor = self.monitor(self.VGA)
        with make_service([monitor]) as service:
            service.save_snapshot()
            monitor.values[0x20] = 120
            monitor.writes.clear()
            service.restore_snapshot()
            self.assertEqual(monitor.writes, [(0x20, 181)])


class TestSavedSnapshotQuery(unittest.TestCase):
    def test_nothing_saved_reads_as_none(self) -> None:
        with make_service([make_monitor()]) as service:
            self.assertIsNone(service.saved_snapshot())

    def test_the_saved_snapshot_is_readable_without_restoring(self) -> None:
        monitor = make_monitor()
        with make_service([monitor]) as service:
            saved = service.save_snapshot()
            monitor.writes.clear()
            self.assertEqual(service.saved_snapshot(), saved)
            self.assertEqual(monitor.writes, [])

    def test_the_service_reports_where_it_persists(self) -> None:
        with make_service([make_monitor()]) as service:
            self.assertIsNone(service.profiles_path)
        with make_service([make_monitor()], store=ProfileStore("a.json")) as service:
            self.assertEqual(service.profiles_path, Path("a.json"))


PANEL_A = EdidInfo("ABC", 0x0001, 0, "SERIAL-A", "PanelA")
PANEL_B = EdidInfo("ABC", 0x0001, 0, "SERIAL-B", "PanelB")
PANEL_C = EdidInfo("XYZ", 0x0002, 0, "SERIAL-C", "PanelC")
#: Same model key as the quirk table's TCL 25G64 entry.
TCL = EdidInfo("TCL", 0x1003, 0xFFFFFFFF, "2E675AA021124", "25G64")


class TestMonitorIdentity(unittest.TestCase):
    """Windows renumbers DISPLAYn between boots; calibrations follow the panel."""

    def test_swapped_display_numbers_follow_the_panel(self) -> None:
        # Both monitors carry the same capabilities model, so only the EDID can
        # tell them apart.
        store = ProfileStore()
        a = make_monitor(device=r"\\.\DISPLAY1", edid=PANEL_A)
        b = make_monitor(device=r"\\.\DISPLAY2", edid=PANEL_B, primary=False)
        a.values[0x10] = 30
        b.values[0x10] = 80
        with make_service([a, b], store=store) as service:
            service.save_snapshot()

        # Next boot: the numbers are swapped and both brightnesses have drifted.
        a.device, b.device = r"\\.\DISPLAY2", r"\\.\DISPLAY1"
        a.values[0x10] = b.values[0x10] = 55
        with make_service([b, a], store=store) as service:
            report = service.restore_snapshot()

        self.assertEqual(a.values[0x10], 30)
        self.assertEqual(b.values[0x10], 80)
        self.assertEqual(report.skipped, ())

    def test_a_different_panel_on_the_same_port_is_left_alone(self) -> None:
        store = ProfileStore()
        with make_service([make_monitor(edid=PANEL_A)], store=store) as service:
            service.save_snapshot()

        stranger = make_monitor(edid=PANEL_C)
        stranger.values[0x10] = 99
        with make_service([stranger], store=store) as service:
            report = service.restore_snapshot()

        self.assertEqual(stranger.writes, [])
        self.assertEqual(len(report.skipped), 1)
        self.assertIn("not connected", report.skipped[0].reason)

    def test_a_snapshot_from_before_hardware_ids_still_matches(self) -> None:
        store = ProfileStore()
        store.save(
            Snapshot(
                saved_at=0.0,
                monitors=(
                    MonitorSnapshot(
                        id="display1",
                        name="Generic PnP Monitor",
                        device=r"\\.\DISPLAY1",
                        values={"brightness": 20},
                    ),
                ),
            )
        )
        monitor = make_monitor(edid=PANEL_A)
        with make_service([monitor], store=store) as service:
            service.restore_snapshot()
        self.assertEqual(monitor.values[0x10], 20)

    def test_a_name_shared_by_two_saved_monitors_does_not_decide(self) -> None:
        """With one of two identical panels unplugged, the survivor must not
        take whichever entry happens to be listed first."""
        store = ProfileStore()
        store.save(
            Snapshot(
                saved_at=0.0,
                monitors=(
                    MonitorSnapshot(
                        "display1", "TestDisplay", r"\\.\DISPLAY1", {"brightness": 11}
                    ),
                    MonitorSnapshot(
                        "display2", "TestDisplay", r"\\.\DISPLAY2", {"brightness": 22}
                    ),
                ),
            )
        )
        survivor = make_monitor(device=r"\\.\DISPLAY2")
        with make_service([survivor], store=store) as service:
            service.restore_snapshot()
        self.assertEqual(survivor.values[0x10], 22)

    def test_the_hardware_id_is_saved_and_survives_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            with make_service(
                [make_monitor(edid=PANEL_A)], store=ProfileStore(path)
            ) as service:
                service.save_snapshot()
            reloaded = ProfileStore(path).load()
        self.assertEqual(reloaded.monitors[0].hardware_id, "ABC-0001-SERIAL-A")

    def test_a_file_without_hardware_ids_loads(self) -> None:
        snapshot = Snapshot.from_payload(
            {
                "version": SCHEMA_VERSION,
                "savedAt": 0.0,
                "monitors": [
                    {"id": "display1", "device": "x", "name": "y", "values": {}}
                ],
            }
        )
        self.assertEqual(snapshot.monitors[0].hardware_id, "")

    def test_the_edid_name_is_used_when_the_capabilities_have_no_model(self) -> None:
        monitor = make_monitor(capabilities="(vcp(10 12))", edid=TCL)
        with make_service([monitor]) as service:
            self.assertEqual(service.list_monitors()[0].name, "25G64")


class TestVolatileControls(unittest.TestCase):
    """The TCL 25G64 forgets its gains on power-off but keeps reporting them,
    and ignores a write of the value it reports.  A restore must step away
    and back regardless of what the read-back says: through the 50 pivot when
    the user asks for it, through a neighbour for invisible upkeep."""

    def make_tcl(self) -> FakeMonitor:
        return make_monitor(edid=TCL, ignores_same_value=frozenset({0x16}))

    def test_a_volatile_control_is_rewritten_even_when_it_reads_correctly(self) -> None:
        monitor = self.make_tcl()
        with make_service([monitor]) as service:
            service.save_snapshot()
            monitor.writes.clear()
            report = service.restore_snapshot()

        # Red gain was saved at 60: through the pivot, then back.  Nothing else
        # moves.
        self.assertEqual(monitor.writes, [(0x16, 50), (0x16, 60)])
        self.assertEqual(monitor.values[0x16], 60)
        self.assertEqual([o.key for o in report.applied], ["red_gain"])
        self.assertEqual(report.applied[0].confirmed, 60)
        self.assertEqual(report.unchanged, 3)

    def restore_writes(self, saved_value: int, maximum: int = 100, **kwargs):
        monitor = self.make_tcl()
        monitor.values[0x16] = saved_value
        monitor.maxima[0x16] = maximum
        with make_service([monitor]) as service:
            service.save_snapshot()
            monitor.writes.clear()
            service.restore_snapshot(**kwargs)
        return monitor.writes

    def test_a_value_equal_to_the_pivot_goes_through_a_neighbour(self) -> None:
        """50 -> 50 would be the very no-op the pivot exists to avoid."""
        self.assertEqual(self.restore_writes(50), [(0x16, 49), (0x16, 50)])

    def test_the_pivot_never_exceeds_the_monitor_maximum(self) -> None:
        self.assertEqual(self.restore_writes(10, maximum=30), [(0x16, 30), (0x16, 10)])

    def test_the_neighbour_strategy_is_a_single_step(self) -> None:
        writes = self.restore_writes(60, reassert=Reassert.NEIGHBOUR)
        self.assertEqual(writes, [(0x16, 59), (0x16, 60)])

    def test_the_neighbour_of_zero_is_one(self) -> None:
        writes = self.restore_writes(0, reassert=Reassert.NEIGHBOUR)
        self.assertEqual(writes, [(0x16, 1), (0x16, 0)])

    def test_setting_the_reported_value_by_hand_takes_effect(self) -> None:
        """The slider path: typing the value the monitor already reports must
        still reach the panel, without the user stepping away and back."""
        monitor = self.make_tcl()
        with make_service([monitor]) as service:
            state = service.list_monitors()[0]
            monitor.writes.clear()
            outcome = service.write_feature(state.id, "red_gain", 60)
        self.assertEqual(monitor.writes, [(0x16, 59), (0x16, 60)])
        self.assertEqual(outcome.confirmed, 60)
        self.assertTrue(any("stepped through 59" in w for w in outcome.warnings))

    def test_a_different_value_is_written_once(self) -> None:
        monitor = self.make_tcl()
        with make_service([monitor]) as service:
            state = service.list_monitors()[0]
            monitor.writes.clear()
            outcome = service.write_feature(state.id, "red_gain", 70)
        self.assertEqual(monitor.writes, [(0x16, 70)])
        self.assertEqual(outcome.warnings, ())

    def test_the_same_quirk_does_not_apply_to_other_models(self) -> None:
        monitor = make_monitor(edid=PANEL_A)
        with make_service([monitor]) as service:
            service.save_snapshot()
            monitor.writes.clear()
            service.restore_snapshot()
        self.assertEqual(monitor.writes, [])

    def test_a_budget_too_small_for_both_steps_writes_nothing(self) -> None:
        """Running out between the two writes would leave the gain one step off."""
        monitor = self.make_tcl()
        store = ProfileStore()
        with make_service([monitor], store=store) as service:
            service.save_snapshot()
        with make_service(
            [monitor], store=store, budget=WriteBudget(capacity=1, refill_per_second=0.01)
        ) as service:
            monitor.writes.clear()
            report = service.restore_snapshot()

        self.assertEqual(monitor.writes, [])
        self.assertEqual(monitor.values[0x16], 60)
        self.assertEqual(len(report.failed), 1)
        self.assertIn("budget", report.failed[0].reason)


class TestUpkeep(unittest.TestCase):
    """``reassert_saved`` is what runs on a timer, so what it leaves alone
    matters as much as what it writes: a monitor that keeps its settings must
    never be written to, or the timer becomes an EEPROM-wear loop."""

    def setUp(self) -> None:
        self.tcl = make_monitor(
            device=r"\\.\DISPLAY1", edid=TCL, ignores_same_value=frozenset({0x16})
        )
        self.other = make_monitor(device=r"\\.\DISPLAY2", edid=PANEL_A, primary=False)
        self.store = ProfileStore()
        with make_service([self.tcl, self.other], store=self.store) as service:
            service.save_snapshot()
        self.tcl.writes.clear()
        self.other.writes.clear()

    def service(self):
        return make_service([self.tcl, self.other], store=self.store)

    def test_only_volatile_controls_are_written(self) -> None:
        # Drift on a persistent control is none of upkeep's business.
        self.tcl.values[0x10] = 99
        self.other.values[0x10] = 99
        with self.service() as service:
            report = service.reassert_saved(reassert=Reassert.NEIGHBOUR)
        self.assertEqual(self.tcl.writes, [(0x16, 59), (0x16, 60)])
        self.assertEqual(self.other.writes, [])
        self.assertEqual([o.key for o in report.applied], ["red_gain"])
        self.assertEqual((report.unchanged, report.skipped, report.failed), (0, (), ()))

    def test_the_pivot_is_used_when_asked_for(self) -> None:
        with self.service() as service:
            service.reassert_saved(reassert=Reassert.PIVOT)
        self.assertEqual(self.tcl.writes, [(0x16, 50), (0x16, 60)])

    def test_it_can_be_limited_to_some_monitors(self) -> None:
        with self.service() as service:
            other_id = next(
                m.id for m in service.list_monitors() if m.hardware_id != TCL.hardware_id
            )
            service.reassert_saved(reassert=Reassert.PIVOT, monitor_ids=[other_id])
        self.assertEqual(self.tcl.writes, [])

    def test_a_missing_monitor_is_not_reported_as_a_problem(self) -> None:
        """Upkeep runs every minute; a switched-off monitor is not news."""
        with make_service([self.other], store=self.store) as service:
            report = service.reassert_saved(reassert=Reassert.PIVOT)
        self.assertEqual(report.skipped, ())

    def test_without_a_snapshot_it_refuses(self) -> None:
        with make_service([self.tcl]) as service, self.assertRaises(SnapshotMissing):
            service.reassert_saved(reassert=Reassert.PIVOT)


class TestPing(unittest.TestCase):
    def test_a_monitor_that_answers_is_alive(self) -> None:
        monitor = make_monitor()
        with make_service([monitor]) as service:
            state = service.list_monitors()[0]
            monitor.writes.clear()
            self.assertTrue(service.ping(state.id))
            self.assertEqual(monitor.writes, [])

    def test_a_monitor_that_stopped_answering_is_not(self) -> None:
        monitor = make_monitor()
        with make_service([monitor]) as service:
            state = service.list_monitors()[0]
            monitor.gone = True
            self.assertFalse(service.ping(state.id))


class TestAfterACheapEnumeration(unittest.TestCase):
    """The tray app re-enumerates without probing on every display change,
    which leaves every cached value empty.  Save and Restore must not act on
    that emptiness."""

    def test_save_reads_the_values_instead_of_saving_none(self) -> None:
        monitor = make_monitor()
        with make_service([monitor]) as service:
            service.refresh(probe=False)
            snapshot = service.save_snapshot()
        self.assertEqual(
            dict(snapshot.monitors[0].values),
            {"brightness": 50, "contrast": 70, "color_preset": 5, "red_gain": 60},
        )

    def test_restore_does_not_mistake_unread_for_drifted(self) -> None:
        """Otherwise every control would be rewritten: EEPROM wear for nothing."""
        monitor = make_monitor()
        store = ProfileStore()
        with make_service([monitor], store=store) as service:
            service.save_snapshot()
            service.refresh(probe=False)
            monitor.writes.clear()
            report = service.restore_snapshot()
        self.assertEqual(monitor.writes, [])
        self.assertEqual(report.unchanged, 4)

    def test_restore_sees_a_change_made_with_the_monitors_own_buttons(self) -> None:
        """The cache is not the monitor: the tray app's cache can be days old."""
        monitor = make_monitor()
        with make_service([monitor]) as service:
            service.save_snapshot()
            monitor.values[0x10] = 99  # behind the cache's back
            report = service.restore_snapshot()
        self.assertEqual(monitor.values[0x10], 50)
        self.assertEqual([o.key for o in report.applied], ["brightness"])

    def test_a_known_monitor_keeps_its_values_across_a_cheap_enumeration(self) -> None:
        """Re-enumerating renews handles; it does not change the monitor."""
        monitor = make_monitor(edid=PANEL_A)
        with make_service([monitor]) as service:
            service.list_monitors()
            monitor.reads.clear()
            state = service.refresh(probe=False)[0]
        self.assertTrue(state.probed)
        self.assertEqual(state.feature("brightness").value, 50)
        self.assertEqual(monitor.reads, [])

    def test_a_control_whose_probe_read_failed_is_tried_again(self) -> None:
        """A probe during power-up must not drop a control from Restore and
        upkeep for the session: every cheap enumeration carries it over."""
        tcl = make_monitor(edid=TCL, ignores_same_value=frozenset({0x16}))
        store = ProfileStore()
        with make_service([tcl], store=store) as service:
            service.save_snapshot()
        tcl.transient_failures = {0x10: 2, 0x16: 2}  # retry exhausted
        with make_service([tcl], store=store) as service:
            service.list_monitors()
            service.refresh(probe=False)
            tcl.writes.clear()
            upkeep = service.reassert_saved(reassert=Reassert.PIVOT)
            self.assertEqual(tcl.writes, [(0x16, 50), (0x16, 60)])
            self.assertEqual(upkeep.skipped, ())
            tcl.values[0x10] = 99
            restore = service.restore_snapshot()
        self.assertEqual(tcl.values[0x10], 50)
        self.assertIn("brightness", [o.key for o in restore.applied])
        self.assertEqual(restore.skipped, ())

    def test_a_monitor_without_an_edid_is_never_assumed_the_same(self) -> None:
        monitor = make_monitor()
        with make_service([monitor]) as service:
            service.list_monitors()
            state = service.refresh(probe=False)[0]
        self.assertFalse(state.probed)


if __name__ == "__main__":
    unittest.main()
