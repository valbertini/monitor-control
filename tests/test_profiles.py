"""Tests for named profiles: the book, its file, and the service operations.

Two properties matter more than any single operation: a monitor's profiles are
its own - switching one never writes another - and nothing a user saved is
ever lost by an upgrade, a corrupt file or a monitor that is unplugged.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from monitorctl.ddc.edid import EdidInfo
from monitorctl.ddc.errors import ProfileError, SnapshotError
from monitorctl.ddc.profiles import (
    DEFAULT_PROFILE_NAME,
    MAX_NAME_LENGTH,
    MonitorProfiles,
    Profile,
    ProfileBook,
    ProfileStore,
    check_profile_name,
    default_profiles_path,
)
from monitorctl.ddc.service import MonitorService, Pacing, Reassert
from monitorctl.ddc.snapshot import MonitorSnapshot, Snapshot
from monitorctl.gpu import GpuController

from .fakes import FakeBackend, FakeMonitor, legacy_payload

NO_PACING = Pacing(
    between_io=0.0, after_write=0.0, after_capabilities=0.0, retry_backoff=0.0
)
CAPS = "(prot(monitor)type(LCD)model(TestDisplay)vcp(10 12 16)mccs_ver(2.1))"
TCL = EdidInfo("TCL", 0x1003, 0xFFFFFFFF, "2E675AA021124", "25G64")
ASUS = EdidInfo("ACI", 0x2498, 0, "SERIAL-ASUS", "VS248")


def entry(values: dict[str, int]) -> MonitorSnapshot:
    return MonitorSnapshot(
        id="display1", name="TestDisplay", device=r"\\.\DISPLAY1", values=values
    )


def one_monitor_snapshot(brightness: int, at: float = 1.0) -> Snapshot:
    return Snapshot(saved_at=at, monitors=(entry({"brightness": brightness}),))


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


class TestNames(unittest.TestCase):
    def test_a_name_is_trimmed(self) -> None:
        self.assertEqual(check_profile_name("  Night  "), "Night")

    def test_empty_long_and_control_characters_are_refused(self) -> None:
        for bad in ("", "   ", "x" * (MAX_NAME_LENGTH + 1), "a\tb", "a\nb"):
            with self.subTest(bad=bad), self.assertRaises(ProfileError):
                check_profile_name(bad)

    def test_accents_are_fine(self) -> None:
        self.assertEqual(check_profile_name("Calibração"), "Calibração")


# --------------------------------------------------------------------------- #
# The book
# --------------------------------------------------------------------------- #


class TestBook(unittest.TestCase):
    def setUp(self) -> None:
        self.monitor = entry({})
        self.present = (self.monitor,)
        self.book = ProfileBook().saving(
            [(self.monitor, {"brightness": 30})], self.present, 1.0
        )

    def profiles(self, book: ProfileBook) -> MonitorProfiles:
        return book.monitors[book.index_for(self.monitor, self.present)]

    def test_the_first_save_creates_an_active_default(self) -> None:
        profiles = self.profiles(self.book)
        self.assertEqual(profiles.names, (DEFAULT_PROFILE_NAME,))
        self.assertEqual(profiles.active, DEFAULT_PROFILE_NAME)
        self.assertEqual(dict(profiles.active_profile.values), {"brightness": 30})

    def test_saving_again_replaces_the_active_profile_only(self) -> None:
        book = self.book.creating(
            self.monitor, self.present, "Night", {"brightness": 5}, 2
        )
        book = book.saving([(self.monitor, {"brightness": 9})], self.present, 3.0)
        profiles = self.profiles(book)
        self.assertEqual(dict(profiles.profile("Night").values), {"brightness": 9})
        self.assertEqual(dict(profiles.profile("Default").values), {"brightness": 30})

    def test_a_new_profile_becomes_active(self) -> None:
        book = self.book.creating(
            self.monitor, self.present, "Night", {"brightness": 5}, 2
        )
        self.assertEqual(self.profiles(book).active, "Night")

    def test_names_are_unique_ignoring_case(self) -> None:
        with self.assertRaises(ProfileError):
            self.book.creating(self.monitor, self.present, "default", {}, 2)

    def test_switching_to_an_unknown_profile_is_refused(self) -> None:
        with self.assertRaises(ProfileError):
            self.book.activating(self.monitor, self.present, "Nope")

    def test_switching_ignores_case_and_keeps_the_stored_spelling(self) -> None:
        book = self.book.creating(self.monitor, self.present, "Night", {}, 2)
        book = book.activating(self.monitor, self.present, "default")
        self.assertEqual(self.profiles(book).active, "Default")

    def test_renaming_the_active_profile_keeps_it_active(self) -> None:
        book = self.book.renaming(self.monitor, self.present, "Default", "Calibrated")
        self.assertEqual(self.profiles(book).active, "Calibrated")
        self.assertEqual(self.profiles(book).names, ("Calibrated",))

    def test_renaming_only_the_case_is_allowed(self) -> None:
        book = self.book.renaming(self.monitor, self.present, "Default", "DEFAULT")
        self.assertEqual(self.profiles(book).names, ("DEFAULT",))

    def test_renaming_onto_another_profile_is_refused(self) -> None:
        book = self.book.creating(self.monitor, self.present, "Night", {}, 2)
        with self.assertRaises(ProfileError):
            book.renaming(self.monitor, self.present, "Night", "default")

    def test_the_active_profile_cannot_be_deleted(self) -> None:
        with self.assertRaises(ProfileError):
            self.book.deleting(self.monitor, self.present, "Default")

    def test_another_profile_can_be_deleted(self) -> None:
        book = self.book.creating(self.monitor, self.present, "Night", {}, 2)
        book = book.activating(self.monitor, self.present, "Default")
        book = book.deleting(self.monitor, self.present, "Night")
        self.assertEqual(self.profiles(book).names, ("Default",))

    def test_a_refused_change_leaves_the_book_as_it_was(self) -> None:
        before = self.book
        with self.assertRaises(ProfileError):
            self.book.deleting(self.monitor, self.present, "Default")
        self.assertEqual(self.book, before)

    def test_an_unplugged_monitor_keeps_its_profiles_when_others_save(self) -> None:
        other = MonitorSnapshot("display2", "Other", r"\\.\DISPLAY2", {})
        book = self.book.saving([(other, {"brightness": 70})], (other,), 2.0)
        self.assertEqual(len(book.monitors), 2)
        snapshot = book.active_snapshot()
        self.assertEqual(
            {m.name: dict(m.values) for m in snapshot.monitors},
            {"TestDisplay": {"brightness": 30}, "Other": {"brightness": 70}},
        )

    def test_a_monitor_with_profiles_but_none_active_gets_a_fresh_default(self) -> None:
        book = ProfileBook(
            (
                MonitorProfiles(
                    "display1",
                    "TestDisplay",
                    r"\\.\DISPLAY1",
                    "",
                    profiles=(Profile("Default", 1.0, {}),),
                ),
            )
        )
        book = book.saving([(self.monitor, {"brightness": 1})], self.present, 2.0)
        profiles = self.profiles(book)
        self.assertEqual(profiles.names, ("Default", "Default 2"))
        self.assertEqual(profiles.active, "Default 2")

    def test_the_active_view_is_none_without_any_active_profile(self) -> None:
        self.assertIsNone(ProfileBook().active_snapshot())


class TestBookFile(unittest.TestCase):
    def test_round_trip(self) -> None:
        monitor = entry({})
        book = ProfileBook().saving([(monitor, {"brightness": 30})], (monitor,), 1.0)
        book = book.creating(monitor, (monitor,), "Night", {"brightness": 5}, 2.0)
        self.assertEqual(ProfileBook.from_payload(book.to_payload()), book)

    def test_an_active_name_that_does_not_exist_leaves_none_active(self) -> None:
        payload = {
            "version": 1,
            "monitors": [
                {
                    "id": "display1",
                    "name": "x",
                    "device": "d",
                    "hardwareId": "",
                    "active": "Ghost",
                    "profiles": [{"name": "Default", "savedAt": 1, "values": {}}],
                }
            ],
        }
        with self.assertLogs("monitorctl.ddc.profiles", level="WARNING"):
            book = ProfileBook.from_payload(payload)
        self.assertEqual(book.monitors[0].active, "")
        self.assertEqual(book.monitors[0].names, ("Default",))

    def test_duplicate_names_make_the_file_unreadable(self) -> None:
        payload = {
            "version": 1,
            "monitors": [
                {
                    "id": "a",
                    "name": "x",
                    "device": "d",
                    "active": "",
                    "profiles": [
                        {"name": "Night", "savedAt": 1, "values": {}},
                        {"name": "night", "savedAt": 1, "values": {}},
                    ],
                }
            ],
        }
        with self.assertRaises(ValueError):
            ProfileBook.from_payload(payload)


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


class TestStore(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "nested" / "profiles.json"
        self.legacy = self.directory / "nested" / "snapshot.json"

    def test_memory_store_keeps_nothing_on_disk(self) -> None:
        store = ProfileStore()
        self.assertIsNone(store.path)
        self.assertIsNone(store.load())
        store.save(one_monitor_snapshot(50))
        self.assertEqual(store.load(), one_monitor_snapshot(50))

    def test_file_store_survives_a_new_process(self) -> None:
        ProfileStore(self.path).save(one_monitor_snapshot(50))
        self.assertEqual(ProfileStore(self.path).load(), one_monitor_snapshot(50))

    def test_saving_twice_leaves_no_temporary_files(self) -> None:
        store = ProfileStore(self.path)
        store.save(one_monitor_snapshot(50))
        store.save(one_monitor_snapshot(80, at=2.0))
        self.assertEqual(ProfileStore(self.path).load(), one_monitor_snapshot(80, at=2.0))
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["profiles.json"])

    def test_missing_file_reads_as_nothing_saved(self) -> None:
        self.assertIsNone(ProfileStore(self.path).load())

    def test_a_corrupt_file_reads_as_nothing_and_is_kept_aside_before_a_save(
        self,
    ) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        store = ProfileStore(self.path)
        with self.assertLogs("monitorctl.ddc.profiles", level="WARNING"):
            self.assertIsNone(store.load())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

        with self.assertLogs("monitorctl.ddc.profiles", level="WARNING"):
            store.save(one_monitor_snapshot(50))
        aside = self.path.with_name("profiles.unreadable.json")
        self.assertEqual(aside.read_text(encoding="utf-8"), "{not json")
        self.assertEqual(ProfileStore(self.path).load(), one_monitor_snapshot(50))

    def test_a_file_of_a_newer_version_reads_as_nothing(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"version": 99, "monitors": []}), "utf-8")
        with self.assertLogs("monitorctl.ddc.profiles", level="WARNING"):
            self.assertIsNone(ProfileStore(self.path).load())

    def test_unwritable_destination_raises_but_keeps_the_change(self) -> None:
        """A read-only disk costs persistence, not the feature."""
        blocker = self.directory / "blocked"
        blocker.write_text("a file, not a directory", encoding="utf-8")
        store = ProfileStore(blocker / "profiles.json")
        with self.assertRaises(SnapshotError):
            store.save(one_monitor_snapshot(50))
        self.assertEqual(store.load(), one_monitor_snapshot(50))

    def test_a_save_by_another_process_is_picked_up(self) -> None:
        """The tray app lives for days; ``monitorctl save`` runs beside it."""
        app = ProfileStore(self.path)
        app.save(one_monitor_snapshot(50))
        ProfileStore(self.path).save(one_monitor_snapshot(80, at=2.0))  # the CLI
        self.assertEqual(app.load(), one_monitor_snapshot(80, at=2.0))

    def test_an_unchanged_file_is_not_read_again(self) -> None:
        store = ProfileStore(self.path)
        store.save(one_monitor_snapshot(50))
        with mock.patch.object(Path, "read_text") as read:
            store.load()
        read.assert_not_called()

    def test_a_refused_change_writes_nothing(self) -> None:
        store = ProfileStore(self.path)
        store.save(one_monitor_snapshot(50))
        before = self.path.read_bytes()
        monitor = one_monitor_snapshot(50).monitors[0]
        with self.assertRaises(ProfileError):
            store.update(lambda b: b.deleting(monitor, (monitor,), "Default"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_old_snapshot_becomes_each_monitors_default_profile(self) -> None:
        """Upgrading must not lose what was saved before profiles existed."""
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text(
            json.dumps(legacy_payload(one_monitor_snapshot(42, at=7.0))), "utf-8"
        )
        store = ProfileStore(self.path, legacy_path=self.legacy)
        self.assertEqual(store.load(), one_monitor_snapshot(42, at=7.0))
        book = store.book()
        self.assertEqual(book.monitors[0].names, (DEFAULT_PROFILE_NAME,))
        self.assertTrue(self.legacy.exists())  # never deleted

    def test_the_old_snapshot_is_ignored_once_profiles_exist(self) -> None:
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text(
            json.dumps(legacy_payload(one_monitor_snapshot(42))), "utf-8"
        )
        ProfileStore(self.path).save(one_monitor_snapshot(60))
        store = ProfileStore(self.path, legacy_path=self.legacy)
        self.assertEqual(store.load(), one_monitor_snapshot(60))

    def test_default_path_is_in_the_state_directory(self) -> None:
        path = default_profiles_path()
        self.assertEqual(path.name, "profiles.json")
        self.assertEqual(path.parent.name, "monitorctl")


# --------------------------------------------------------------------------- #
# Through the service
# --------------------------------------------------------------------------- #


def make_monitor(edid: EdidInfo, device: str, **overrides) -> FakeMonitor:
    defaults = {
        "device": device,
        "capabilities": CAPS,
        "values": {0x10: 40, 0x12: 70, 0x16: 60},
        "maxima": {0x10: 100, 0x12: 100, 0x16: 100},
        "edid": edid,
        "ignores_same_value": frozenset({0x16}) if edid is TCL else frozenset(),
    }
    defaults.update(overrides)
    return FakeMonitor(**defaults)


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tcl = make_monitor(TCL, r"\\.\DISPLAY1")
        self.asus = make_monitor(ASUS, r"\\.\DISPLAY2", primary=False)
        self.monitors = [self.tcl, self.asus]
        self.store = ProfileStore()
        self.service = self.make_service()
        self.ids = {m.hardware_id: m.id for m in self.service.list_monitors()}
        self.tcl_id = self.ids[TCL.hardware_id]
        self.asus_id = self.ids[ASUS.hardware_id]

    def make_service(self) -> MonitorService:
        service = MonitorService(
            FakeBackend(self.monitors),
            pacing=NO_PACING,
            gpu=GpuController([]),
            profiles=self.store,
        )
        self.addCleanup(service.close)
        return service

    def clear(self) -> None:
        for monitor in self.monitors:
            monitor.writes.clear()


class TestServiceProfiles(ServiceTestCase):
    def test_switching_writes_the_profile_to_that_monitor_only(self) -> None:
        self.service.save_snapshot()
        self.tcl.values[0x10] = 20
        self.service.write_feature(self.tcl_id, "brightness", 20)
        self.service.create_profile(self.tcl_id, "Night")
        self.service.activate_profile(self.tcl_id, "Default")  # back to 40
        self.clear()

        report = self.service.activate_profile(self.tcl_id, "Night")

        self.assertEqual(self.tcl.values[0x10], 20)
        self.assertIn((0x10, 20), self.tcl.writes)
        # Forgetful control pushed through the pivot, as on any restore.
        self.assertIn((0x16, 50), self.tcl.writes)
        self.assertEqual(self.asus.writes, [])
        self.assertTrue(all(o.monitor_id == self.tcl_id for o in report.applied))

    def test_one_monitors_switch_leaves_the_others_active_profile(self) -> None:
        self.service.save_snapshot()
        self.service.create_profile(self.tcl_id, "Night")
        profiles = self.service.profiles_by_monitor()
        self.assertEqual(profiles[self.tcl_id].active, "Night")
        self.assertEqual(profiles[self.asus_id].active, "Default")

    def test_restore_and_the_keeper_follow_the_active_profile(self) -> None:
        self.service.save_snapshot()
        self.service.write_feature(self.tcl_id, "brightness", 15)
        self.service.create_profile(self.tcl_id, "Dim")
        self.service.write_feature(self.tcl_id, "brightness", 90)

        self.service.restore_snapshot()

        self.assertEqual(self.tcl.values[0x10], 15)
        saved = {
            m.hardware_id: dict(m.values) for m in self.service.saved_snapshot().monitors
        }
        self.assertEqual(saved[TCL.hardware_id]["brightness"], 15)

    def test_save_into_one_monitor_only(self) -> None:
        self.service.save_snapshot()
        self.service.write_feature(self.tcl_id, "brightness", 11)
        self.service.write_feature(self.asus_id, "brightness", 22)
        self.service.save_snapshot(monitor_ids=[self.tcl_id])
        profiles = self.service.profiles_by_monitor()
        self.assertEqual(profiles[self.tcl_id].active_profile.values["brightness"], 11)
        self.assertEqual(profiles[self.asus_id].active_profile.values["brightness"], 40)

    def test_profiles_follow_the_panel_when_windows_renumbers(self) -> None:
        self.service.save_snapshot()
        self.service.create_profile(self.tcl_id, "Night")
        self.tcl.device, self.asus.device = self.asus.device, self.tcl.device
        service = self.make_service()
        ids = {m.hardware_id: m.id for m in service.list_monitors()}
        profiles = service.profiles_by_monitor()
        self.assertEqual(profiles[ids[TCL.hardware_id]].active, "Night")
        self.assertEqual(profiles[ids[ASUS.hardware_id]].active, "Default")

    def test_profile_errors_reach_the_caller(self) -> None:
        self.service.save_snapshot()
        with self.assertRaises(ProfileError):
            self.service.create_profile(self.tcl_id, "default")
        with self.assertRaises(ProfileError):
            self.service.delete_profile(self.tcl_id, "Default")
        with self.assertRaises(ProfileError):
            self.service.activate_profile(self.tcl_id, "Nope")

    def test_rename_and_delete_write_nothing_to_the_monitor(self) -> None:
        self.service.save_snapshot()
        self.service.create_profile(self.tcl_id, "Night")
        self.clear()
        self.service.rename_profile(self.tcl_id, "Default", "Day")
        self.service.delete_profile(self.tcl_id, "Day")
        self.assertEqual(self.tcl.writes, [])
        self.assertEqual(
            self.service.profiles_by_monitor()[self.tcl_id].names, ("Night",)
        )


class TestSaveRereads(ServiceTestCase):
    def test_without_reread_the_cache_is_saved(self) -> None:
        """The window saves what it shows."""
        self.service.save_snapshot()
        self.asus.values[0x10] = 77  # changed with the monitor's own buttons
        snapshot = self.service.save_snapshot()
        asus = next(m for m in snapshot.monitors if m.id == self.asus_id)
        self.assertEqual(asus.values["brightness"], 40)

    def test_with_reread_a_change_made_on_the_monitor_is_saved(self) -> None:
        """The tray shows nothing, so it asks the monitors."""
        self.service.save_snapshot()
        self.asus.values[0x10] = 77
        snapshot = self.service.save_snapshot(reread=True)
        asus = next(m for m in snapshot.monitors if m.id == self.asus_id)
        self.assertEqual(asus.values["brightness"], 77)

    def test_a_control_that_fails_to_answer_keeps_its_cached_value(self) -> None:
        self.service.save_snapshot()
        self.asus.transient_failures[0x12] = 5
        snapshot = self.service.save_snapshot(reread=True)
        asus = next(m for m in snapshot.monitors if m.id == self.asus_id)
        self.assertEqual(asus.values["contrast"], 70)

    def test_reread_writes_nothing(self) -> None:
        self.service.save_snapshot()
        self.clear()
        self.service.save_snapshot(reread=True)
        self.assertEqual(self.tcl.writes + self.asus.writes, [])


class TestReadFailures(unittest.TestCase):
    """A file that cannot be read *right now* is not a file with nothing in it."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "profiles.json"
        self.monitor = entry({})
        self.app = ProfileStore(self.path)
        self.app.save(one_monitor_snapshot(50))
        self.app.update(
            lambda b: b.creating(self.monitor, (self.monitor,), "Night", {}, 2.0)
        )
        # Another process saves, then the next read hits a locked file.
        ProfileStore(self.path).save(one_monitor_snapshot(60, at=3.0))
        self.before = self.path.read_bytes()

    def locked(self):
        return mock.patch.object(Path, "read_text", side_effect=PermissionError("locked"))

    def test_the_last_good_book_is_still_served(self) -> None:
        with self.locked(), self.assertLogs("monitorctl.ddc.profiles", "WARNING"):
            snapshot = self.app.load()
        self.assertIsNotNone(snapshot)

    def test_no_change_is_written_over_it(self) -> None:
        with (
            self.locked(),
            self.assertLogs("monitorctl.ddc.profiles", "WARNING"),
            self.assertRaises(SnapshotError),
        ):
            self.app.save(one_monitor_snapshot(70, at=4.0))
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_the_next_read_tries_again(self) -> None:
        with self.locked(), self.assertLogs("monitorctl.ddc.profiles", "WARNING"):
            self.app.load()
        self.assertEqual(self.app.load(), one_monitor_snapshot(60, at=3.0))
        names = self.app.book().monitors[0].names
        self.assertEqual(names, ("Default", "Night"))

    def test_an_unreadable_file_that_cannot_be_kept_aside_is_not_overwritten(
        self,
    ) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        store = ProfileStore(self.path)
        with self.assertLogs("monitorctl.ddc.profiles", "WARNING"):
            store.load()
        with (
            mock.patch("monitorctl.ddc.profiles.os.replace", side_effect=OSError("busy")),
            self.assertRaises(SnapshotError),
        ):
            store.save(one_monitor_snapshot(50))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")


class TestSaveKeepsWhatCouldNotBeRead(ServiceTestCase):
    def test_a_control_that_failed_its_read_keeps_its_saved_value(self) -> None:
        """A garbled reply during a probe is not the monitor losing contrast."""
        self.service.save_snapshot()
        self.asus.transient_failures[0x12] = 2  # the read and its one retry
        self.service.refresh(probe=True)
        self.service.save_snapshot()
        saved = self.service.profiles_by_monitor()[self.asus_id].active_profile.values
        self.assertEqual(saved["contrast"], 70)

    def test_the_tray_save_reads_it_again(self) -> None:
        self.service.save_snapshot()
        self.asus.transient_failures[0x12] = 2
        self.service.refresh(probe=True)
        self.asus.values[0x12] = 33  # answers now, with a new value
        self.service.save_snapshot(reread=True)
        saved = self.service.profiles_by_monitor()[self.asus_id].active_profile.values
        self.assertEqual(saved["contrast"], 33)


class TestFlakyRead(ServiceTestCase):
    def test_one_failed_read_does_not_mark_a_control_unsupported(self) -> None:
        """A garbled reply is not the monitor saying it lacks the control."""
        from monitorctl.ddc.errors import DdcBackendError

        self.service.refresh(probe=True)
        self.asus.transient_failures[0x12] = 2  # the read and its one retry
        with self.assertRaises(DdcBackendError):
            self.service.read_feature(self.asus_id, "contrast")
        contrast = self.service.get_monitor(self.asus_id).feature("contrast")
        self.assertTrue(contrast.supported)
        self.assertEqual(contrast.value, 70)

    def test_restore_reports_it_as_failed_not_as_unsupported(self) -> None:
        self.service.save_snapshot()
        self.asus.transient_failures[0x12] = 2
        report = self.service.restore_snapshot()
        self.assertIn("contrast", {i.key for i in report.failed})
        self.assertNotIn("contrast", {i.key for i in report.skipped})


class TestReassertStillPivots(ServiceTestCase):
    def test_new_profile_values_reach_the_forgetful_monitor(self) -> None:
        self.service.save_snapshot()
        self.clear()
        self.service.create_profile(self.tcl_id, "Night")
        self.service.reassert_saved(reassert=Reassert.PIVOT, monitor_ids=[self.tcl_id])
        self.assertEqual(self.tcl.writes, [(0x16, 50), (0x16, 60)])
        self.assertEqual(self.asus.writes, [])


if __name__ == "__main__":
    unittest.main()
