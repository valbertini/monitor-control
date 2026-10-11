"""The single thread that talks to the display bus.

Every DDC/CI operation is slow by design - paced at tens of milliseconds, and a
full probe takes seconds - so none of it may run on the thread that draws the
window or answers the tray icon.  This thread owns that work:

* **Jobs** submitted by the UI (read everything, write one control, save,
  restore) run in order, one at a time.
* **Events** reported by Windows (display change, resume, ...) are coalesced:
  they arrive in bursts, and the monitors need a moment after one before they
  answer, so the keeper runs once, a few seconds after the last of a burst.
* **Checks** run every ``interval_minutes``.

Results travel back through a queue.  The thread then calls ``notify`` - which
in the app posts a message to the hidden tray window - and the main thread
calls :meth:`Worker.drain` to run the callbacks where they may touch the UI.
Nothing here ever calls into Tk, so nothing here can deadlock on it.

The thread survives anything a job throws: the exception is handed to the
job's callback and logged, and the next job runs.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..ddc.service import MonitorService
from .keeper import Event, Keeper, KeeperResult
from .settings import AppSettings

__all__ = ["EVENT_SETTLE_SECONDS", "Worker"]

_log = logging.getLogger(__name__)

#: Wait after the last event of a burst before acting.  A monitor that has just
#: been switched on, or a display stack that has just been reconfigured, does
#: not answer DDC/CI reliably straight away.
EVENT_SETTLE_SECONDS = 3.0

#: The order in which simultaneous events are reported in the log: the most
#: specific reason wins.
_EVENT_PRIORITY = (
    Event.STARTUP,
    Event.MONITOR_ARRIVED,
    Event.RESUMED,
    Event.DISPLAY_ON,
    Event.SESSION_UNLOCKED,
    Event.DISPLAY_CHANGED,
)

Callback = Callable[[Any, BaseException | None], None]


@dataclass(slots=True)
class _Job:
    run: Callable[[MonitorService], Any]
    on_done: Callback | None
    label: str


@dataclass(slots=True)
class _Done:
    on_done: Callback
    result: Any = None
    error: BaseException | None = None


_STOP = object()


@dataclass(slots=True)
class _Pending:
    events: set[Event] = field(default_factory=set)
    due: float | None = None


class Worker:
    """Runs jobs, events and periodic checks on one background thread."""

    def __init__(
        self,
        service: MonitorService,
        keeper: Keeper,
        settings: Callable[[], AppSettings],
        *,
        notify: Callable[[], None],
        on_keeper: Callable[[KeeperResult], None] | None = None,
        settle_seconds: float = EVENT_SETTLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            service: The monitors; only ever touched from this thread.
            keeper: Decides what checks and events do.
            settings: Current settings, read before every wait.
            notify: Called from this thread when results are waiting.  Must be
                thread-safe and must not block (``PostMessageW`` is both).
            on_keeper: Called on the *main* thread, from :meth:`drain`, with
                every keeper result that wrote something or rescanned.
            settle_seconds: Delay between the last event of a burst and acting.
            clock: Monotonic time source.
        """
        self._service = service
        self._keeper = keeper
        self._settings = settings
        self._notify = notify
        self._on_keeper = on_keeper
        self._settle = settle_seconds
        self._clock = clock

        self._jobs: queue.Queue[object] = queue.Queue()
        self._done: queue.Queue[_Done] = queue.Queue()
        self._lock = threading.Lock()
        self._pending = _Pending()
        #: The keeper may have just been freed (see :meth:`nudge`).
        self._nudged = False
        self._next_check = clock() + settings().interval_seconds
        self._stopping = False
        self._thread = threading.Thread(
            target=self._loop, name="monitorctl-bus", daemon=True
        )

    # ------------------------------------------------------------------ #
    # Called from the main thread
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Finish the job in progress, drop the rest, and end the thread.

        A write is never interrupted halfway: half of a two-step write would
        leave a control at the intermediate value.  Jobs still queued are
        dropped: the queue is first in, first out, so a stop request posted
        behind a restore would otherwise wait for every one of its writes.
        """
        self._stopping = True
        self._jobs.put(_STOP)
        self._thread.join(timeout)
        if self._thread.is_alive():
            _log.warning("the bus thread did not stop within %.0fs", timeout)

    def submit(
        self,
        run: Callable[[MonitorService], Any],
        on_done: Callback | None = None,
        *,
        label: str = "",
    ) -> None:
        """Queue ``run(service)``; ``on_done(result, error)`` runs in :meth:`drain`."""
        self._jobs.put(_Job(run, on_done, label))

    def post_event(self, event: Event, *, delay: float | None = None) -> None:
        """Note that ``event`` happened; act on the burst once it settles."""
        with self._lock:
            self._pending.events.add(event)
            self._pending.due = self._clock() + (self._settle if delay is None else delay)
        self._jobs.put(None)  # wake the loop so it recomputes its deadline

    def reschedule(self) -> None:
        """Settings changed: restart the countdown to the next check.

        Also a :meth:`nudge`: the change may be the end of a pause.
        """
        with self._lock:
            self._next_check = self._clock() + self._settings().interval_seconds
            self._nudged = True
        self._jobs.put(None)

    def nudge(self) -> None:
        """The keeper may be free again (a pause or an edit ended): pay what
        it owes now rather than at the next check, minutes away."""
        with self._lock:
            self._nudged = True
        self._jobs.put(None)

    def drain(self) -> None:
        """Run the callbacks of finished work.  Main thread only."""
        while True:
            try:
                done = self._done.get_nowait()
            except queue.Empty:
                return
            try:
                done.on_done(done.result, done.error)
            except Exception:
                _log.exception("a result callback failed")

    # ------------------------------------------------------------------ #
    # The thread
    # ------------------------------------------------------------------ #

    def _deadline(self) -> float:
        with self._lock:
            due = self._pending.due
            return self._next_check if due is None else min(due, self._next_check)

    def _loop(self) -> None:
        while True:
            timeout = max(0.0, self._deadline() - self._clock())
            try:
                item = self._jobs.get(timeout=timeout)
            except queue.Empty:
                item = None
            if item is _STOP or self._stopping:
                return
            if isinstance(item, _Job):
                self._run_job(item)
                if self._stopping:
                    return
            self._run_due()

    def _run_due(self) -> None:
        now = self._clock()
        events: set[Event] = set()
        with self._lock:
            if self._pending.due is not None and now >= self._pending.due:
                events = self._pending.events
                self._pending = _Pending()
            check = now >= self._next_check
            if check:
                self._next_check = now + self._settings().interval_seconds
            nudged, self._nudged = self._nudged, False
        if events:
            event = next(e for e in _EVENT_PRIORITY if e in events)
            self._keep(lambda: self._keeper.handle_event(event), f"event: {event.value}")
        elif check:
            self._keep(self._keeper.tick, "periodic check")
        elif nudged:
            self._keep(self._keeper.catch_up, "catching up")

    def _keep(self, action: Callable[[], KeeperResult | None], label: str) -> None:
        try:
            result = action()
        except Exception:
            _log.exception("%s failed", label)
            return
        if result is not None and self._on_keeper is not None:
            callback = self._on_keeper
            self._finish(_Done(lambda r, _e: callback(r), result))

    def _run_job(self, job: _Job) -> None:
        try:
            result, error = job.run(self._service), None
        except Exception as exc:
            if job.on_done is None:
                _log.exception("background job %r failed", job.label)
            else:
                _log.info("background job %r failed: %s", job.label, exc)
            result, error = None, exc
        if job.on_done is not None:
            self._finish(_Done(job.on_done, result, error))

    def _finish(self, done: _Done) -> None:
        self._done.put(done)
        try:
            self._notify()
        except Exception:
            _log.exception("could not wake the main thread")
