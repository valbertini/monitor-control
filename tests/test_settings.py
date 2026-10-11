"""Tests for the app settings file: written atomically, read defensively."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from monitorctl.app.settings import (
    MAX_INTERVAL_MINUTES,
    AppSettings,
    RefreshMode,
    SettingsStore,
    Theme,
)


class TestAppSettings(unittest.TestCase):
    def test_defaults(self) -> None:
        settings = AppSettings()
        self.assertEqual(settings.interval_minutes, 1)
        self.assertIs(settings.mode, RefreshMode.DETECT)
        self.assertFalse(settings.paused)
        self.assertTrue(settings.apply_on_start)
        self.assertIs(settings.theme, Theme.DARK)
        self.assertEqual(settings.interval_seconds, 60.0)

    def test_interval_is_bounded(self) -> None:
        for bad in (0, -1, MAX_INTERVAL_MINUTES + 1):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                AppSettings(interval_minutes=bad)
        for bad in (1.5, "5", True):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                AppSettings(interval_minutes=bad)

    def test_round_trip(self) -> None:
        original = AppSettings(
            interval_minutes=15,
            mode=RefreshMode.REWRITE,
            paused=True,
            apply_on_start=False,
            theme=Theme.LIGHT,
        )
        payload = json.loads(json.dumps(original.to_payload()))
        self.assertEqual(AppSettings.from_payload(payload), original)

    def test_one_bad_field_does_not_cost_the_others(self) -> None:
        with self.assertLogs("monitorctl.app.settings", level="WARNING"):
            settings = AppSettings.from_payload(
                {
                    "version": 1,
                    "interval_minutes": 99999,
                    "mode": "rewrite",
                    "paused": "yes",
                    "theme": "light",
                }
            )
        self.assertEqual(settings.interval_minutes, 1)  # default
        self.assertIs(settings.mode, RefreshMode.REWRITE)  # kept
        self.assertFalse(settings.paused)  # default
        self.assertIs(settings.theme, Theme.LIGHT)  # kept

    def test_not_an_object_or_another_version_is_refused(self) -> None:
        for payload in ([], {"version": 2}, {}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                AppSettings.from_payload(payload)


class TestSettingsStore(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "nested" / "settings.json"

    def test_missing_file_gives_defaults(self) -> None:
        self.assertEqual(SettingsStore(self.path).get(), AppSettings())

    def test_changes_survive_a_new_process(self) -> None:
        SettingsStore(self.path).update(interval_minutes=5, mode=RefreshMode.REWRITE)
        reloaded = SettingsStore(self.path).get()
        self.assertEqual(reloaded.interval_minutes, 5)
        self.assertIs(reloaded.mode, RefreshMode.REWRITE)

    def test_an_invalid_change_changes_nothing(self) -> None:
        store = SettingsStore(self.path)
        with self.assertRaises(ValueError):
            store.update(interval_minutes=0)
        self.assertEqual(store.get(), AppSettings())
        self.assertFalse(self.path.exists())

    def test_corrupt_file_gives_defaults_and_is_left_alone(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("monitorctl.app.settings", level="WARNING"):
            self.assertEqual(SettingsStore(self.path).get(), AppSettings())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

    def test_damaged_bytes_give_defaults_instead_of_stopping_the_app(self) -> None:
        # Read when the tray app starts: an error escaping here kept it from
        # starting at all.
        self.path.parent.mkdir(parents=True)
        for raw in (b"\xff\xfe{", b"[" * 100_000):
            with self.subTest(raw=raw[:8]):
                self.path.write_bytes(raw)
                with self.assertLogs("monitorctl.app.settings", level="WARNING"):
                    self.assertEqual(SettingsStore(self.path).get(), AppSettings())

    def test_a_byte_order_mark_is_accepted(self) -> None:
        # Notepad and Windows PowerShell 5.1 write UTF-8 with one.
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xef\xbb\xbf" + b'{"version": 1, "interval_minutes": 7}')
        self.assertEqual(SettingsStore(self.path).get().interval_minutes, 7)

    def test_unwritable_location_keeps_the_change_in_memory(self) -> None:
        blocker = self.path.parent
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("a file where the directory should be", encoding="utf-8")
        store = SettingsStore(blocker / "settings.json")
        with self.assertLogs("monitorctl.app.settings", level="WARNING"):
            store.update(paused=True)
        self.assertTrue(store.get().paused)

    def test_memory_only_store(self) -> None:
        store = SettingsStore(None)
        store.update(paused=True)
        self.assertTrue(store.get().paused)


if __name__ == "__main__":
    unittest.main()
