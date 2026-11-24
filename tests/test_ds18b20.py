from __future__ import annotations

import unittest

from eisy_onewire.ds18b20 import (
    InvalidRomError,
    ScratchpadError,
    StaleConversionError,
    decode_scratchpad,
    parse_ds18b20_rom,
)
from eisy_onewire.onewire_discovery import dallas_crc8


def scratchpad(raw_temperature: int, *, resolution: int = 12) -> bytes:
    config = {9: 0x1F, 10: 0x3F, 11: 0x5F, 12: 0x7F}[resolution]
    prefix = raw_temperature.to_bytes(2, "little", signed=True) + bytes(
        [0x4B, 0x46, config, 0xFF, 0x0C, 0x10]
    )
    return prefix + bytes([dallas_crc8(prefix)])


class Ds18b20Tests(unittest.TestCase):
    def test_sensor_a_rom_is_complete_family_28_and_crc_valid(self) -> None:
        self.assertEqual(
            parse_ds18b20_rom("2811223344556656"),
            bytes.fromhex("2811223344556656"),
        )

    def test_invalid_rom_length_hex_family_and_crc_are_rejected(self) -> None:
        for value in (
            "2876",
            "28112233445566ZZ",
            "814455667788999C",
            "2811223344556657",
        ):
            with self.subTest(value=value), self.assertRaises(InvalidRomError):
                parse_ds18b20_rom(value)

    def test_positive_fractional_temperature(self) -> None:
        decoded = decode_scratchpad(scratchpad(0x0191), conversion_completed=True)

        self.assertEqual(decoded.temperature_celsius, 25.0625)
        self.assertAlmostEqual(decoded.temperature_fahrenheit, 77.1125)
        self.assertEqual(decoded.resolution_bits, 12)

    def test_negative_fractional_temperature(self) -> None:
        decoded = decode_scratchpad(scratchpad(-162), conversion_completed=True)

        self.assertEqual(decoded.temperature_celsius, -10.125)
        self.assertAlmostEqual(decoded.temperature_fahrenheit, 13.775)

    def test_resolution_masks_undefined_low_bits(self) -> None:
        decoded = decode_scratchpad(
            scratchpad(0x0197, resolution=9), conversion_completed=True
        )

        self.assertEqual(decoded.temperature_celsius, 25.0)
        self.assertEqual(decoded.temperature_fahrenheit, 77.0)

    def test_incomplete_and_crc_invalid_scratchpads_are_rejected(self) -> None:
        valid = scratchpad(0x0191)
        with self.assertRaisesRegex(ScratchpadError, "exactly 9"):
            decode_scratchpad(valid[:8], conversion_completed=True)
        with self.assertRaisesRegex(ScratchpadError, "CRC-8"):
            decode_scratchpad(valid[:-1] + bytes([valid[-1] ^ 0x01]), conversion_completed=True)

    def test_temperature_outside_device_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(ScratchpadError, "outside"):
            decode_scratchpad(scratchpad(126 * 16), conversion_completed=True)

    def test_stale_data_is_rejected_without_conversion_receipt(self) -> None:
        with self.assertRaises(StaleConversionError):
            decode_scratchpad(scratchpad(0x0550), conversion_completed=False)


if __name__ == "__main__":
    unittest.main()
