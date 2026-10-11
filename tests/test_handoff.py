"""TkHandoff: the hidden window's handlers never call Tk inside Tk's loop.

The regression test reproduces the crash of 2026-10-02 (D30) with real Win32
messages and a real Tk loop.  Without the handoff it does not fail, it aborts
the interpreter ("Fatal Python error: PyEval_RestoreThread"), so it runs in a
child process and the test reads the child's exit code.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import tkinter as tk
import unittest
from pathlib import Path

from monitorctl.app.handoff import POLL_HIDDEN_MS, POLL_SHOWN_MS, TkHandoff

ROOT = Path(__file__).resolve().parents[1]


class TestTkHandoff(unittest.TestCase):
    def setUp(self) -> None:
        self.handoff = TkHandoff()
        self.calls: list[str] = []

    def test_without_tk_an_action_runs_at_once(self) -> None:
        self.handoff.run(lambda: self.calls.append("now"))
        self.assertEqual(self.calls, ["now"])

    def test_with_tk_actions_wait_for_the_poll_and_keep_their_order(self) -> None:
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        self.handoff.attach(root)
        self.handoff.run(lambda: self.calls.append("a"))
        self.handoff.run(lambda: self.calls.append("b"))
        self.assertEqual(self.calls, [])
        self.handoff._poll()
        self.assertEqual(self.calls, ["a", "b"])
        self.handoff.detach()

    def test_a_failing_action_does_not_stop_the_others(self) -> None:
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        self.handoff.attach(root)
        self.handoff.run(lambda: 1 / 0)
        self.handoff.run(lambda: self.calls.append("after"))
        with self.assertLogs("monitorctl.app.handoff", "ERROR"):
            self.handoff._poll()
        self.assertEqual(self.calls, ["after"])
        self.handoff.detach()

    def test_an_action_queued_by_an_action_waits_for_the_next_poll(self) -> None:
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        self.handoff.attach(root)
        self.handoff.run(lambda: self.handoff.run(lambda: self.calls.append("later")))
        self.handoff._poll()
        self.assertEqual(self.calls, [])
        self.handoff._poll()
        self.assertEqual(self.calls, ["later"])
        self.handoff.detach()

    def test_detach_drops_what_is_left_and_stops_polling(self) -> None:
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        self.handoff.attach(root)
        self.handoff.run(lambda: self.calls.append("dropped"))
        self.handoff.detach()
        self.assertFalse(self.handoff.attached)
        root.update()
        self.assertEqual(self.calls, [])
        self.handoff.run(lambda: self.calls.append("direct"))
        self.assertEqual(self.calls, ["direct"])

    def test_polls_slowly_while_hidden(self) -> None:
        self.assertLess(POLL_SHOWN_MS, POLL_HIDDEN_MS)
        self.assertLessEqual(POLL_HIDDEN_MS, 500)  # the tray menu must feel instant


#: Floods the hidden window with messages whose handler touches Tk, while Tk
#: callbacks keep firing - the shape of a slider drag with writes returning.
_STRESS = textwrap.dedent(
    """
    import sys, threading, time, tkinter as tk
    from monitorctl.app import win32
    from monitorctl.app.handoff import TkHandoff

    handoff = TkHandoff()
    window = win32.MessageWindow("monitorctl.test.handoff")
    root = tk.Tk()
    root.withdraw()
    if sys.argv[1] == "handoff":
        handoff.attach(root)
    label = tk.Label(root)
    done = []
    window.on(win32.WM_APP + 9, lambda _w, _l: handoff.run(
        lambda: done.append(label.configure(text=str(len(done))))) or 0)

    def tick():
        root.after(1, tick)

    def flood():
        end = time.monotonic() + 3
        while time.monotonic() < end:
            win32.post_message(window.hwnd, win32.WM_APP + 9)
            time.sleep(0.0005)

    root.after(1, tick)
    threading.Thread(target=flood, daemon=True).start()
    root.after(3600, root.quit)
    root.mainloop()
    handoff.detach()
    print(len(done))
    """
)


@unittest.skipUnless(sys.platform == "win32", "Win32 message loop")
class TestHiddenWindowUnderTk(unittest.TestCase):
    def run_stress(self, mode: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", _STRESS, mode],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_handlers_that_go_through_the_handoff_survive(self) -> None:
        result = self.run_stress("handoff")
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertGreater(int(result.stdout.strip()), 100)

    def test_the_hazard_is_real_without_it(self) -> None:
        # Keeps the test above honest: if this ever survives, the stress no
        # longer reproduces the crash and proves nothing.
        result = self.run_stress("direct")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PyEval_RestoreThread", result.stderr)


if __name__ == "__main__":
    unittest.main()
