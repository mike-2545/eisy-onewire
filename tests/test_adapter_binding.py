from __future__ import annotations

import unittest

from eisy_onewire.adapter_binding import (
    AdapterBindingError,
    AdapterIdentityBinding,
    AdapterIdentityMismatchError,
)
from eisy_onewire.ds2490_transport import AdapterNotFoundError, UsbTransportError
from eisy_onewire.onewire_discovery import (
    DiscoveryReport,
    InvalidRom,
    OneWireDevice,
    dallas_crc8,
)
from eisy_onewire.usb_detector import UsbDeviceDescriptor


def rom(family: int, serial: bytes) -> bytes:
    prefix = bytes([family]) + serial
    return prefix + bytes([dallas_crc8(prefix)])


def descriptor(bus: int, address: int, path: tuple[int, ...]) -> UsbDeviceDescriptor:
    return UsbDeviceDescriptor(
        0x110, 0, 0, 0, 8, 0x04FA, 0x2490, 2, 1, bus, address, path
    )


class FakeContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None


class FakePersistentFactory:
    def __init__(self, location: UsbDeviceDescriptor) -> None:
        self.descriptor = location
        self.valid = True
        self.closed = 0
        self.opened = 0

    def open(self):
        if not self.valid:
            raise AdapterNotFoundError("adapter was disconnected")
        self.opened += 1
        return FakeContext()

    def close(self) -> None:
        self.closed += 1
        self.valid = False


class FailingTransactionContext:
    def __init__(self, factory: FakePersistentFactory) -> None:
        self.factory = factory

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.factory.valid = False
        return None


class AdapterBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.expected = rom(0x81, bytes.fromhex("010203040506"))
        self.other = rom(0x81, bytes.fromhex("111213141516"))
        self.sensor = rom(0x28, bytes.fromhex("212223242526"))

    def binding(self, factories, reports) -> AdapterIdentityBinding:
        factory_iter = iter(factories)
        report_iter = iter(reports)
        return AdapterIdentityBinding(
            candidate_factory=lambda: next(factory_iter),
            identity_search=lambda factory: next(report_iter),
        )

    def report(self, *roms: bytes) -> DiscoveryReport:
        return DiscoveryReport(tuple(OneWireDevice(item) for item in roms), ())

    def test_verified_identity_is_bound_to_retained_handle_not_location(self) -> None:
        factory = FakePersistentFactory(descriptor(0, 4, (2, 1)))
        binding = self.binding([factory], [self.report(self.expected, self.sensor)])

        evidence = binding.verify(self.expected)

        self.assertTrue(binding.verified)
        self.assertEqual(evidence.identity_rom, self.expected)
        with binding.open():
            pass
        self.assertEqual(factory.opened, 1)
        self.assertEqual(factory.closed, 0)

    def test_original_removed_while_second_remains_never_falls_back(self) -> None:
        second = FakePersistentFactory(descriptor(0, 4, (2, 1)))
        binding = self.binding([second], [self.report(self.other)])

        with self.assertRaises(AdapterIdentityMismatchError):
            binding.verify(self.expected)

        self.assertFalse(binding.verified)
        self.assertEqual(second.closed, 1)
        with self.assertRaisesRegex(AdapterNotFoundError, "Verify Adapter Identity"):
            binding.open()

    def test_address_reuse_by_different_adapter_is_rejected(self) -> None:
        reused = FakePersistentFactory(descriptor(0, 4, (2, 1)))
        binding = self.binding([reused], [self.report(self.other)])
        with self.assertRaises(AdapterIdentityMismatchError):
            binding.verify(self.expected)
        self.assertEqual(reused.closed, 1)

    def test_bus_address_change_for_same_identity_can_be_explicitly_rebound(self) -> None:
        first = FakePersistentFactory(descriptor(0, 4, (2, 1)))
        second = FakePersistentFactory(descriptor(1, 7, (3, 4)))
        binding = self.binding(
            [first, second],
            [self.report(self.expected), self.report(self.expected)],
        )
        binding.verify(self.expected)
        first.valid = False

        evidence = binding.verify(self.expected)

        self.assertEqual(evidence.descriptor.device_address, 7)
        self.assertEqual(first.closed, 1)
        self.assertTrue(binding.verified)

    def test_disconnect_during_transaction_invalidates_without_reenumeration(self) -> None:
        factory = FakePersistentFactory(descriptor(0, 4, (2,)))
        candidate_calls = 0

        def candidate():
            nonlocal candidate_calls
            candidate_calls += 1
            return factory

        binding = AdapterIdentityBinding(
            candidate_factory=candidate,
            identity_search=lambda unused: self.report(self.expected),
        )
        binding.verify(self.expected)
        factory.valid = False

        with self.assertRaisesRegex(AdapterNotFoundError, "Verify Adapter Identity"):
            binding.open()
        self.assertEqual(candidate_calls, 1)

    def test_disconnect_notification_is_actionable_and_does_not_retry(self) -> None:
        factory = FakePersistentFactory(descriptor(0, 4, (2,)))
        notices = []
        binding = AdapterIdentityBinding(
            candidate_factory=lambda: factory,
            identity_search=lambda unused: self.report(self.expected),
            on_invalidated=notices.append,
        )
        binding.verify(self.expected)
        factory.valid = False

        with self.assertRaises(AdapterNotFoundError):
            binding.open()

        self.assertEqual(
            notices, ["verified DS9490 retained handle became unusable"]
        )
        self.assertEqual(factory.closed, 1)

    def test_failed_transaction_discards_cached_evidence_and_requires_reverify(self) -> None:
        original = FakePersistentFactory(descriptor(0, 3, (4,)))
        wrong = FakePersistentFactory(descriptor(0, 5, (6,)))
        reconnected = FakePersistentFactory(descriptor(0, 7, (4,)))
        factories = iter((original, wrong, reconnected))
        reports = iter(
            (
                self.report(self.expected),
                self.report(self.other),
                self.report(self.expected),
            )
        )
        notices = []
        binding = AdapterIdentityBinding(
            candidate_factory=lambda: next(factories),
            identity_search=lambda unused: next(reports),
            on_invalidated=notices.append,
        )
        binding.verify(self.expected)
        original.open = lambda: FailingTransactionContext(original)

        with self.assertRaisesRegex(UsbTransportError, "libusb error -11"):
            with binding.open():
                raise UsbTransportError("read USB configuration", -11)

        self.assertFalse(binding.verified)
        self.assertIsNone(binding.evidence)
        self.assertEqual(original.closed, 1)
        self.assertEqual(
            notices, ["verified DS9490 retained handle became unusable"]
        )

        with self.assertRaises(AdapterIdentityMismatchError):
            binding.verify(self.expected)
        self.assertEqual(wrong.closed, 1)
        self.assertFalse(binding.verified)

        evidence = binding.verify(self.expected)
        self.assertEqual(evidence.identity_rom, self.expected)
        self.assertEqual(evidence.descriptor.device_address, 7)
        self.assertTrue(binding.verified)

    def test_restart_never_trusts_persisted_location_or_identity_without_search(self) -> None:
        factory = FakePersistentFactory(descriptor(0, 4, (2,)))
        binding = self.binding([factory], [self.report(self.expected)])

        with self.assertRaisesRegex(AdapterNotFoundError, "not verified"):
            binding.open()

        self.assertEqual(factory.opened, 0)

    def test_missing_duplicate_crc_invalid_and_incorrect_identity_fail_closed(self) -> None:
        invalid = bytes.fromhex("8101020304050600")
        cases = (
            self.report(self.sensor),
            self.report(self.expected, self.other),
            DiscoveryReport((), (InvalidRom(invalid, "ROM CRC-8 is invalid"),)),
            self.report(self.other),
        )
        for report in cases:
            with self.subTest(report=report):
                factory = FakePersistentFactory(descriptor(0, 4, (2,)))
                binding = self.binding([factory], [report])
                with self.assertRaises(AdapterBindingError):
                    binding.verify(self.expected)
                self.assertFalse(binding.verified)
                self.assertEqual(factory.closed, 1)

    def test_invalid_accepted_identity_opens_nothing(self) -> None:
        calls = 0

        def candidate():
            nonlocal calls
            calls += 1
            return FakePersistentFactory(descriptor(0, 4, (2,)))

        binding = AdapterIdentityBinding(candidate_factory=candidate)
        with self.assertRaisesRegex(AdapterBindingError, "CRC-valid"):
            binding.verify(bytes.fromhex("8101020304050600"))
        self.assertEqual(calls, 0)

    def test_verification_has_no_retry(self) -> None:
        calls = 0

        def candidate():
            nonlocal calls
            calls += 1
            raise AdapterNotFoundError("disconnect between enumeration and open")

        binding = AdapterIdentityBinding(candidate_factory=candidate)
        with self.assertRaises(AdapterNotFoundError):
            binding.verify(self.expected)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
