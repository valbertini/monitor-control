"""Tests for the cross-process display-bus lock.

A Windows mutex is owned by a *thread*, so two threads with their own handles
on the same name behave exactly like two processes would - which is what makes
this testable without spawning one.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
import uuid

if sys.platform == "win32":
    from monitorctl.ddc.buslock import BusLock

TIMEOUT = 5.0


@unittest.skipUnless(sys.platform == "win32", "Windows named mutex")
class TestBusLock(unittest.TestCase):
    def setUp(self) -> None:
        # A unique name, so a running monitorctl never meets the test.
        self.name = rf"Local\monitorctl-test-{uuid.uuid4()}"

    def lock(self) -> BusLock:
        lock = BusLock(self.name)
        self.addCleanup(lock.close)
        return lock

    def test_a_second_holder_waits_for_the_first(self) -> None:
        holding = threading.Event()
        release = threading.Event()
        order: list[str] = []

        def first() -> None:
            with self.lock():
                holding.set()
                release.wait(TIMEOUT)
                order.append("first released")

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(holding.wait(TIMEOUT))
        threading.Timer(0.2, release.set).start()
        started = time.monotonic()
        with self.lock():
            order.append("second acquired")
        thread.join(TIMEOUT)
        self.assertEqual(order, ["first released", "second acquired"])
        self.assertGreaterEqual(time.monotonic() - started, 0.15)

    def test_it_is_reentrant_within_a_thread(self) -> None:
        lock = self.lock()
        with lock, lock:
            pass

    def test_a_holder_that_died_does_not_block_forever(self) -> None:
        """A crashed process leaves the mutex abandoned, not locked."""

        def die_holding() -> None:
            lock = BusLock(self.name)
            lock.__enter__()  # and never leave

        thread = threading.Thread(target=die_holding)
        thread.start()
        thread.join(TIMEOUT)
        with self.assertLogs("monitorctl.ddc.buslock", level="INFO") as logs, self.lock():
            pass
        self.assertIn("without releasing", logs.output[0])


if __name__ == "__main__":
    unittest.main()
