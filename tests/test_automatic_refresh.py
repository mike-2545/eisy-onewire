from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone

from eisy_onewire.automatic_refresh import (
    AutomaticRefreshScheduler,
    RefreshTarget,
    SchedulerShutdownTimeout,
)
from eisy_onewire.ds2409_transport import BranchHop, Ds2409Branch


ROM_ONE = bytes.fromhex("2811223344556656")
ROM_TWO = bytes.fromhex("2822334455667727")
COUPLER = bytes.fromhex("1F5566778899AA38")


class Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.wall = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.value

    def utcnow(self) -> datetime:
        return self.wall

    def advance(self, seconds: float, *, wall_seconds: float | None = None) -> None:
        self.value += seconds
        self.wall += timedelta(
            seconds=seconds if wall_seconds is None else wall_seconds
        )


class Logger:
    def __init__(self) -> None:
        self.entries = []

    def info(self, message, *args) -> None:
        self.entries.append(("info", message % args))

    def warning(self, message, *args) -> None:
        self.entries.append(("warning", message % args))

    def error(self, message, *args) -> None:
        self.entries.append(("error", message % args))


class TrackingNotices(dict):
    def __init__(self, interface_stopped: threading.Event) -> None:
        super().__init__()
        self.interface_stopped = interface_stopped
        self.updates_after_stop = 0

    def __setitem__(self, key, value) -> None:
        if self.interface_stopped.is_set():
            self.updates_after_stop += 1
        super().__setitem__(key, value)


def target(
    rom: bytes,
    address: str,
    interval: int,
    calls: list[str],
    *,
    branch_path=(),
    result=True,
    error=None,
    automatic_eligible=True,
):
    return RefreshTarget(
        address,
        rom,
        address,
        interval,
        branch_path,
        lambda: calls.append(address) or result,
        lambda: error,
        automatic_eligible,
    )


class AutomaticRefreshSchedulerTests(unittest.TestCase):
    def make_scheduler(self):
        clock = Clock()
        notices = {}
        logger = Logger()
        scheduler = AutomaticRefreshScheduler(
            logger,
            notices,
            monotonic=clock.monotonic,
            utcnow=clock.utcnow,
        )
        return scheduler, clock, notices, logger

    def test_disarmed_master_never_runs_and_reports_upgrade_safe_state(self) -> None:
        scheduler, clock, _, _ = self.make_scheduler()
        calls = []
        scheduler.configure(
            master_enabled=True,
            armed=False,
            adapter_accessible=True,
            configuration_valid=True,
            targets=(target(ROM_ONE, "one", 30, calls),),
        )

        clock.advance(3600)
        self.assertFalse(scheduler.run_due_once())
        self.assertEqual(calls, [])
        self.assertEqual(scheduler.snapshot()["status"], "disarmed")
        self.assertIsNone(scheduler.snapshot()["sensors"][0]["next_due_in_seconds"])

    def test_root_targets_are_staggered_and_rescheduled_after_completion(self) -> None:
        scheduler, clock, _, _ = self.make_scheduler()
        calls = []
        scheduler.configure(
            master_enabled=True,
            armed=True,
            adapter_accessible=True,
            configuration_valid=True,
            targets=(
                target(ROM_ONE, "one", 30, calls),
                target(ROM_TWO, "two", 60, calls),
            ),
        )

        self.assertFalse(scheduler.run_due_once())
        clock.advance(2)
        self.assertTrue(scheduler.run_due_once())
        self.assertEqual(calls, ["one"])
        first = scheduler.snapshot()["sensors"][0]
        self.assertEqual(first["next_due_in_seconds"], 30.0)
        self.assertIsNotNone(first["last_attempt_at"])
        self.assertIsNotNone(first["last_success_at"])

        clock.advance(2)
        self.assertTrue(scheduler.run_due_once())
        self.assertEqual(calls, ["one", "two"])
        clock.advance(28)
        self.assertTrue(scheduler.run_due_once())
        self.assertEqual(calls, ["one", "two", "one"])

    def test_failure_has_no_immediate_retry_and_preserves_error(self) -> None:
        scheduler, clock, _, _ = self.make_scheduler()
        calls = []
        scheduler.configure(
            master_enabled=True,
            armed=True,
            adapter_accessible=True,
            configuration_valid=True,
            targets=(
                target(
                    ROM_ONE,
                    "one",
                    30,
                    calls,
                    result=False,
                    error="adapter is busy",
                ),
            ),
        )

        clock.advance(2)
        self.assertTrue(scheduler.run_due_once())
        self.assertFalse(scheduler.run_due_once())
        state = scheduler.snapshot()["sensors"][0]
        self.assertEqual(state["last_error"], "adapter is busy")
        self.assertEqual(state["next_due_in_seconds"], 30.0)
        self.assertIsNone(state["last_success_at"])

    def test_disconnect_after_success_retains_last_success_as_stale_history(self) -> None:
        scheduler, clock, _, _ = self.make_scheduler()
        calls = []
        available = [True]

        def refresh():
            calls.append("one")
            return available[0]

        scheduler.configure(
            master_enabled=True,
            armed=True,
            adapter_accessible=True,
            configuration_valid=True,
            targets=(
                RefreshTarget(
                    "one",
                    ROM_ONE,
                    "one",
                    30,
                    (),
                    refresh,
                    lambda: "USB device disconnected",
                ),
            ),
        )
        clock.advance(2)
        self.assertTrue(scheduler.run_due_once())
        successful_at = scheduler.snapshot()["sensors"][0]["last_success_at"]

        available[0] = False
        clock.advance(30)
        self.assertTrue(scheduler.run_due_once())
        state = scheduler.snapshot()["sensors"][0]
        self.assertEqual(state["last_success_at"], successful_at)
        self.assertEqual(state["last_error"], "USB device disconnected")
        self.assertEqual(calls, ["one", "one"])

    def test_branch_sensor_is_visible_but_never_automatically_called(self) -> None:
        scheduler, clock, _, _ = self.make_scheduler()
        calls = []
        branch = (BranchHop(COUPLER, Ds2409Branch.MAIN),)
        scheduler.configure(
            master_enabled=True,
            armed=True,
            adapter_accessible=True,
            configuration_valid=True,
            targets=(
                target(
                    ROM_ONE,
                    "branch",
                    30,
                    calls,
                    branch_path=branch,
                    automatic_eligible=False,
                ),
            ),
        )

        clock.advance(300)
        self.assertFalse(scheduler.run_due_once())
        sensor = scheduler.snapshot()["sensors"][0]
        self.assertEqual(sensor["eligibility"], "branch_policy_disabled")
        self.assertIn("manual refresh only", sensor["last_error"])
        self.assertEqual(calls, [])

    def test_authorized_branch_target_is_staggered_and_scheduled_without_retry(self) -> None:
        scheduler, clock, _, _ = self.make_scheduler()
        calls = []
        branch = (BranchHop(COUPLER, Ds2409Branch.MAIN),)
        scheduler.configure(
            master_enabled=True,
            armed=True,
            adapter_accessible=True,
            configuration_valid=True,
            targets=(target(ROM_ONE, "branch", 30, calls, branch_path=branch),),
        )

        clock.advance(2)
        self.assertTrue(scheduler.run_due_once())
        sensor = scheduler.snapshot()["sensors"][0]
        self.assertEqual(sensor["eligibility"], "scheduled_branch")
        self.assertEqual(sensor["next_due_in_seconds"], 30.0)
        self.assertTrue(scheduler.snapshot()["automatic_ds2409_switching"])
        self.assertEqual(calls, ["branch"])

    def test_root_and_authorized_branch_share_one_staggered_operation_sequence(self) -> None:
        scheduler, clock, _, _ = self.make_scheduler()
        calls = []
        branch = (BranchHop(COUPLER, Ds2409Branch.MAIN),)
        scheduler.configure(
            master_enabled=True,
            armed=True,
            adapter_accessible=True,
            configuration_valid=True,
            targets=(
                target(ROM_ONE, "root", 30, calls),
                target(ROM_TWO, "branch", 60, calls, branch_path=branch),
            ),
        )

        clock.advance(2)
        self.assertTrue(scheduler.run_due_once())
        clock.advance(2)
        self.assertTrue(scheduler.run_due_once())
        self.assertEqual(calls, ["branch", "root"])
        self.assertEqual(
            {item["eligibility"] for item in scheduler.snapshot()["sensors"]},
            {"scheduled_branch", "scheduled_root"},
        )

    def test_disable_interval_change_and_duplicate_configure_cancel_due_work(self) -> None:
        scheduler, clock, _, _ = self.make_scheduler()
        calls = []
        configured = dict(
            master_enabled=True,
            armed=True,
            adapter_accessible=True,
            configuration_valid=True,
        )
        scheduler.configure(
            **configured, targets=(target(ROM_ONE, "one", 60, calls),)
        )
        clock.advance(1)
        scheduler.configure(
            **configured, targets=(target(ROM_ONE, "one", 60, calls),)
        )
        self.assertEqual(
            scheduler.snapshot()["sensors"][0]["next_due_in_seconds"], 1.0
        )

        scheduler.configure(
            master_enabled=False,
            armed=False,
            adapter_accessible=True,
            configuration_valid=True,
            targets=(target(ROM_ONE, "one", 60, calls),),
        )
        clock.advance(100)
        self.assertFalse(scheduler.run_due_once())
        scheduler.configure(
            **configured, targets=(target(ROM_ONE, "one", 0, calls),)
        )
        self.assertFalse(scheduler.run_due_once())
        self.assertEqual(calls, [])

    def test_wall_clock_shift_does_not_change_monotonic_due_time(self) -> None:
        scheduler, clock, _, _ = self.make_scheduler()
        calls = []
        scheduler.configure(
            master_enabled=True,
            armed=True,
            adapter_accessible=True,
            configuration_valid=True,
            targets=(target(ROM_ONE, "one", 30, calls),),
        )

        clock.advance(1, wall_seconds=86400)
        self.assertFalse(scheduler.run_due_once())
        clock.advance(1, wall_seconds=-172800)
        self.assertTrue(scheduler.run_due_once())
        self.assertEqual(calls, ["one"])

    def test_concurrent_due_runner_cannot_start_a_second_operation(self) -> None:
        scheduler, clock, _, _ = self.make_scheduler()
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def blocking_refresh():
            calls.append("one")
            entered.set()
            release.wait(timeout=2)
            return True

        scheduler.configure(
            master_enabled=True,
            armed=True,
            adapter_accessible=True,
            configuration_valid=True,
            targets=(
                RefreshTarget(
                    "one", ROM_ONE, "one", 30, (), blocking_refresh, lambda: None
                ),
                target(ROM_TWO, "two", 30, calls),
            ),
        )
        clock.advance(4)
        worker = threading.Thread(target=scheduler.run_due_once)
        worker.start()
        self.assertTrue(entered.wait(timeout=1))

        self.assertFalse(scheduler.run_due_once())
        self.assertEqual(calls, ["one"])

        release.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def test_missing_adapter_invalid_config_and_clean_stop_fail_closed(self) -> None:
        scheduler, clock, _, _ = self.make_scheduler()
        calls = []
        scheduler.configure(
            master_enabled=True,
            armed=True,
            adapter_accessible=False,
            configuration_valid=True,
            targets=(target(ROM_ONE, "one", 30, calls),),
        )
        clock.advance(100)
        self.assertFalse(scheduler.run_due_once())
        self.assertEqual(scheduler.snapshot()["status"], "adapter_unavailable")

        scheduler.configure(
            master_enabled=True,
            armed=True,
            adapter_accessible=True,
            configuration_valid=False,
            targets=(),
        )
        self.assertEqual(scheduler.snapshot()["status"], "invalid_configuration")
        scheduler.start()
        scheduler.stop()
        self.assertEqual(calls, [])

    def test_shutdown_waits_for_inflight_refresh_before_interface_stop(self) -> None:
        clock = Clock()
        stopped = threading.Event()
        notices = TrackingNotices(stopped)
        scheduler = AutomaticRefreshScheduler(
            Logger(),
            notices,
            monotonic=clock.monotonic,
            utcnow=clock.utcnow,
            shutdown_timeout_seconds=1.0,
        )
        entered = threading.Event()
        release = threading.Event()
        driver_updates_after_stop = []

        def slow_refresh():
            entered.set()
            release.wait(timeout=2)
            driver_updates_after_stop.append(stopped.is_set())
            return True

        configured = dict(
            master_enabled=True,
            armed=True,
            adapter_accessible=True,
            configuration_valid=True,
            targets=(
                RefreshTarget(
                    "one", ROM_ONE, "one", 30, (), slow_refresh, lambda: None
                ),
            ),
        )
        scheduler.start()
        scheduler.configure(**configured)
        clock.advance(2)
        scheduler.configure(**configured)
        self.assertTrue(entered.wait(timeout=1))

        def shutdown_then_stop_interface():
            scheduler.stop()
            stopped.set()

        shutdown = threading.Thread(target=shutdown_then_stop_interface)
        shutdown.start()
        self.assertFalse(stopped.wait(timeout=0.05))
        release.set()
        shutdown.join(timeout=2)

        self.assertFalse(shutdown.is_alive())
        self.assertTrue(stopped.is_set())
        self.assertEqual(driver_updates_after_stop, [False])
        self.assertEqual(notices.updates_after_stop, 0)

    def test_shutdown_timeout_fails_closed_and_cancels_future_work(self) -> None:
        scheduler, clock, _, _ = self.make_scheduler()
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def blocked_refresh():
            calls.append("started")
            entered.set()
            release.wait(timeout=2)
            return True

        scheduler = AutomaticRefreshScheduler(
            Logger(),
            {},
            monotonic=clock.monotonic,
            utcnow=clock.utcnow,
            shutdown_timeout_seconds=0.02,
        )
        configured = dict(
            master_enabled=True,
            armed=True,
            adapter_accessible=True,
            configuration_valid=True,
            targets=(
                RefreshTarget(
                    "one", ROM_ONE, "one", 30, (), blocked_refresh, lambda: None
                ),
            ),
        )
        scheduler.start()
        scheduler.configure(**configured)
        clock.advance(2)
        scheduler.configure(**configured)
        self.assertTrue(entered.wait(timeout=1))

        with self.assertRaises(SchedulerShutdownTimeout):
            scheduler.stop()

        release.set()
        scheduler.stop()
        clock.advance(300)
        self.assertFalse(scheduler.run_due_once())
        self.assertEqual(calls, ["started"])


if __name__ == "__main__":
    unittest.main()
