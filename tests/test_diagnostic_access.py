from __future__ import annotations

import unittest
from pathlib import Path

from eisy_onewire.adapter_binding import (
    AdapterIdentityBinding,
    AdapterIdentityMismatchError,
)
from eisy_onewire.diagnostic_access import (
    DiagnosticAccessError,
    DiagnosticAdapterBusyError,
    VerifiedDiagnosticAccess,
    parse_adapter_identity,
)
from eisy_onewire.discovery_inventory import DiscoveryInventory, KnownDevice
from eisy_onewire.ds2490_transport import AdapterNotFoundError
from eisy_onewire.onewire_discovery import DiscoveryReport, OneWireDevice, dallas_crc8
from eisy_onewire.usb_detector import UsbDeviceDescriptor


def rom(family: int, serial: bytes) -> bytes:
    prefix = bytes([family]) + serial
    return prefix + bytes([dallas_crc8(prefix)])


EXPECTED = rom(0x81, bytes.fromhex("010203040506"))
OTHER = rom(0x81, bytes.fromhex("111213141516"))
SENSOR = rom(0x28, bytes.fromhex("212223242526"))


def inventory(adapter: bytes = EXPECTED) -> DiscoveryInventory:
    timestamp = "2026-11-27T12:00:00Z"
    return DiscoveryInventory((KnownDevice(adapter, timestamp, timestamp),))


def descriptor(address: int = 4) -> UsbDeviceDescriptor:
    return UsbDeviceDescriptor(
        0x110, 0, 0, 0, 8, 0x04FA, 0x2490, 2, 1, 0, address, (2, 1)
    )


class Lock:
    def __init__(self, acquired: bool = True) -> None:
        self.acquired = acquired
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self, blocking: bool = True) -> bool:
        self.acquire_calls += 1
        self.blocking = blocking
        return self.acquired

    def release(self) -> None:
        self.release_calls += 1


class Session:
    def __init__(self, factory: "PersistentFactory") -> None:
        self.factory = factory

    def __enter__(self):
        if not self.factory.valid:
            raise AdapterNotFoundError("verified adapter disconnected")
        self.factory.session_entries += 1
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None


class PersistentFactory:
    def __init__(self, *, valid: bool = True, address: int = 4) -> None:
        self.descriptor = descriptor(address)
        self.valid = valid
        self.closed = 0
        self.session_entries = 0

    def open(self):
        if not self.valid:
            raise AdapterNotFoundError("verified adapter disconnected")
        return Session(self)

    def close(self) -> None:
        self.closed += 1
        self.valid = False


class DiagnosticAccessTests(unittest.TestCase):
    def binding(self, factory, *identities: bytes) -> AdapterIdentityBinding:
        return AdapterIdentityBinding(
            candidate_factory=lambda: factory,
            identity_search=lambda _factory: DiscoveryReport(
                tuple(OneWireDevice(item) for item in identities), ()
            ),
        )

    def access(self, lock, binding, loaded=inventory()):
        return VerifiedDiagnosticAccess(
            lock,
            binding,
            Path("unused"),
            lambda _path: loaded,
        )

    def test_concurrent_plugin_ownership_prevents_evidence_or_usb_access(self) -> None:
        lock = Lock(False)
        factory = PersistentFactory()
        binding = self.binding(factory, EXPECTED)
        loads = 0
        access = VerifiedDiagnosticAccess(
            lock,
            binding,
            Path("unused"),
            lambda _path: (_ for _ in ()).throw(
                AssertionError("evidence must not load while lock is busy")
            ),
        )

        with self.assertRaises(DiagnosticAdapterBusyError):
            access.run(EXPECTED, lambda *_args: None)

        self.assertEqual(loads, 0)
        self.assertEqual(lock.release_calls, 0)
        self.assertEqual(factory.closed, 0)

    def test_same_retained_handle_is_used_and_cleanup_is_guaranteed(self) -> None:
        lock = Lock()
        factory = PersistentFactory()
        binding = self.binding(factory, EXPECTED, SENSOR)
        seen = []

        def operation(retained, loaded):
            self.assertIs(retained, binding)
            self.assertIs(loaded, inventory_value)
            with retained.open():
                seen.append("operation")
            return "ok"

        inventory_value = inventory()
        result = self.access(lock, binding, inventory_value).run(EXPECTED, operation)

        self.assertEqual(result, "ok")
        self.assertEqual(seen, ["operation"])
        self.assertEqual(factory.session_entries, 1)
        self.assertEqual(factory.closed, 1)
        self.assertEqual(lock.release_calls, 1)

    def test_preflight_prepares_once_before_identity_search_and_hardware(self) -> None:
        events: list[str] = []
        lock = Lock()
        factory = PersistentFactory()

        def candidate():
            events.append("open_candidate")
            return factory

        def identity_search(_factory):
            events.append("identity_search")
            return DiscoveryReport((OneWireDevice(EXPECTED),), ())

        binding = AdapterIdentityBinding(
            candidate_factory=candidate,
            identity_search=identity_search,
        )
        access = VerifiedDiagnosticAccess(
            lock,
            binding,
            Path("unused"),
            lambda _path: (events.append("load_evidence"), inventory())[1],
        )

        def preflight(loaded):
            events.append("preflight")
            self.assertEqual(loaded, inventory())
            return "prepared-target"

        def operation(retained, prepared):
            events.append("operation")
            self.assertIs(retained, binding)
            self.assertEqual(prepared, "prepared-target")
            with retained.open():
                pass
            return "ok"

        self.assertEqual(
            access.run(EXPECTED, operation, preflight=preflight),
            "ok",
        )
        self.assertEqual(
            events,
            [
                "load_evidence",
                "preflight",
                "open_candidate",
                "identity_search",
                "operation",
            ],
        )
        self.assertEqual(factory.session_entries, 1)
        self.assertEqual(factory.closed, 1)
        self.assertEqual(lock.release_calls, 1)

    def test_preflight_failure_opens_nothing_and_releases_lock(self) -> None:
        lock = Lock()
        factory = PersistentFactory()
        binding = self.binding(factory, EXPECTED)

        with self.assertRaisesRegex(RuntimeError, "invalid topology"):
            self.access(lock, binding).run(
                EXPECTED,
                lambda *_args: self.fail("hardware operation must not run"),
                preflight=lambda _inventory: (_ for _ in ()).throw(
                    RuntimeError("invalid topology")
                ),
            )

        self.assertEqual(factory.session_entries, 0)
        self.assertEqual(factory.closed, 0)
        self.assertEqual(lock.release_calls, 1)

    def test_missing_evidence_opens_nothing_and_releases_lock(self) -> None:
        lock = Lock()
        factory = PersistentFactory()
        binding = self.binding(factory, EXPECTED)
        access = VerifiedDiagnosticAccess(
            lock,
            binding,
            Path("missing"),
            lambda _path: (_ for _ in ()).throw(
                RuntimeError("discovery evidence is unavailable")
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "evidence is unavailable"):
            access.run(EXPECTED, lambda *_args: None)

        self.assertEqual(factory.session_entries, 0)
        self.assertEqual(factory.closed, 0)
        self.assertEqual(lock.release_calls, 1)

    def test_sole_unexpected_adapter_and_address_reuse_never_run_operation(self) -> None:
        for address in (4, 9):
            with self.subTest(address=address):
                lock = Lock()
                factory = PersistentFactory(address=address)
                binding = self.binding(factory, OTHER)
                operations = 0

                def operation(*_args):
                    nonlocal operations
                    operations += 1

                with self.assertRaises(AdapterIdentityMismatchError):
                    self.access(lock, binding).run(EXPECTED, operation)
                self.assertEqual(operations, 0)
                self.assertEqual(factory.closed, 1)
                self.assertEqual(lock.release_calls, 1)

    def test_multiple_or_missing_identity_never_runs_operation(self) -> None:
        cases = ((), (EXPECTED, OTHER))
        for identities in cases:
            with self.subTest(identities=identities):
                lock = Lock()
                factory = PersistentFactory()
                binding = self.binding(factory, *identities)
                with self.assertRaises(RuntimeError):
                    self.access(lock, binding).run(
                        EXPECTED,
                        lambda *_args: self.fail("sensor operation must not run"),
                    )
                self.assertEqual(factory.closed, 1)
                self.assertEqual(lock.release_calls, 1)

    def test_disconnect_during_verification_and_before_operation_fail_once(self) -> None:
        calls = 0

        def missing_candidate():
            nonlocal calls
            calls += 1
            raise AdapterNotFoundError("disconnect during identity verification")

        lock = Lock()
        binding = AdapterIdentityBinding(candidate_factory=missing_candidate)
        with self.assertRaises(AdapterNotFoundError):
            self.access(lock, binding).run(EXPECTED, lambda *_args: None)
        self.assertEqual(calls, 1)
        self.assertEqual(lock.release_calls, 1)

        factory = PersistentFactory()
        binding = self.binding(factory, EXPECTED)
        lock = Lock()

        def disconnect(retained, _inventory):
            factory.valid = False
            with retained.open():
                self.fail("disconnected handle must not enter")

        with self.assertRaises(AdapterNotFoundError):
            self.access(lock, binding).run(EXPECTED, disconnect)
        self.assertEqual(factory.closed, 1)
        self.assertEqual(lock.release_calls, 1)

    def test_persisted_identity_mismatch_and_invalid_requested_crc_open_nothing(self) -> None:
        factory = PersistentFactory()
        binding = self.binding(factory, EXPECTED)
        lock = Lock()
        with self.assertRaisesRegex(DiagnosticAccessError, "differs"):
            self.access(lock, binding, inventory(OTHER)).run(
                EXPECTED, lambda *_args: None
            )
        self.assertEqual(factory.closed, 0)
        self.assertEqual(lock.release_calls, 1)

        with self.assertRaises(DiagnosticAccessError):
            parse_adapter_identity("8101020304050600")

    def test_operation_failure_closes_handle_and_releases_lock(self) -> None:
        lock = Lock()
        factory = PersistentFactory()
        binding = self.binding(factory, EXPECTED)
        with self.assertRaisesRegex(RuntimeError, "sensor failed"):
            self.access(lock, binding).run(
                EXPECTED,
                lambda *_args: (_ for _ in ()).throw(RuntimeError("sensor failed")),
            )
        self.assertEqual(factory.closed, 1)
        self.assertEqual(lock.release_calls, 1)


if __name__ == "__main__":
    unittest.main()
