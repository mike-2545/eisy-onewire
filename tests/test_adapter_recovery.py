from __future__ import annotations

import threading
import time
import unittest

from eisy_onewire.adapter_recovery import (
    AdapterRecoveryConfigurationError,
    AdapterRecoveryPolicy,
    AdapterRecoveryWorker,
)
from eisy_onewire.usb_detector import UsbDeviceDescriptor


def descriptor(address: int = 3) -> UsbDeviceDescriptor:
    return UsbDeviceDescriptor(
        0x04FA, 0x2490, 0x110, 0x200, 0, 0, 0, 8, 1, 0, address, (4,)
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


class AdapterRecoveryPolicyTests(unittest.TestCase):
    def test_missing_configuration_is_disabled_and_round_trips(self) -> None:
        policy = AdapterRecoveryPolicy.from_data(None)
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.to_data(), {"schema_version": 1, "enabled": False})

    def test_persistent_authorization_is_strictly_validated(self) -> None:
        self.assertTrue(
            AdapterRecoveryPolicy.from_data(
                {"schema_version": 1, "enabled": True}
            ).enabled
        )
        for value in (
            True,
            {},
            {"schema_version": 2, "enabled": True},
            {"schema_version": 1, "enabled": 1},
            {"schema_version": 1, "enabled": True, "extra": False},
        ):
            with self.subTest(value=value):
                with self.assertRaises(AdapterRecoveryConfigurationError):
                    AdapterRecoveryPolicy.from_data(value)


class AdapterRecoveryWorkerTests(unittest.TestCase):
    def make_worker(
        self,
        *,
        authorized=lambda: True,
        identity=lambda: True,
        verified=lambda: False,
        enumerate_candidates=lambda: (descriptor(),),
        verify=lambda reason: True,
        publish=None,
        window=0.03,
        poll=0.005,
        settlement=0.0,
    ):
        reports = [] if publish is None else publish
        worker = AdapterRecoveryWorker(
            Logger(),
            authorized=authorized,
            has_accepted_identity=identity,
            already_verified=verified,
            enumerate_candidates=enumerate_candidates,
            verify=verify,
            publish=reports.append,
            readiness_window_seconds=window,
            readiness_poll_seconds=poll,
            settlement_seconds=settlement,
            shutdown_timeout_seconds=1.0,
        )
        worker.start()
        return worker, reports

    def test_disabled_policy_performs_zero_enumeration_or_identity_work(self) -> None:
        calls = []
        worker, reports = self.make_worker(
            authorized=lambda: False,
            enumerate_candidates=lambda: calls.append("enumerate") or (),
            verify=lambda reason: calls.append("verify") or True,
        )
        try:
            self.assertTrue(worker.trigger("plugin_startup"))
            self.assertTrue(worker.wait_idle())
        finally:
            worker.stop()
        self.assertEqual(calls, [])
        self.assertEqual(reports[-1]["status"], "disabled")

    def test_success_performs_one_identity_transaction(self) -> None:
        calls = []
        worker, reports = self.make_worker(
            verify=lambda reason: calls.append(reason) or True
        )
        try:
            self.assertTrue(worker.trigger("plugin_startup"))
            self.assertTrue(worker.wait_idle())
        finally:
            worker.stop()
        self.assertEqual(calls, ["plugin_startup"])
        self.assertEqual(reports[-1]["status"], "verified")
        self.assertEqual(reports[-1]["identity_transactions"], 1)
        self.assertEqual(reports[-1]["automatic_retries"], 0)

    def test_absent_adapter_waits_bounded_without_identity_transaction(self) -> None:
        calls = []
        worker, reports = self.make_worker(
            enumerate_candidates=lambda: (),
            verify=lambda reason: calls.append(reason) or True,
        )
        try:
            worker.trigger("plugin_startup")
            self.assertTrue(worker.wait_idle())
        finally:
            worker.stop()
        self.assertEqual(calls, [])
        self.assertEqual(reports[-1]["status"], "adapter_absent")
        self.assertEqual(reports[-1]["identity_transactions"], 0)

    def test_delayed_descriptor_readiness_still_attempts_identity_only_once(self) -> None:
        enumerations = iter(((), (), (descriptor(),), (descriptor(),)))
        calls = []
        worker, reports = self.make_worker(
            enumerate_candidates=lambda: next(enumerations),
            verify=lambda reason: calls.append(reason) or True,
        )
        try:
            worker.trigger("plugin_startup")
            self.assertTrue(worker.wait_idle())
        finally:
            worker.stop()
        self.assertEqual(calls, ["plugin_startup"])
        self.assertEqual(reports[-1]["status"], "verified")

    def test_multiple_adapters_fail_before_identity_transaction(self) -> None:
        calls = []
        worker, reports = self.make_worker(
            enumerate_candidates=lambda: (descriptor(3), descriptor(5)),
            verify=lambda reason: calls.append(reason) or True,
        )
        try:
            worker.trigger("plugin_startup")
            self.assertTrue(worker.wait_idle())
        finally:
            worker.stop()
        self.assertEqual(calls, [])
        self.assertEqual(reports[-1]["status"], "multiple_adapters")
        self.assertEqual(reports[-1]["matching_adapter_count"], 2)

    def test_failed_identity_has_zero_retry_and_distinct_event_can_try_again(self) -> None:
        calls = []
        worker, reports = self.make_worker(
            verify=lambda reason: calls.append(reason) or False
        )
        try:
            worker.trigger("plugin_startup")
            self.assertTrue(worker.wait_idle())
            worker.trigger("retained_handle_invalidated")
            self.assertTrue(worker.wait_idle())
        finally:
            worker.stop()
        self.assertEqual(calls, ["plugin_startup", "retained_handle_invalidated"])
        self.assertEqual(
            [item["status"] for item in reports if item["identity_transactions"] == 1],
            ["verification_failed", "verification_failed"],
        )

    def test_duplicate_event_is_coalesced_while_work_is_running(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def verify(reason):
            calls.append(reason)
            entered.set()
            release.wait(0.5)
            return True

        worker, unused = self.make_worker(verify=verify)
        try:
            self.assertTrue(worker.trigger("plugin_startup"))
            self.assertTrue(entered.wait(0.5))
            self.assertFalse(worker.trigger("plugin_startup"))
            release.set()
            self.assertTrue(worker.wait_idle())
        finally:
            worker.stop()
        self.assertEqual(calls, ["plugin_startup"])

    def test_operator_disable_during_settlement_prevents_identity_transaction(self) -> None:
        authorized = True
        calls = []
        worker, reports = self.make_worker(
            authorized=lambda: authorized,
            verify=lambda reason: calls.append(reason) or True,
            settlement=0.04,
        )
        try:
            worker.trigger("retained_handle_invalidated")
            time.sleep(0.01)
            authorized = False
            self.assertTrue(worker.wait_idle())
        finally:
            worker.stop()
        self.assertEqual(calls, [])
        self.assertEqual(reports[-1]["status"], "disabled")

    def test_shutdown_cancels_pending_readiness_and_suppresses_late_publication(self) -> None:
        reports = []
        worker, unused = self.make_worker(
            enumerate_candidates=lambda: (),
            publish=reports,
            window=1.0,
            poll=0.2,
        )
        worker.trigger("plugin_startup")
        time.sleep(0.01)
        worker.stop()
        count = len(reports)
        time.sleep(0.02)
        self.assertEqual(len(reports), count)

    def test_shutdown_waits_for_inflight_identity_work_and_suppresses_result(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        stopped = threading.Event()
        reports = []

        def verify(reason):
            entered.set()
            release.wait(0.5)
            return True

        worker, unused = self.make_worker(verify=verify, publish=reports)
        worker.trigger("plugin_startup")
        self.assertTrue(entered.wait(0.5))

        stopper = threading.Thread(
            target=lambda: (worker.stop(), stopped.set()), daemon=True
        )
        stopper.start()
        time.sleep(0.02)
        self.assertFalse(stopped.is_set())
        release.set()
        stopper.join(0.5)

        self.assertTrue(stopped.is_set())
        self.assertFalse(any(item.get("status") == "verified" for item in reports))


if __name__ == "__main__":
    unittest.main()
