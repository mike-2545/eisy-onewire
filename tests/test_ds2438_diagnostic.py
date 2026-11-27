from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from eisy_onewire.diagnostic_access import VerifiedDiagnosticAccess
from eisy_onewire.branch_temperature import BranchEventPolicyError, BranchRestorationError
from eisy_onewire.discovery_inventory import (
    TOPOLOGY_SCOPE,
    DiscoveryInventory,
    KnownDevice,
    LastScan,
)
from eisy_onewire.ds2409_transport import (
    BranchHop,
    Ds2409Branch,
    Ds2409State,
    Ds2409StatusInfo,
)
from eisy_onewire.ds2438 import Ds2438Measurement
from eisy_onewire.ds2438_diagnostic import (
    Ds2438BranchDiagnostic,
    Ds2438BranchTarget,
    Ds2438DiagnosticResult,
    Ds2438RequestedTarget,
    main,
    read_branch_ds2438,
)
from eisy_onewire.onewire_discovery import dallas_crc8


SENSOR = bytes.fromhex("2666778899AABB54")
ADAPTER = bytes.fromhex("814455667788999C")
OTHER_ADAPTER_PREFIX = bytes.fromhex("81112233445566")
OTHER_ADAPTER = OTHER_ADAPTER_PREFIX + bytes([dallas_crc8(OTHER_ADAPTER_PREFIX)])
COUPLER = bytes.fromhex("1F5566778899AA38")
TARGET = Ds2438BranchTarget(
    SENSOR,
    ADAPTER,
    (BranchHop(COUPLER, Ds2409Branch.MAIN),),
    "2026-11-22T12:00:00Z",
)
REQUEST = Ds2438RequestedTarget(SENSOR, ADAPTER, COUPLER, Ds2409Branch.MAIN)
MEASUREMENT = Ds2438Measurement(SENSOR, 23.875, 74.975, 1.98, 5.21)


def status(
    state: Ds2409State,
    *,
    raw: int = 0x0F,
    main_event: bool = False,
    aux_event: bool = False,
) -> Ds2409StatusInfo:
    return Ds2409StatusInfo(state, main_event, aux_event, raw)


class FakeCoupler:
    def __init__(self, statuses: list[Ds2409StatusInfo]) -> None:
        self.statuses = list(statuses)
        self.calls: list[tuple[object, ...]] = []
        self.fail_restore = False

    def read_status(self, rom: bytes) -> Ds2409StatusInfo:
        self.calls.append(("status", rom))
        return self.statuses.pop(0)

    def select_branch(self, rom: bytes, branch: Ds2409Branch) -> int:
        self.calls.append(("select", rom, branch))
        return 0

    def restore_state(self, rom: bytes, state: Ds2409State) -> None:
        self.calls.append(("restore", rom, state))
        if self.fail_restore:
            raise RuntimeError("restore failed")


class FakeReader:
    def __init__(self, result: object = MEASUREMENT) -> None:
        self.result = result
        self.calls: list[bytes] = []

    def read_measurements(self, rom: bytes) -> Ds2438Measurement:
        self.calls.append(rom)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result  # type: ignore[return-value]


class BusyLock:
    def acquire(self, blocking: bool = True) -> bool:
        del blocking
        return False

    def release(self) -> None:
        raise AssertionError("a lock that was not acquired must not be released")


def inventory(adapter: bytes = ADAPTER) -> DiscoveryInventory:
    return DiscoveryInventory(
        (KnownDevice(adapter, "2026-11-22T12:00:00Z", "2026-11-22T12:00:00Z"),)
    )


def topology_inventory(*, sensor_adapter: bytes = ADAPTER) -> DiscoveryInventory:
    timestamp = "2026-11-22T12:00:00Z"
    hop = TARGET.path[0]
    devices = (
        KnownDevice(ADAPTER, timestamp, timestamp),
        KnownDevice(COUPLER, timestamp, timestamp),
        KnownDevice(SENSOR, timestamp, timestamp, sensor_adapter, (hop,)),
    )
    scan = LastScan(
        timestamp,
        TOPOLOGY_SCOPE,
        tuple(item.rom.hex().upper() for item in devices),
        True,
        (),
        ADAPTER.hex().upper(),
        (hop.label,),
    )
    return DiscoveryInventory(devices, scan)


class RecordingBinding:
    def __init__(self, *, verify_error: Exception | None = None) -> None:
        self.verify_error = verify_error
        self.verify_calls: list[bytes] = []
        self.closed = 0

    def verify(self, expected: bytes) -> None:
        self.verify_calls.append(expected)
        if self.verify_error is not None:
            raise self.verify_error

    def close(self) -> None:
        self.closed += 1

    def open(self):
        raise AssertionError("native session must not open in this test")


class RecordingLock:
    def __init__(self) -> None:
        self.released = False
        self.acquires = 0

    def acquire(self, blocking: bool = True) -> bool:
        del blocking
        self.acquires += 1
        return True

    def release(self) -> None:
        self.released = True


class Ds2438DiagnosticTests(unittest.TestCase):
    def test_selects_reads_once_restores_and_verifies_original_branch(self) -> None:
        coupler = FakeCoupler(
            [
                status(Ds2409State.AUX, main_event=True),
                status(Ds2409State.MAIN, main_event=True),
                status(Ds2409State.MAIN, main_event=True),
                status(Ds2409State.AUX, main_event=True),
            ]
        )
        reader = FakeReader()

        result = read_branch_ds2438(TARGET, coupler, reader)

        self.assertIs(result.measurement, MEASUREMENT)
        self.assertEqual(reader.calls, [SENSOR])
        self.assertEqual(result.original_state, Ds2409State.AUX)
        self.assertEqual(result.restored_state, Ds2409State.AUX)
        self.assertFalse(result.event_flags_cleared)
        self.assertIn(("restore", COUPLER, Ds2409State.AUX), coupler.calls)

    def test_original_off_requires_distinct_explicit_acknowledgement(self) -> None:
        coupler = FakeCoupler(
            [status(Ds2409State.OFF, main_event=True, raw=0x3F)]
        )
        reader = FakeReader()

        with self.assertRaises(BranchEventPolicyError):
            read_branch_ds2438(TARGET, coupler, reader)

        self.assertEqual(reader.calls, [])
        self.assertEqual(len(coupler.calls), 1)

    def test_sensor_failure_restores_and_restoration_failure_overrides_result(self) -> None:
        coupler = FakeCoupler(
            [
                status(Ds2409State.AUX),
                status(Ds2409State.MAIN),
                status(Ds2409State.MAIN),
                status(Ds2409State.AUX),
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "CRC failed"):
            read_branch_ds2438(
                TARGET, coupler, FakeReader(RuntimeError("CRC failed"))
            )
        self.assertIn(("restore", COUPLER, Ds2409State.AUX), coupler.calls)

        failed_restore = FakeCoupler(
            [
                status(Ds2409State.AUX),
                status(Ds2409State.MAIN),
                status(Ds2409State.MAIN),
            ]
        )
        failed_restore.fail_restore = True
        with self.assertRaises(BranchRestorationError):
            read_branch_ds2438(TARGET, failed_restore, FakeReader())

    def test_cross_process_busy_result_prevents_libusb_open(self) -> None:
        binding = RecordingBinding()
        access = VerifiedDiagnosticAccess(
            BusyLock(),  # type: ignore[arg-type]
            binding,  # type: ignore[arg-type]
            Path("unused"),
            lambda _path: inventory(),
        )
        diagnostic = Ds2438BranchDiagnostic(access=access)

        with self.assertRaisesRegex(RuntimeError, "another diagnostic process"):
            diagnostic.read(REQUEST)
        self.assertEqual(binding.verify_calls, [])
        self.assertEqual(binding.closed, 0)

    def test_adapter_process_lock_is_released_when_native_open_fails(self) -> None:
        lock = RecordingLock()
        binding = RecordingBinding(verify_error=RuntimeError("open failed"))
        access = VerifiedDiagnosticAccess(
            lock,  # type: ignore[arg-type]
            binding,  # type: ignore[arg-type]
            Path("unused"),
            lambda _path: inventory(),
        )
        diagnostic = Ds2438BranchDiagnostic(
            access=access,
            target_validator=lambda _inventory, **_kwargs: (
                TARGET.path[0],
                TARGET.evidence_timestamp,
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "open failed"):
            diagnostic.read(REQUEST)

        self.assertTrue(lock.released)
        self.assertEqual(lock.acquires, 1)
        self.assertEqual(binding.closed, 1)

    def test_evidence_is_loaded_after_lock_and_changed_evidence_is_rejected(self) -> None:
        order: list[str] = []
        state = {"inventory": inventory()}

        class RaceLock(RecordingLock):
            def acquire(self, blocking: bool = True) -> bool:
                order.append("acquire")
                state["inventory"] = inventory(OTHER_ADAPTER)
                return super().acquire(blocking)

            def release(self) -> None:
                order.append("release")
                super().release()

        def load(_path):
            order.append("load")
            return state["inventory"]

        def validate(inventory, **_kwargs):
            order.append("validate")
            return TARGET.path[0], TARGET.evidence_timestamp

        lock = RaceLock()
        binding = RecordingBinding()
        access = VerifiedDiagnosticAccess(
            lock,  # type: ignore[arg-type]
            binding,  # type: ignore[arg-type]
            Path("unused"),
            load,  # type: ignore[arg-type]
        )
        diagnostic = Ds2438BranchDiagnostic(
            access=access,
            target_validator=validate,
        )

        with self.assertRaisesRegex(RuntimeError, "differs from accepted"):
            diagnostic.read(REQUEST)

        self.assertEqual(order, ["acquire", "load", "release"])
        self.assertEqual(lock.acquires, 1)
        self.assertEqual(binding.verify_calls, [])

    def test_invalid_topology_is_rejected_before_identity_search_or_usb_open(self) -> None:
        lock = RecordingLock()
        binding = RecordingBinding()
        access = VerifiedDiagnosticAccess(
            lock,  # type: ignore[arg-type]
            binding,  # type: ignore[arg-type]
            Path("unused"),
            lambda _path: inventory(),
        )
        diagnostic = Ds2438BranchDiagnostic(access=access)

        with self.assertRaisesRegex(ValueError, "complete persisted topology"):
            diagnostic.read(REQUEST)

        self.assertEqual(binding.verify_calls, [])
        self.assertEqual(binding.closed, 1)
        self.assertTrue(lock.released)

    def test_inconsistent_adapter_assignment_is_rejected_before_identity_search(self) -> None:
        lock = RecordingLock()
        binding = RecordingBinding()
        access = VerifiedDiagnosticAccess(
            lock,  # type: ignore[arg-type]
            binding,  # type: ignore[arg-type]
            Path("unused"),
            lambda _path: topology_inventory(sensor_adapter=OTHER_ADAPTER),
        )
        diagnostic = Ds2438BranchDiagnostic(access=access)

        with self.assertRaisesRegex(ValueError, "topology path does not match"):
            diagnostic.read(REQUEST)

        self.assertEqual(binding.verify_calls, [])
        self.assertEqual(binding.closed, 1)
        self.assertTrue(lock.released)

    def test_invalid_target_identifiers_are_rejected_before_access_construction(self) -> None:
        with self.assertRaises(ValueError):
            Ds2438RequestedTarget(
                bytes.fromhex("2601020304050600"),
                ADAPTER,
                COUPLER,
                Ds2409Branch.MAIN,
            )
        with self.assertRaises(ValueError):
            Ds2438RequestedTarget(
                SENSOR,
                ADAPTER,
                bytes.fromhex("1F01020304050600"),
                Ds2409Branch.MAIN,
            )

    def test_cli_reports_native_values_but_never_fabricates_humidity(self) -> None:
        result = Ds2438DiagnosticResult(
            MEASUREMENT,
            Ds2409State.AUX,
            Ds2409State.AUX,
            (),
            False,
            TARGET.evidence_timestamp,
        )
        output = io.StringIO()
        with patch.object(
            Ds2438BranchDiagnostic, "read", return_value=result
        ), redirect_stdout(output):
            code = main(
                [
                    "ds2438-read",
                    "--rom",
                    SENSOR.hex(),
                    "--adapter-rom",
                    ADAPTER.hex(),
                    "--coupler-rom",
                    COUPLER.hex(),
                    "--branch",
                    "main",
                ]
            )

        report = json.loads(output.getvalue())
        self.assertEqual(code, 0, report)
        self.assertEqual(report["temperature_celsius"], 23.875)
        self.assertEqual(report["vad_volts"], 1.98)
        self.assertEqual(report["vdd_volts"], 5.21)
        self.assertIsNone(report["humidity_percent"])
        self.assertEqual(report["humidity_status"], "unsupported_model_unidentified")
        self.assertEqual(report["automatic_retries"], 0)
        self.assertEqual(report["eeprom_writes"], 0)
        self.assertEqual(
            report["topology_evidence_timestamp"], TARGET.evidence_timestamp
        )


if __name__ == "__main__":
    unittest.main()
