from __future__ import annotations

import unittest

from eisy_onewire.onewire_discovery import (
    dallas_crc8,
    discover_devices,
    rom_from_owfs_identifier,
)


def rom(prefix: bytes) -> bytes:
    if len(prefix) != 7:
        raise ValueError("ROM prefix must be seven bytes")
    return prefix + bytes([dallas_crc8(prefix)])


class FakeSearchTransport:
    def __init__(self, responses: tuple[bytes, ...]) -> None:
        self.responses = responses
        self.maximum: int | None = None

    def search_roms(self, max_devices: int) -> tuple[bytes, ...]:
        self.maximum = max_devices
        return self.responses


class OneWireDiscoveryTests(unittest.TestCase):
    def test_dallas_crc8_standard_check_value(self) -> None:
        self.assertEqual(dallas_crc8(b"123456789"), 0xA1)

    def test_multiple_devices_report_rom_and_family_codes(self) -> None:
        first = rom(bytes.fromhex("28010203040506"))
        second = rom(bytes.fromhex("100a0b0c0d0e0f"))
        transport = FakeSearchTransport((first, second))

        report = discover_devices(transport, max_devices=7)

        self.assertEqual(transport.maximum, 7)
        self.assertEqual([device.rom for device in report.devices], [first, second])
        self.assertEqual([device.family_code for device in report.devices], [0x28, 0x10])
        self.assertEqual(report.devices[0].rom_address, first.hex().upper())
        self.assertEqual(report.invalid_responses, ())

    def test_sensor_a_owfs_identifier_maps_to_complete_wire_order_rom(self) -> None:
        sensor_a = rom_from_owfs_identifier("28.112233445566")

        self.assertEqual(sensor_a.hex().upper(), "2811223344556656")
        self.assertEqual(dallas_crc8(sensor_a), 0)
        report = discover_devices(FakeSearchTransport((sensor_a,)))
        self.assertEqual(report.devices[0].family_code, 0x28)
        self.assertEqual(report.devices[0].serial_number, "112233445566")
        self.assertEqual(report.devices[0].crc8, 0x56)
        self.assertEqual(report.devices[0].owfs_identifier, "28.112233445566")

    def test_owfs_identifier_optional_crc_must_match(self) -> None:
        expected = bytes.fromhex("2811223344556656")

        self.assertEqual(rom_from_owfs_identifier("28.112233445566.56"), expected)
        with self.assertRaisesRegex(ValueError, "expected 0x56"):
            rom_from_owfs_identifier("28.112233445566.00")
        with self.assertRaisesRegex(ValueError, "FF.SSSSSSSSSSSS"):
            rom_from_owfs_identifier("28-112233445566")

    def test_invalid_crc_is_reported_and_not_discovered(self) -> None:
        valid = rom(bytes.fromhex("28010203040506"))
        invalid = valid[:-1] + bytes([valid[-1] ^ 0x01])

        report = discover_devices(FakeSearchTransport((invalid, valid)))

        self.assertEqual([device.rom for device in report.devices], [valid])
        self.assertEqual(len(report.invalid_responses), 1)
        self.assertEqual(report.invalid_responses[0].raw, invalid)
        self.assertIn("CRC-8", report.invalid_responses[0].reason)

    def test_duplicate_and_wrong_length_responses_are_rejected(self) -> None:
        valid = rom(bytes.fromhex("28010203040506"))

        report = discover_devices(FakeSearchTransport((valid, valid, b"short")))

        self.assertEqual(len(report.devices), 1)
        self.assertEqual(
            [item.reason for item in report.invalid_responses],
            ["duplicate ROM response", "ROM response is not 8 bytes"],
        )

    def test_no_devices_is_a_successful_empty_report(self) -> None:
        report = discover_devices(FakeSearchTransport(()))

        self.assertEqual(report.devices, ())
        self.assertEqual(report.invalid_responses, ())


if __name__ == "__main__":
    unittest.main()
