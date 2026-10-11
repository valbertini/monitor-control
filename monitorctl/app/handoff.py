"""Hand work from the hidden window's handlers to Tk, once Tk owns the loop.

Why this exists
    Once the window is open, ``Tk.mainloop()`` pumps every message of the main
    thread, the hidden window's too (see ``tray.py``).  Its handlers are ctypes
    callbacks: Tcl dispatched the message from inside ``Tcl_DoOneEvent``, with
    the GIL released by ``_tkinter``, and ctypes took the GIL back without
    telling ``_tkinter``.  Any Tk call made there ends by clearing
    ``_tkinter``'s saved thread state; the next Tcl event of the same
    ``Tcl_DoOneEvent`` that calls Python - a slider binding, an ``after``
    timer - then finds no thread state and the interpreter aborts:
    ``Fatal Python error: PyEval_RestoreThread ... the GIL is released``.
    That killed the app three times on 2026-10-02 while a slider was dragged
    as write results came back (D30).

What it does
    Before Tk exists, a handler's work runs at once: nothing can go wrong
    without Tk.  Afterwards it is queued, and a Tk timer runs the queue - a
    Tk callback, entered the way ``_tkinter`` expects, where touching Tk is
    safe.  Nothing outside Tk can wake Tk without a Tk call, so the timer
    polls: briskly while the window is shown, slowly while it is hidden.
"""

from __future__ import annotations

import contextlib
import logging
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import tkinter as tk

__all__ = ["POLL_HIDDEN_MS", "POLL_SHOWN_MS", "TkHandoff"]

_log = logging.getLogger(__name__)

#: A write's result reaches the slider within this; well under what a person
#: notices after letting go of it.
POLL_SHOWN_MS = 50
#: Hidden, only the tray's menu and the keeper's results arrive this way.
#: Four wake-ups a second cost nothing measurable.
POLL_HIDDEN_MS = 250


class TkHandoff:
    """Runs callables where Tk may be called.  Main thread only."""

    def __init__(self) -> None:
        self._root: tk.Tk | None = None
        self._queue: deque[Callable[[], object]] = deque()
        self._timer: str | None = None

    @property
    def attached(self) -> bool:
        return self._root is not None

    def attach(self, root: tk.Tk) -> None:
        """Tk owns the loop from now on: queue, and start polling.

        Call it right after creating ``root``, before anything can pump
        messages (``mainloop``, ``update``, a dialog).
        """
        self._root = root
        self._timer = root.after(0, self._poll)

    def detach(self) -> None:
        """Tk's loop has ended, so the app is exiting: stop polling.

        What is still queued is dropped: showing the window or refreshing it
        after the user chose Exit would be wrong, not late.
        """
        root, self._root = self._root, None
        if root is not None and self._timer is not None:
            with contextlib.suppress(Exception):  # the interpreter may be gone
                root.after_cancel(self._timer)
        self._timer = None
        if self._queue:
            _log.debug("exiting: %d deferred action(s) dropped", len(self._queue))
            self._queue.clear()

    def run(self, action: Callable[[], object]) -> None:
        """Run ``action`` now if Tk is not up, else on the next poll."""
        if self._root is None:
            action()
        else:
            self._queue.append(action)

    def _run_queued(self) -> None:
        # Only what is queued now: an action that queues another must not
        # keep this loop going forever.
        for _ in range(len(self._queue)):
            action = self._queue.popleft()
            try:
                action()
            except Exception:
                _log.exception("a deferred action failed")

    def _poll(self) -> None:
        self._timer = None
        root = self._root
        if root is None:
            return
        self._run_queued()
        if self._root is not root:
            return  # an action ended Tk's loop and detached
        try:
            shown = root.state() != "withdrawn"
        except Exception:  # destroyed under us
            return
        self._timer = root.after(POLL_SHOWN_MS if shown else POLL_HIDDEN_MS, self._poll)
