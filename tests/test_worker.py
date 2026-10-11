"""Tests for the bus thread: jobs, event coalescing, periodic checks, survival.

These run a real thread with short real delays; every wait is bounded, so a
regression shows up as a failure rather than a hung test run.
"""

from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass

from monitorctl.app.keeper import Event, KeeperResult
from monitorctl.app.worker import Worker

TIMEOUT = 5.0


@dataclass(frozen=True)
class Settings:
    interval_seconds: float = 3600.0


class FakeKeeper:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.ticks = 0
        self.ticked = threading.Event()
        self.handled = threading.Event()
        self.caught_up = threading.Event()
        self.fail = False

    def catch_up(self):
        self.caught_up.set()
        return None

    def tick(self):
        self.ticks += 1
        self.ticked.set()
        if self.fail:
            raise RuntimeError("tick blew up")
        return KeeperResult("periodic", None)

    def handle_event(self, event: Event):
        self.events.append(event)
        self.handled.set()
        return KeeperResult(event.value, None, rescanned=True)


class WorkerTestCase(unittest.TestCase):
    def make(self, *, interval: float = 3600.0, settle: float = 0.05):
        self.keeper = FakeKeeper()
        self.notified = threading.Event()
        self.keeper_results: list[KeeperResult] = []
        worker = Worker(
            service=object(),
            keeper=self.keeper,
            settings=lambda: Settings(interval),
            notify=self.notified.set,
            on_keeper=self.keeper_results.append,
            settle_seconds=settle,
        )
        worker.start()
        self.addCleanup(worker.stop)
        return worker

    def wait_and_drain(self, worker: Worker) -> None:
        self.assertTrue(self.notified.wait(TIMEOUT), "the worker never notified")
        self.notified.clear()
        worker.drain()


class TestJobs(WorkerTestCase):
    def test_a_result_comes_back_through_drain(self) -> None:
        worker = self.make()
        results = []
        worker.submit(lambda service: 42, lambda r, e: results.append((r, e)))
        self.wait_and_drain(worker)
        self.assertEqual(results, [(42, None)])

    def test_jobs_run_in_order_on_one_other_thread(self) -> None:
        worker = self.make()
        seen: list[tuple[int, str]] = []
        done = threading.Event()
        for n in range(5):
            worker.submit(
                lambda s, n=n: seen.append((n, threading.current_thread().name))
            )
        worker.submit(lambda s: done.set())
        self.assertTrue(done.wait(TIMEOUT))
        self.assertEqual([n for n, _ in seen], list(range(5)))
        self.assertEqual({name for _, name in seen}, {"monitorctl-bus"})

    def test_an_exception_reaches_the_callback_and_the_thread_survives(self) -> None:
        worker = self.make()
        results = []

        def boom(service):
            raise ValueError("bad")

        worker.submit(boom, lambda r, e: results.append(e))
        self.wait_and_drain(worker)
        self.assertIsInstance(results[0], ValueError)

        worker.submit(lambda s: "still alive", lambda r, e: results.append(r))
        self.wait_and_drain(worker)
        self.assertEqual(results[1], "still alive")

    def test_a_failing_callback_does_not_stop_the_others(self) -> None:
        worker = self.make()
        results = []
        worker.submit(lambda s: 1, lambda r, e: 1 / 0)
        worker.submit(lambda s: 2, lambda r, e: results.append(r))
        done = threading.Event()
        worker.submit(lambda s: done.set())
        self.assertTrue(done.wait(TIMEOUT))
        with self.assertLogs("monitorctl.app.worker", level="ERROR"):
            worker.drain()
        self.assertEqual(results, [2])


class TestEvents(WorkerTestCase):
    def test_a_burst_is_handled_once_with_the_most_specific_reason(self) -> None:
        worker = self.make(settle=0.2)
        worker.post_event(Event.DISPLAY_CHANGED)
        worker.post_event(Event.MONITOR_ARRIVED)
        worker.post_event(Event.DISPLAY_CHANGED)
        self.assertTrue(self.keeper.handled.wait(TIMEOUT))
        self.wait_and_drain(worker)
        self.assertEqual(self.keeper.events, [Event.MONITOR_ARRIVED])
        self.assertEqual(len(self.keeper_results), 1)
        self.assertTrue(self.keeper_results[0].rescanned)

    def test_a_later_event_postpones_the_burst(self) -> None:
        worker = self.make(settle=0.3)
        worker.post_event(Event.DISPLAY_CHANGED)
        self.assertFalse(self.keeper.handled.wait(0.15))
        worker.post_event(Event.DISPLAY_CHANGED)
        self.assertFalse(self.keeper.handled.wait(0.2))  # 0.35 s after the first
        self.assertTrue(self.keeper.handled.wait(TIMEOUT))


class TestChecks(WorkerTestCase):
    def test_checks_follow_the_interval(self) -> None:
        self.make(interval=0.05)
        self.assertTrue(self.keeper.ticked.wait(TIMEOUT))

    def test_no_check_before_the_interval(self) -> None:
        self.make(interval=3600.0)
        self.assertFalse(self.keeper.ticked.wait(0.3))

    def test_a_failing_check_is_logged_and_the_next_one_still_runs(self) -> None:
        with self.assertLogs("monitorctl.app.worker", level="ERROR"):
            self.make(interval=0.05)
            self.keeper.fail = True
            self.assertTrue(self.keeper.ticked.wait(TIMEOUT))
            self.keeper.ticked.clear()
            self.assertTrue(self.keeper.ticked.wait(TIMEOUT))

    def test_a_nudge_lets_the_keeper_catch_up_now_not_at_the_next_check(self) -> None:
        worker = self.make(interval=3600.0)
        self.assertFalse(self.keeper.caught_up.wait(0.1))
        worker.nudge()
        self.assertTrue(self.keeper.caught_up.wait(TIMEOUT))
        self.assertEqual(self.keeper.ticks, 0)

    def test_changed_settings_nudge_too(self) -> None:
        """Unpausing is a settings change."""
        worker = self.make(interval=3600.0)
        worker.reschedule()
        self.assertTrue(self.keeper.caught_up.wait(TIMEOUT))


class TestStop(WorkerTestCase):
    def test_stop_finishes_the_job_in_progress(self) -> None:
        worker = self.make()
        started = threading.Event()
        release = threading.Event()
        finished = []

        def slow(service):
            started.set()
            release.wait(TIMEOUT)
            finished.append(True)

        worker.submit(slow)
        self.assertTrue(started.wait(TIMEOUT))
        threading.Timer(0.1, release.set).start()
        worker.stop()
        self.assertEqual(finished, [True])

    def test_stop_drops_the_jobs_still_queued(self) -> None:
        worker = self.make()
        started = threading.Event()
        release = threading.Event()
        later = []

        def slow(service):
            started.set()
            release.wait(TIMEOUT)

        worker.submit(slow)
        self.assertTrue(started.wait(TIMEOUT))
        for n in range(3):
            worker.submit(lambda s, n=n: later.append(n))
        threading.Timer(0.1, release.set).start()
        worker.stop()
        self.assertEqual(later, [])


if __name__ == "__main__":
    unittest.main()
