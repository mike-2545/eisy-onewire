from __future__ import annotations

import threading
import unittest
from datetime import datetime, timezone

from eisy_onewire.ds2409_status import ManualDs2409StatusService
from eisy_onewire.ds2409_transport import (
    Ds2409Branch,
    Ds2409ControlMode,
    Ds2409State,
    Ds2409StatusInfo,
)
from eisy_onewire.manual_discovery import AdapterBusyError


ADAPTER = bytes.fromhex("814455667788999C")
COUPLER = bytes.fromhex("1F5566778899AA38")


def flagged_status() -> Ds2409StatusInfo:
    return Ds2409StatusInfo(
        Ds2409State.OFF,
        True,
        True,
        0x7F,
        True,
        True,
        Ds2409ControlMode.AUTO,
        Ds2409Branch.AUX,
        None,
    )


class Ds2409StatusTests(unittest.TestCase):
    def test_one_shot_status_is_serialized_and_reports_nonmutating_semantics(self) -> None:
        lock = threading.Lock()
        calls = []
        service = ManualDs2409StatusService(
            lock,
            read=lambda rom: calls.append(rom) or flagged_status(),
            clock=lambda: datetime(2026, 11, 22, 20, 0, tzinfo=timezone.utc),
        )

        report = service.run_once(ADAPTER, COUPLER)

        self.assertEqual(calls, [COUPLER])
        self.assertFalse(lock.locked())
        notice = report.to_notice()
        self.assertEqual(notice["raw_status"], "0x7F")
        self.assertEqual(notice["branch_state"], "off")
        self.assertTrue(notice["main"]["event_flag"])
        self.assertTrue(notice["aux"]["event_flag"])
        self.assertEqual(notice["control"]["mode"], "auto")
        self.assertEqual(notice["control"]["association"], "aux")
        self.assertTrue(notice["event_flags_preserved"])
        self.assertEqual(notice["mutating_ds2409_commands"], 0)
        self.assertEqual(notice["automatic_retries"], 0)

    def test_busy_and_error_paths_do_not_retry_and_release_lock(self) -> None:
        lock = threading.Lock()
        calls = []
        service = ManualDs2409StatusService(
            lock,
            read=lambda rom: calls.append(rom) or flagged_status(),
        )
        lock.acquire()
        try:
            with self.assertRaises(AdapterBusyError):
                service.run_once(ADAPTER, COUPLER)
        finally:
            lock.release()
        self.assertEqual(calls, [])

        def fail(rom):
            calls.append(rom)
            raise TimeoutError("bounded timeout")

        failed = ManualDs2409StatusService(lock, read=fail)
        with self.assertRaisesRegex(TimeoutError, "bounded timeout"):
            failed.run_once(ADAPTER, COUPLER)
        self.assertEqual(calls, [COUPLER])
        self.assertFalse(lock.locked())


if __name__ == "__main__":
    unittest.main()
