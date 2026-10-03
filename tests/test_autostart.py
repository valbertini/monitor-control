"""Tests for starting the tray app with Windows, and the CLI paths around it.

The real Run key is never touched: :class:`DictRunKey` stands in for it.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from monitorctl import autostart, cli


class DictRunKey:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, command: str) -> None:
        self.values[name] = command

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


class TestLaunchCommand(unittest.TestCase):
    def test_from_source_it_runs_the_windowless_launcher(self) -> None:
        command = autostart.launch_command()
        self.assertTrue(command.endswith(f'launch_app.pyw" {autostart.STARTUP_FLAG}'))
        launcher = Path(command.split('"')[3])
        self.assertTrue(launcher.is_file(), launcher)

    def test_packaged_it_runs_the_executable(self) -> None:
        with (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "executable", r"C:\Apps\monitorctl\monitorctl.exe"),
        ):
            command = autostart.launch_command()
        self.assertTrue(command.startswith('"'))
        self.assertIn("monitorctl.exe", command)
        self.assertTrue(command.endswith(autostart.STARTUP_FLAG))

    def test_enabled_from_the_cli_it_still_registers_the_tray_app(self) -> None:
        """``monitorctl-cli autostart enable`` must not register the CLI."""
        with (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(
                sys, "executable", r"C:\Apps\monitorctl\monitorctl-cli.exe"
            ),
        ):
            command = autostart.launch_command()
        app = Path(r"C:\Apps\monitorctl\monitorctl.exe").resolve()
        self.assertEqual(command, f'"{app}" --startup')


class TestRunKey(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(autostart, "remove_legacy_task", return_value=False)
        self.remove_legacy = patcher.start()
        self.addCleanup(patcher.stop)

    def test_enable_registers_this_installation(self) -> None:
        key = DictRunKey()
        command = autostart.enable(key)
        self.assertEqual(key.values, {autostart.VALUE_NAME: command})
        self.assertTrue(autostart.is_enabled(key))

    def test_enabling_retires_the_old_logon_task(self) -> None:
        """Both running would apply the same settings twice at every logon."""
        autostart.enable(DictRunKey())
        self.remove_legacy.assert_called_once()

    def test_disable(self) -> None:
        key = DictRunKey({autostart.VALUE_NAME: "x"})
        self.assertTrue(autostart.disable(key))
        self.assertFalse(autostart.is_enabled(key))
        self.assertFalse(autostart.disable(key))

    def test_repair_follows_a_moved_folder(self) -> None:
        key = DictRunKey(
            {autostart.VALUE_NAME: r'"D:\old place\monitorctl.exe" --startup'}
        )
        self.assertTrue(autostart.repair(key))
        self.assertEqual(key.values[autostart.VALUE_NAME], autostart.launch_command())
        self.assertFalse(autostart.repair(key))  # already right

    def test_repair_leaves_another_installation_that_still_exists(self) -> None:
        """Trying the fresh build in ``dist`` must not take the start over."""
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory) / "monitorctl.exe"
            other.write_bytes(b"")
            registered = f'"{other}" --startup'
            key = DictRunKey({autostart.VALUE_NAME: registered})
            self.assertFalse(autostart.repair(key))
            self.assertEqual(key.values[autostart.VALUE_NAME], registered)

    def test_repair_never_turns_a_disabled_start_back_on(self) -> None:
        key = DictRunKey()
        self.assertFalse(autostart.repair(key))
        self.assertEqual(key.values, {})


class TestCli(unittest.TestCase):
    def test_autostart_commands_never_touch_the_monitors(self) -> None:
        with (
            mock.patch.object(cli, "_build_backend") as build_backend,
            mock.patch.object(autostart.RegistryRunKey, "get", return_value=None),
            mock.patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            self.assertEqual(cli.main(["autostart", "status"]), 0)
        build_backend.assert_not_called()
        self.assertIn("Start with Windows: off", out.getvalue())

    def test_features_lists_the_registry_without_touching_any_hardware(self) -> None:
        with (
            mock.patch.object(cli, "_build_backend") as build_backend,
            mock.patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            self.assertEqual(cli.main(["features"]), 0)
        build_backend.assert_not_called()
        self.assertIn("brightness", out.getvalue())

    def test_status_with_zero_lines_prints_no_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "monitorctl.log"
            log.write_text("first\nsecond\n", encoding="utf-8")
            with (
                mock.patch.object(cli, "state_directory", return_value=Path(directory)),
                mock.patch.object(autostart.RegistryRunKey, "get", return_value=None),
                mock.patch("sys.stdout", new_callable=io.StringIO) as out,
            ):
                cli.main(["autostart", "status", "--lines", "0"])
        self.assertNotIn("first", out.getvalue())

    def test_log_file_captures_printed_output_and_log_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "nested" / "restore.log"
            with mock.patch.object(autostart.RegistryRunKey, "get", return_value=None):
                self.assertEqual(
                    cli.main(["--log-file", str(log), "autostart", "status"]), 0
                )
            text = log.read_text(encoding="utf-8")
        self.assertIn("monitorctl", text)  # the log record naming the run
        self.assertIn("Start with Windows: off", text)  # what the command printed

    def test_an_unexpected_failure_lands_in_the_log(self) -> None:
        """Without a console a traceback on stderr goes nowhere."""
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "restore.log"
            with mock.patch.object(
                autostart.RegistryRunKey, "get", side_effect=RuntimeError("boom")
            ):
                self.assertEqual(
                    cli.main(["--log-file", str(log), "autostart", "status"]), 1
                )
            text = log.read_text(encoding="utf-8")
        self.assertIn("RuntimeError: boom", text)

    def test_a_large_log_is_rotated_before_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "restore.log"
            log.write_text("x" * (cli._LOG_ROTATE_BYTES + 1), encoding="utf-8")
            with mock.patch.object(autostart.RegistryRunKey, "get", return_value=None):
                cli.main(["--log-file", str(log), "autostart", "status"])
            self.assertTrue(log.with_name("restore.log.1").is_file())
            self.assertLess(log.stat().st_size, cli._LOG_ROTATE_BYTES)


if __name__ == "__main__":
    unittest.main()
