"""Start the tray app with Windows, through the per-user Run key.

``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` is the conventional
place for a tray application, and the right one here:

* it needs no administrator rights;
* it shows up in Task Manager's *Startup apps* tab, so the user can turn it off
  there without opening this program - which is also why the registry, not the
  settings file, is the source of truth for whether it is enabled;
* it is one value, removed with one call.

The Run key cannot delay a start.  It does not need to: the app shows its icon
at once and waits on its own before touching the monitors (``app/tray.py``).

**Portability.**  The value holds an absolute path.  When the portable folder
is moved, the old value points nowhere; the app notices on its next start and
rewrites it (:func:`repair`).

**The scheduled task.**  An earlier version restored the snapshot at logon with
a scheduled task named ``monitorctl-restore``.  With the tray app running, that
task would apply the same settings a second time, so enabling the app's start
removes it.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Protocol

__all__ = [
    "APP_EXECUTABLE",
    "LEGACY_TASK_NAME",
    "RUN_KEY",
    "STARTUP_FLAG",
    "VALUE_NAME",
    "AutostartError",
    "RegistryRunKey",
    "RunKey",
    "disable",
    "enable",
    "is_enabled",
    "launch_command",
    "remove_legacy_task",
    "repair",
]

_log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "monitorctl"
LEGACY_TASK_NAME = "monitorctl-restore"

#: Passed by the Run key so the app knows it was started with Windows.
STARTUP_FLAG = "--startup"

#: The tray app's executable in the packaged folder (``packaging/monitorctl.spec``).
APP_EXECUTABLE = "monitorctl.exe"


class AutostartError(Exception):
    """The Run key could not be read or written."""


class RunKey(Protocol):
    """The three operations needed on the Run key; replaceable in tests."""

    def get(self, name: str) -> str | None: ...
    def set(self, name: str, command: str) -> None: ...
    def delete(self, name: str) -> None: ...


class RegistryRunKey:
    """The real ``HKCU`` Run key."""

    def get(self, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, kind = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AutostartError(f"could not read the Run key: {exc}") from exc
        return value if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else None

    def set(self, name: str, command: str) -> None:
        import winreg

        try:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
        except OSError as exc:
            raise AutostartError(f"could not write the Run key: {exc}") from exc

    def delete(self, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise AutostartError(f"could not write the Run key: {exc}") from exc


def _default_key() -> RunKey:
    return RegistryRunKey()


def launch_command() -> str:
    """The command line that starts this very installation of the app.

    Packaged, that is ``monitorctl.exe`` in the running executable's folder -
    *not* the running executable, which is ``monitorctl-cli.exe`` when the
    command comes from ``monitorctl-cli autostart enable``: the CLI knows no
    ``--startup`` and would fail at every logon.  From source it is the windowless
    interpreter running ``launch_app.pyw`` next to the package, which puts the
    checkout on ``sys.path`` - the Run key has no working directory to rely on.
    """
    if getattr(sys, "frozen", False):
        app = Path(sys.executable).resolve().with_name(APP_EXECUTABLE)
        return f'"{app}" {STARTUP_FLAG}'
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    interpreter = windowless if windowless.is_file() else executable
    launcher = Path(__file__).resolve().parent.parent / "launch_app.pyw"
    return f'"{interpreter}" "{launcher}" {STARTUP_FLAG}'


def is_enabled(key: RunKey | None = None) -> bool:
    """Whether *some* monitorctl is set to start with Windows."""
    return (key or _default_key()).get(VALUE_NAME) is not None


def enable(key: RunKey | None = None) -> str:
    """Start this installation with Windows; returns the command registered.

    Raises:
        AutostartError: If the registry refused the write.
    """
    command = launch_command()
    (key or _default_key()).set(VALUE_NAME, command)
    _log.info("start with Windows enabled: %s", command)
    remove_legacy_task()
    return command


def disable(key: RunKey | None = None) -> bool:
    """Stop starting with Windows.  Returns ``False`` if it was not enabled."""
    key = key or _default_key()
    if key.get(VALUE_NAME) is None:
        return False
    key.delete(VALUE_NAME)
    _log.info("start with Windows disabled")
    return True


def _installation_exists(command: str) -> bool:
    """Whether every program a registered command line names is still there.

    The paths are the quoted parts, which is how :func:`launch_command` writes
    them.  A command with nothing quoted was not written by this module, so it
    is assumed to work rather than replaced.
    """
    paths = command.split('"')[1::2]
    return all(Path(path).is_file() for path in paths)


def repair(key: RunKey | None = None) -> bool:
    """Point an enabled Run value at this installation if its own is gone.

    Called at every start, so that a portable folder that was moved keeps
    starting with Windows.  Two things it must never do:

    * turn a start back on that the user turned off - only an *enabled* value
      is touched;
    * take the start over from another installation that still exists.
      Launching a second copy - the fresh build in ``dist``, or the source
      checkout - to try something would otherwise silently repoint the Run key
      at it, and the next rebuild would delete what Windows starts.

    Returns whether the value was rewritten.
    """
    key = key or _default_key()
    current = key.get(VALUE_NAME)
    command = launch_command()
    if current is None or current == command or _installation_exists(current):
        return False
    key.set(VALUE_NAME, command)
    _log.info("start with Windows pointed at %r, which is gone; now %r", current, command)
    return True


def remove_legacy_task() -> bool:
    """Delete the logon task of earlier versions, if it exists.

    Failure is logged, not raised: the task only duplicates work the app does
    anyway, and a stray duplicate is no reason to refuse enabling the app.
    """
    if sys.platform != "win32":
        return False
    try:
        query = subprocess.run(
            ["schtasks", "/Query", "/TN", LEGACY_TASK_NAME],
            capture_output=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if query.returncode != 0:
            return False
        deleted = subprocess.run(
            ["schtasks", "/Delete", "/TN", LEGACY_TASK_NAME, "/F"],
            capture_output=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as exc:
        _log.warning("could not check for the old logon task: %s", exc)
        return False
    if deleted.returncode != 0:
        _log.warning("could not remove the old logon task %r", LEGACY_TASK_NAME)
        return False
    _log.info("removed the old logon task %r; the tray app replaces it", LEGACY_TASK_NAME)
    return True
