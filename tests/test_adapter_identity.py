from __future__ import annotations

import io
import json
import unittest
from types import SimpleNamespace

from eisy_onewire.adapter_identity import (
    AdapterIdentityDiagnostic,
    enumeration_report,
    main,
)
from eisy_onewire.onewire_discovery import dallas_crc8
from eisy_onewire.usb_detector import (
    DS9490_PRODUCT_ID,
    DS9490_VENDOR_ID,
    UsbDeviceDescriptor,
    UsbDeviceSelector,
    UsbEnumerationResult,
)


def rom(family: int, serial: bytes) -> bytes:
    prefix = bytes([family]) + serial
    return prefix + bytes([dallas_crc8(prefix)])


class FakeLock:
    def __init__(self, acquired: bool = True) -> None:
        self.acquired = acquired
        self.released = False

    def acquire(self, blocking: bool = True) -> bool:
        self.blocking = blocking
        return self.acquired

    def release(self) -> None:
        self.released = True


class FakeAdapter:
    def __init__(self, roms: tuple[bytes, ...]) -> None:
        self.roms = roms
        self.calls = 0

    def search_roms(self, max_devices: int) -> tuple[bytes, ...]:
        self.calls += 1
        self.max_devices = max_devices
        return self.roms


def descriptor(address: int = 4) -> UsbDeviceDescriptor:
    return UsbDeviceDescriptor(
        0x110, 0, 0, 0, 8, DS9490_VENDOR_ID, DS9490_PRODUCT_ID,
        2, 1, 1, address, (2, address),
    )


class AdapterIdentityTests(unittest.TestCase):
    def test_enumeration_report_is_descriptor_only(self) -> None:
        rendered = enumeration_report(UsbEnumerationResult((descriptor(), descriptor(5))))
        self.assertEqual(rendered["matching_adapter_count"], 2)
        self.assertEqual(rendered["usb_operations"]["transfers"], 0)

    def test_selected_adapter_requires_exactly_one_valid_family_81_rom(self) -> None:
        identity = rom(0x81, bytes.fromhex("010203040506"))
        sensor = rom(0x28, bytes.fromhex("111213141516"))
        lock = FakeLock()
        adapter = FakeAdapter((identity, sensor))
        factory = SimpleNamespace(selected_descriptor=descriptor())
        diagnostic = AdapterIdentityDiagnostic(
            UsbDeviceSelector(1, 4), lock, lambda selected: (adapter, factory)
        )

        report = diagnostic.run()

        self.assertEqual(report["adapter_identity_rom"], identity.hex().upper())
        self.assertEqual(report["operation"]["search_commands"], 1)
        self.assertEqual(report["operation"]["branch_commands"], 0)
        self.assertEqual(adapter.calls, 1)
        self.assertTrue(lock.released)

    def test_identity_failure_releases_lock_and_never_retries(self) -> None:
        for responses in ((), (rom(0x81, b"123456"), rom(0x81, b"654321"))):
            lock = FakeLock()
            adapter = FakeAdapter(responses)
            diagnostic = AdapterIdentityDiagnostic(
                UsbDeviceSelector(1, 4),
                lock,
                lambda selected: (adapter, SimpleNamespace(selected_descriptor=None)),
            )
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                diagnostic.run()
            self.assertEqual(adapter.calls, 1)
            self.assertTrue(lock.released)

    def test_invalid_rom_crc_is_rejected_and_lock_is_released(self) -> None:
        lock = FakeLock()
        invalid = bytes.fromhex("8101020304050600")
        adapter = FakeAdapter((invalid,))
        diagnostic = AdapterIdentityDiagnostic(
            UsbDeviceSelector(1, 4),
            lock,
            lambda selected: (adapter, SimpleNamespace(selected_descriptor=None)),
        )
        with self.assertRaisesRegex(RuntimeError, "invalid ROM"):
            diagnostic.run()
        self.assertEqual(adapter.calls, 1)
        self.assertTrue(lock.released)

    def test_cli_inventory_and_disconnected_selection_fail_closed(self) -> None:
        output = io.StringIO()
        code = main(
            ["adapter-enumerate"],
            enumerator=lambda: UsbEnumerationResult((descriptor(),)),
            output=output,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["matching_adapter_count"], 1)

        error = io.StringIO()
        code = main(
            ["adapter-identify", "--bus-number", "1", "--device-address", "9"],
            identity_factory=lambda selector: (_ for _ in ()).throw(
                RuntimeError("selected adapter disconnected")
            ),
            error_output=error,
        )
        self.assertEqual(code, 1)
        self.assertIn("disconnected", json.loads(error.getvalue())["detail"])


if __name__ == "__main__":
    unittest.main()
