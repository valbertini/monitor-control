"""The tray application: one process, one UI thread, one bus thread.

Threads
    *Main* runs the Win32 message loop: the tray icon, the hidden window that
    receives Windows' notifications, and - once opened - the Tk window.
    *Bus* (:class:`~monitorctl.app.worker.Worker`) does every DDC/CI operation.

One message loop, two owners
    Until the window is first opened, the main thread pumps messages with a
    plain ``GetMessage`` loop and Tk is not even imported.  Opening the window
    ends that loop and hands the thread to ``Tk.mainloop()`` for the rest of
    the process.  That works because Tk's Windows notifier dispatches *every*
    message of the thread, including the hidden window's: the tray keeps
    answering while Tk owns the loop, and nothing ever runs Tk off the main
    thread.  The hidden window's handlers must still never call Tk
    themselves while Tk owns the loop - that aborts the interpreter (see
    ``handoff.py``) - so whatever of theirs may touch the window goes through
    :class:`~monitorctl.app.handoff.TkHandoff`.  The window is hidden, not
    destroyed, when closed; re-creating a Tk interpreter costs more than
    keeping the idle one.

Getting results back
    The bus thread never calls the UI.  It queues a result and posts
    ``WM_APP_WORKER`` to the hidden window, and the main thread drains the
    queue - at once in the plain loop, from a Tk timer once Tk owns it.
    ``PostMessageW`` is thread-safe and never blocks, so a slow window cannot
    stall the bus and vice versa.
"""

from __future__ import annotations

import contextlib
import logging
import time
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING

from .. import autostart
from ..ddc.profiles import ProfileStore, default_profiles_path
from ..ddc.service import MonitorService
from ..ddc.snapshot import legacy_snapshot_path
from ..gpu import GpuController
from . import win32
from .handoff import TkHandoff
from .keeper import Event, Keeper, KeeperResult
from .settings import AppSettings, RefreshMode, SettingsStore, default_settings_path
from .worker import Worker

if TYPE_CHECKING:
    from .window import MainWindow

__all__ = ["CLASS_NAME", "WM_APP_SHOW", "TrayApp"]

_log = logging.getLogger(__name__)

#: Window class of the hidden window; a second launch finds the first by it.
CLASS_NAME = "monitorctl.tray"

WM_APP_TRAY = win32.WM_APP + 1
WM_APP_WORKER = win32.WM_APP + 2
#: Posted by a second launch: "show the window".
WM_APP_SHOW = win32.WM_APP + 3

#: After logon the display stack is still settling, and monitors that were
#: just powered answer DDC/CI late; the first apply waits this long.
STARTUP_DELAY_SECONDS = 10.0

ICON_PATH = Path(__file__).resolve().parent / "assets" / "monitorctl.ico"


class Command(IntEnum):
    OPEN = 1
    RESTORE = 2
    SAVE = 3
    PAUSE = 4
    AUTOSTART = 5
    EXIT = 9


class TrayApp:
    """Owns every long-lived object of the app and the main thread's loop."""

    def __init__(
        self,
        service: MonitorService,
        profiles: ProfileStore,
        *,
        started_with_windows: bool,
    ) -> None:
        """
        Args:
            service: The monitors.
            profiles: The store ``service`` saves to.  The same object, not a
                second one on the same file, so the tray menu's idea of
                whether anything is saved is the service's.
            started_with_windows: Launched by the Run key.
        """
        self.settings = SettingsStore(default_settings_path())
        self.profiles = profiles
        self.service = service
        self._started_with_windows = started_with_windows
        self._editing = False
        self._exiting = False
        self._want_window = not started_with_windows
        self._ui: MainWindow | None = None
        self._last_action = ""
        #: Last console display state Windows reported: 0 off, 1 on, 2 dimmed.
        self._display_state: int | None = None
        #: Where the hidden window's handlers send anything that may touch Tk.
        self._handoff = TkHandoff()

        self.keeper = Keeper(
            self.service, self.settings.get, suspended=lambda: self._editing
        )
        self.window = win32.MessageWindow(CLASS_NAME)
        self.worker = Worker(
            self.service,
            self.keeper,
            self.settings.get,
            notify=lambda: win32.post_message(self.window.hwnd, WM_APP_WORKER),
            on_keeper=self._keeper_acted,
        )
        self.tray = win32.TrayIcon(self.window, WM_APP_TRAY, ICON_PATH)

    @classmethod
    def create(cls, *, started_with_windows: bool) -> TrayApp:
        from ..ddc.buslock import BusLock
        from ..ddc.windows import WindowsDdcBackend

        store = ProfileStore(default_profiles_path(), legacy_path=legacy_snapshot_path())
        service = MonitorService(
            WindowsDdcBackend(), profiles=store, gpu=GpuController(), bus_lock=BusLock()
        )
        return cls(service, store, started_with_windows=started_with_windows)

    # ------------------------------------------------------------------ #
    # Life cycle
    # ------------------------------------------------------------------ #

    def run(self) -> int:
        self._wire()
        self.tray.show()
        self._update_tooltip()
        self.worker.start()
        self.worker.post_event(
            Event.STARTUP,
            delay=STARTUP_DELAY_SECONDS if self._started_with_windows else 1.0,
        )
        _log.info(
            "tray app running (started with Windows: %s)", self._started_with_windows
        )
        try:
            while not self._exiting:
                if self._want_window:
                    self._want_window = False
                    self._run_window()  # returns only when the app exits
                else:
                    win32.pump_messages()
        finally:
            self._shutdown()
        return 0

    def _wire(self) -> None:
        w = self.window
        w.on(WM_APP_TRAY, self._on_tray)
        w.on(WM_APP_WORKER, lambda _wp, _lp: self._drain())
        w.on(WM_APP_SHOW, lambda _wp, _lp: self._handoff.run(self.open_window) or 0)
        w.on(w.register_message("TaskbarCreated"), self._on_taskbar_created)
        w.on(win32.WM_DISPLAYCHANGE, lambda _wp, _lp: self._event(Event.DISPLAY_CHANGED))
        w.on(win32.WM_DEVICECHANGE, self._on_device_change)
        w.on(win32.WM_POWERBROADCAST, self._on_power)
        w.on(win32.WM_WTSSESSION_CHANGE, self._on_session)
        w.on(win32.WM_QUERYENDSESSION, lambda _wp, _lp: 1)
        w.on(win32.WM_CLOSE, lambda _wp, _lp: self._handoff.run(self.exit) or 0)
        w.on(win32.WM_ENDSESSION, self._on_end_session)
        w.watch_monitors()
        w.watch_display_power()
        w.watch_session()

    def _run_window(self) -> None:
        import tkinter as tk

        from .window import MainWindow

        root = tk.Tk()
        # Before anything can pump a message: from here on the hidden window's
        # handlers must not call Tk themselves.
        self._handoff.attach(root)
        try:
            root.withdraw()
            self._ui = MainWindow(
                root, self, str(ICON_PATH) if ICON_PATH.is_file() else None
            )
            self._ui.show()
            root.mainloop()
        finally:
            self._handoff.detach()
        self._exiting = True

    def open_window(self) -> None:
        if self._ui is not None:
            self._ui.show()
            return
        # Still in the plain message loop: end it so run() can start Tk.
        self._want_window = True
        win32.post_quit()

    def exit(self) -> None:
        self._exiting = True
        if self._ui is not None:
            self._ui.root.quit()
        else:
            win32.post_quit()

    def _shutdown(self) -> None:
        _log.info("shutting down")
        self.worker.stop()
        self.tray.remove()
        if self._ui is not None:
            with contextlib.suppress(Exception):  # interpreter may be gone already
                self._ui.root.destroy()
        self.window.destroy()
        self.service.close()

    # ------------------------------------------------------------------ #
    # Bridge used by the window
    # ------------------------------------------------------------------ #

    def settings_changed(self) -> None:
        self.worker.reschedule()
        self._update_tooltip()

    def set_editing(self, editing: bool) -> None:
        was, self._editing = self._editing, editing
        if was and not editing:
            # Whatever the keeper had to let pass meanwhile is paid now.
            self.worker.nudge()

    def window_hidden(self) -> None:
        # The window's pages are not needed until it is opened again.
        win32.trim_working_set()

    def tray_status(self) -> str:
        settings = self.settings.get()
        if settings.paused:
            return "Automatic re-apply paused"
        how = "detect" if settings.mode is RefreshMode.DETECT else "rewrite"
        minutes = settings.interval_minutes
        every = "every minute" if minutes == 1 else f"every {minutes} min"
        return f"Automatic re-apply: {every} ({how})"

    # ------------------------------------------------------------------ #
    # Notifications
    # ------------------------------------------------------------------ #

    def _event(self, event: Event) -> None:
        _log.info("Windows reported: %s", event.value)
        self.worker.post_event(event)

    def _on_device_change(self, wparam: int, lparam: int) -> None:
        if win32.is_monitor_arrival(wparam, lparam):
            self._event(Event.MONITOR_ARRIVED)

    def _on_power(self, wparam: int, lparam: int) -> int:
        if wparam in (win32.PBT_APMRESUMEAUTOMATIC, win32.PBT_APMRESUMESUSPEND):
            self._event(Event.RESUMED)
        elif wparam == win32.PBT_POWERSETTINGCHANGE:
            state = win32.display_power_state(lparam)
            if state is not None:
                # Registering delivers the current state at once; only a
                # change *to* on means the monitors may have lost power.
                if state == 1 and self._display_state in (0, 2):
                    self._event(Event.DISPLAY_ON)
                self._display_state = state
        return 1

    def _on_session(self, wparam: int, _lparam: int) -> None:
        if wparam == win32.WTS_SESSION_UNLOCK:
            self._event(Event.SESSION_UNLOCKED)

    def _on_end_session(self, wparam: int, _lparam: int) -> int:
        if wparam:
            # Windows is logging off: there is no time for a graceful exit,
            # only for not leaving a ghost icon behind.
            self.tray.remove()
        return 0

    def _on_taskbar_created(self, _wparam: int, _lparam: int) -> int:
        _log.info("Explorer restarted; adding the tray icon again")
        self.tray.show()
        self._update_tooltip()
        return 0

    def _drain(self) -> int:
        self._handoff.run(self.worker.drain)
        return 0

    # ------------------------------------------------------------------ #
    # Tray icon
    # ------------------------------------------------------------------ #

    def _on_tray(self, wparam: int, lparam: int) -> int:
        event = win32.loword(lparam)
        if event in (win32.NIN_SELECT, win32.NIN_KEYSELECT, win32.WM_LBUTTONDBLCLK):
            self._handoff.run(self.open_window)
        elif event == win32.WM_CONTEXTMENU:
            x, y = win32.signed_xy(wparam)
            self._menu(x, y)
        return 0

    def _menu(self, x: int, y: int) -> None:
        settings = self.settings.get()
        try:
            autostart_on = autostart.is_enabled()
        except autostart.AutostartError:
            autostart_on = False
        has_snapshot = self.profiles.load() is not None
        items = [
            win32.MenuItem("Open monitorctl", Command.OPEN, default=True),
            win32.SEPARATOR,
            win32.MenuItem(
                "Restore saved settings", Command.RESTORE, enabled=has_snapshot
            ),
            win32.MenuItem("Save current settings", Command.SAVE),
            win32.SEPARATOR,
            win32.MenuItem(
                "Pause automatic re-apply", Command.PAUSE, checked=settings.paused
            ),
            win32.MenuItem("Start with Windows", Command.AUTOSTART, checked=autostart_on),
            win32.SEPARATOR,
            win32.MenuItem("Exit", Command.EXIT),
        ]
        # The menu itself needs no Tk; what the chosen command does may.
        command = self.tray.popup(items, x, y)
        if command:
            self._handoff.run(lambda: self._command(command, settings, autostart_on))

    def _command(self, command: int, settings: AppSettings, autostart_on: bool) -> None:
        if command == Command.OPEN:
            self.open_window()
        elif command == Command.RESTORE:
            self._restore_from_tray()
        elif command == Command.SAVE:
            self._save_from_tray()
        elif command == Command.PAUSE:
            self.settings.update(paused=not settings.paused)
            self.settings_changed()
            if self._ui is not None:
                self._ui.paused_var.set(not settings.paused)
                self._ui.refresh_keeper_label()
        elif command == Command.AUTOSTART:
            now_on = autostart_on  # unchanged unless the registry agreed
            try:
                if autostart_on:
                    autostart.disable()
                else:
                    autostart.enable()
                now_on = not autostart_on
            except autostart.AutostartError as exc:
                _log.warning("%s", exc)
            if self._ui is not None:
                self._ui.autostart_var.set(now_on)
        elif command == Command.EXIT:
            # Only the menu asks about unsaved changes; WM_CLOSE (``--quit``,
            # the build script, logoff) must never wait for a person.
            if self._ui is not None:
                self._ui.confirm_exit(self.exit)
            else:
                self.exit()

    def _window_open(self) -> bool:
        return self._ui is not None and self._ui.root.state() != "withdrawn"

    def _restore_from_tray(self) -> None:
        from ..ddc.service import Reassert

        if self._window_open():
            # The window knows about changes waiting for Apply; its Restore
            # discards them, as the button would.
            self._ui.restore()
            return

        def done(report: object, error: BaseException | None) -> None:
            if error is not None:
                self._note(f"restore failed: {error}")
            else:
                self._note(f"restored, {len(report.applied)} written")
            if self._ui is not None:
                self._ui.stale = True
                if self._ui.root.state() != "withdrawn":
                    self._ui.load(probe=False)

        self.worker.submit(
            lambda s: s.restore_snapshot(reassert=Reassert.PIVOT), done, label="Restoring"
        )

    def _save_from_tray(self) -> None:
        from ..ddc.service import Reassert

        if self._window_open():
            # What the window shows is what is saved: its Save applies the
            # changes waiting there first.
            self._ui.save()
            return

        def run(service: MonitorService) -> object:
            # Read first: the tray shows no values, so the cache may hold ones
            # the monitor's own buttons have changed since.
            snapshot = service.save_snapshot(reread=True)
            report = service.reassert_saved(reassert=Reassert.PIVOT)
            return snapshot, report

        def done(result: object, error: BaseException | None) -> None:
            if error is not None:
                self._note(f"save failed: {error}")
                return
            snapshot, report = result
            if report.failed:
                # Saved, but the screen may not show it: say so, as the CLI does.
                issue = report.failed[0]
                self._note(
                    f"saved {snapshot.value_count} settings; {len(report.failed)} "
                    f"not pushed ({issue.monitor_name} {issue.key}: {issue.reason})"
                )
            else:
                self._note(f"saved {snapshot.value_count} settings")
            if self._ui is not None:
                self._ui.stale = True
                if self._ui.root.state() != "withdrawn":
                    self._ui.load(probe=False)

        self.worker.submit(run, done, label="Saving")

    def _keeper_acted(self, result: KeeperResult) -> None:
        written = len(result.report.applied) if result.report else 0
        if written:
            self._note(f"re-applied {written} ({result.reason})")
        if self._ui is not None:
            self._ui.keeper_changed(result.rescanned, result.reason, written)
        else:
            # Startup and every re-apply touch code and data that will not be
            # needed again until the next one; hand those pages back.
            win32.trim_working_set()

    def _note(self, action: str) -> None:
        self._last_action = f"{time.strftime('%H:%M')} {action}"
        _log.info("%s", action)
        self._update_tooltip()

    def _update_tooltip(self) -> None:
        lines = ["monitorctl", self.tray_status()]
        if self._last_action:
            lines.append(self._last_action)
        self.tray.set_tooltip("\n".join(lines)[:127])
