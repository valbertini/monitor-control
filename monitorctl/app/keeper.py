"""Decides when the saved settings have to be pushed to the monitors again.

The problem is a monitor that forgets some controls when it is switched off and
then *lies* about it: it keeps reporting the forgotten values
(``ddc/quirks.py``).  Reading those controls back can never reveal the loss,
so the keeper watches for the power cycle instead, by three routes:

1. **Windows tells us.**  A DisplayPort monitor switched off disappears from the
   display configuration and reappears when switched on; resuming from sleep
   and unlocking the session are announced too.  Any of these is an *event*.
   Events are the cheap and reliable route, and they work in both modes.
2. **It stops answering.**  In :attr:`~RefreshMode.DETECT` mode every periodic
   check reads one control from each forgetful monitor - a read, never a write
   - only to see whether an answer comes back.  A monitor that did not answer
   and now does was off and is back.
3. **Brute force.**  In :attr:`~RefreshMode.REWRITE` mode every check simply
   pushes the forgetful controls again, through a neighbouring value, which is
   invisible on screen.  For a monitor whose power cycle neither route above
   notices.

Whatever the route, only forgetful controls and GPU adjustments are ever
written (``MonitorService.reassert_saved``).  A monitor that keeps its settings
gets no writes from the keeper at all, so a one-minute timer cannot become an
EEPROM-wear loop.

**Held off.**  Paused, or while the user edits in the window, nothing is
written - but what happens meanwhile is not forgotten: an event, a monitor
seen coming back (detect mode keeps reading while the window holds it), or
the pause itself leaves a re-apply *owed*, paid with one full push as soon as
the keeper is free (:meth:`Keeper.catch_up`).

Nothing in here sleeps, spawns threads or touches a window: the worker thread
calls :meth:`Keeper.tick`, :meth:`Keeper.handle_event` and
:meth:`Keeper.catch_up`, and the tests call
them directly.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

from ..ddc.errors import DdcError, SnapshotMissing
from ..ddc.service import MonitorService, MonitorState, Reassert, RestoreReport
from .settings import AppSettings, RefreshMode

__all__ = ["Event", "Keeper", "KeeperResult"]

_log = logging.getLogger(__name__)

#: While a monitor is missing, re-enumerate at most this often.  A monitor that
#: vanished from Windows leaves a dead handle behind, and only re-enumerating
#: gets a live one; but re-enumerating reads every capabilities string, which
#: is not something to do every minute for a monitor that is simply off.
RESCAN_WHILE_LOST_SECONDS = 5 * 60


class Event(Enum):
    """Something Windows reported that may mean a monitor lost its settings."""

    STARTUP = "startup"
    DISPLAY_CHANGED = "display configuration changed"
    MONITOR_ARRIVED = "monitor connected"
    RESUMED = "resumed from sleep"
    DISPLAY_ON = "displays switched back on"
    SESSION_UNLOCKED = "session unlocked"


@dataclass(frozen=True, slots=True)
class KeeperResult:
    """What one check or event led to.

    Attributes:
        reason: Why anything was written, in words for the log and the UI.
        report: The writes performed.
        rescanned: The monitor list was rebuilt, so ids may have changed and
            cached values were dropped; a window showing them must reload.
    """

    reason: str
    report: RestoreReport | None
    rescanned: bool = False


class Keeper:
    """The policy behind automatic re-applying."""

    def __init__(
        self,
        service: MonitorService,
        settings: Callable[[], AppSettings],
        *,
        suspended: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            service: The monitors.
            settings: Returns the current settings; read on every call, so a
                change in the window applies from the next check.
            suspended: Returns true while automatic writes must not happen -
                while the user is editing in the window, a timer pushing the
                saved values would undo every slider they move.
            clock: Monotonic time source, replaceable in tests.
        """
        self._service = service
        self._settings = settings
        self._suspended = suspended
        self._clock = clock
        self._lost: set[str] = set()
        #: Every forgetful monitor seen this session, so one that vanishes from
        #: the list is still looked for instead of forgotten.
        self._known: set[str] = set()
        #: EDID identities shared by more than one connected monitor.
        self._ambiguous: frozenset[str] = frozenset()
        self._last_rescan = clock()
        #: Why a re-apply is owed: something that may have cost a monitor its
        #: settings happened while the keeper could not write.  Paid with one
        #: full push as soon as it can (:meth:`catch_up`).
        self._owed: str | None = None

    def _identity(self, state: MonitorState) -> str:
        """What a forgetful monitor is known by across enumerations.

        Its EDID identity, unless two connected monitors share it (identical
        panels without a serial): then the identity is qualified by the
        position, so one unit answering is never taken for the other coming
        back - which would push the pivot at it on every check.
        """
        if state.hardware_id and state.hardware_id not in self._ambiguous:
            return state.hardware_id
        return f"{state.hardware_id}@{state.id}"

    def _note_ambiguous(self, states: Iterable[MonitorState]) -> None:
        counts = Counter(s.hardware_id for s in states if s.hardware_id)
        self._ambiguous = frozenset(h for h, n in counts.items() if n > 1)

    @property
    def lost(self) -> frozenset[str]:
        """Monitors that stopped answering and have not come back yet."""
        return frozenset(self._lost)

    @property
    def owed(self) -> str | None:
        """Why a full re-apply waits for the keeper to be free; ``None`` if none."""
        return self._owed

    def _owe(self, reason: str) -> None:
        if self._owed is None:
            _log.info(
                "%s while automatic writes were held; re-applying afterwards", reason
            )
            self._owed = reason

    def _idle(self) -> bool:
        if self._service.saved_snapshot() is None:
            return True  # nothing to put back, so nothing is ever owed either
        if self._settings().paused:
            # Checks skipped, events ignored: nothing tells what the monitors
            # went through meanwhile.  Unpausing pays for it with one push.
            self._owe("paused")
            return True
        return self._suspended()

    # ------------------------------------------------------------------ #
    # Catching up
    # ------------------------------------------------------------------ #

    def catch_up(self) -> KeeperResult | None:
        """Pay a re-apply owed since a pause or an edit, if the keeper is free.

        "Everything", as an event would: every forgetful monitor that answers
        gets its controls through the pivot, and every GPU adjustment is put
        back.  A forgetful monitor that does not answer is marked lost, and
        pushed by the checks when it does.
        """
        if self._owed is None or self._idle():
            return None
        reason, self._owed = self._owed, None
        states = self._service.list_monitors(probe=False)
        self._note_ambiguous(states)
        silent = {self._identity(s) for s in states if s.volatile and not self._ping(s)}
        self._lost |= silent
        targets = [s.id for s in states if self._identity(s) not in silent]
        if not targets:
            return None
        return self._push(Reassert.PIVOT, targets, reason=f"{reason}, held until now")

    # ------------------------------------------------------------------ #
    # Periodic check
    # ------------------------------------------------------------------ #

    def tick(self) -> KeeperResult | None:
        """Run one periodic check.  Returns ``None`` when nothing was written."""
        if self._idle():
            settings = self._settings()
            if (
                self._suspended()
                and not settings.paused
                and settings.mode is RefreshMode.DETECT
                and self._service.saved_snapshot() is not None
            ):
                # Held off by the window, not paused: still watch (reads
                # only), so a power cycle meanwhile is owed, not missed.
                self._watch_while_held()
            return None
        if self._owed is not None:
            return self.catch_up()
        monitors = self._service.list_monitors(probe=False)
        self._note_ambiguous(monitors)
        watched = [s for s in monitors if s.volatile]
        self._known.update(self._identity(s) for s in watched)
        if not watched and not self._lost:
            # Nothing forgetful is connected: no bus traffic at all.
            return None
        if self._settings().mode is RefreshMode.REWRITE:
            result = self._push(
                Reassert.NEIGHBOUR,
                [s.id for s in watched],
                reason="periodic rewrite",
            )
            failed = result.report is not None and result.report.failed
            if failed and self._clock() - self._last_rescan >= RESCAN_WHILE_LOST_SECONDS:
                # Most likely handles that died with a power cycle: without a
                # fresh enumeration every later check would fail the same way.
                self._rescan()
                return KeeperResult(result.reason, result.report, rescanned=True)
            return result
        return self._detect(watched)

    def _detect(self, watched: list[MonitorState]) -> KeeperResult | None:
        came_back: set[str] = set()
        for state in watched:
            key = self._identity(state)
            if self._ping(state):
                if key in self._lost:
                    self._lost.discard(key)
                    came_back.add(key)
            elif key not in self._lost:
                _log.info("%s stopped answering; watching for its return", state.name)
                self._lost.add(key)

        rescanned = False
        if self._lost and self._clock() - self._last_rescan >= RESCAN_WHILE_LOST_SECONDS:
            # The lost monitor is not in the list any more, or it is but its
            # handle died with the power cycle - a dead handle never answers
            # again, so pinging it forever would never see the monitor return.
            # Only a fresh enumeration gets a live one.
            states = self._rescan()
            rescanned = True
            for state in states:
                key = self._identity(state)
                if key in self._lost and state.volatile and self._ping(state):
                    self._lost.discard(key)
                    came_back.add(key)

        if not came_back:
            return KeeperResult("rescan", None, rescanned=True) if rescanned else None
        # Resolved by identity against the *current* list: a rescan above may
        # have renumbered the ids of monitors that came back by ping.
        targets = [
            s
            for s in self._service.list_monitors(probe=False)
            if self._identity(s) in came_back
        ]
        names = ", ".join(s.name for s in targets)
        result = self._push(
            Reassert.PIVOT, [s.id for s in targets], reason=f"{names} came back on"
        )
        return KeeperResult(result.reason, result.report, rescanned=rescanned)

    def _watch_while_held(self) -> None:
        """Detection without the push: a monitor back from a power cycle is
        noted as owed, since nothing may be written while the user edits.

        No rescan here - it would redraw the window under the user; a dead
        handle waits for the checks that follow the edit.
        """
        monitors = self._service.list_monitors(probe=False)
        self._note_ambiguous(monitors)
        for state in monitors:
            if not state.volatile:
                continue
            key = self._identity(state)
            self._known.add(key)
            if self._ping(state):
                if key in self._lost:
                    self._lost.discard(key)
                    self._owe(f"{state.name} came back on")
            elif key not in self._lost:
                _log.info("%s stopped answering; watching for its return", state.name)
                self._lost.add(key)

    def _ping(self, state: MonitorState) -> bool:
        try:
            return self._service.ping(state.id)
        except DdcError:
            return False

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #

    def handle_event(self, event: Event) -> KeeperResult | None:
        """React to something Windows reported.

        The monitor list is rebuilt first, because the event may well be the
        reason it changed - a monitor that came back may have a new device name
        and needs a live handle.  That happens even while the keeper is paused
        or the user is editing: it writes nothing, and skipping it would leave
        the service holding handles that died with the change.

        Then every forgetful monitor that answers gets its controls pushed
        through the pivot, and every GPU adjustment is put back, since a display
        change resets the gamma ramp.  A forgetful monitor that is still listed
        but does not answer - switched off on a connection that keeps it
        enumerated - is marked lost instead, so the checks push it when it
        answers again rather than counting a failed push as done.
        """
        if event is Event.STARTUP and (
            not self._settings().apply_on_start or self._idle()
        ):
            # Nothing is cached before the first enumeration, so there are no
            # handles to renew either.
            return None
        states = self._rescan()
        present = {self._identity(s) for s in states}
        self._known.update(self._identity(s) for s in states if s.volatile)
        # A forgetful monitor that the event took away is now lost: the next
        # checks look for it, and push its settings when it is back.
        gone = self._known - present
        if self._idle():
            self._lost = gone | (self._lost & present)
            if self._service.saved_snapshot() is not None:
                # Held off (paused, or the user editing): whatever the event
                # cost the monitors is put back once the keeper is free.
                self._owe(event.value)
            return KeeperResult(event.value, None, rescanned=True)
        self._owed = None  # what follows is the same full push
        silent = {self._identity(s) for s in states if s.volatile and not self._ping(s)}
        for state in states:
            if (
                self._identity(state) in silent
                and self._identity(state) not in self._lost
            ):
                _log.info("%s does not answer; watching for its return", state.name)
        self._lost = gone | silent
        targets = [s.id for s in states if self._identity(s) not in silent]
        if not targets:
            return KeeperResult(event.value, None, rescanned=True)
        result = self._push(Reassert.PIVOT, targets, reason=event.value)
        return KeeperResult(result.reason, result.report, rescanned=True)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _rescan(self) -> tuple[MonitorState, ...]:
        self._last_rescan = self._clock()
        try:
            states = self._service.refresh(probe=False)
        except DdcError as exc:
            _log.warning("could not enumerate monitors: %s", exc)
            return ()
        self._note_ambiguous(states)
        return states

    def _push(
        self, reassert: Reassert, monitor_ids: list[str] | None, *, reason: str
    ) -> KeeperResult:
        try:
            report = self._service.reassert_saved(
                reassert=reassert, monitor_ids=monitor_ids
            )
        except SnapshotMissing:
            return KeeperResult(reason, None)
        if report.applied or report.failed:
            _log.info(
                "re-applied after %s: %d written, %d failed",
                reason,
                len(report.applied),
                len(report.failed),
            )
        for issue in report.failed:
            _log.warning(
                "could not re-apply %s :: %s - %s",
                issue.monitor_name,
                issue.key,
                issue.reason,
            )
        return KeeperResult(reason, report)
