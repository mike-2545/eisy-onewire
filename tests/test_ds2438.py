from __future__ import annotations

import unittest

from eisy_onewire.ds2438 import (
    Ds2438CrcError,
    Ds2438RangeError,
    UnsupportedHumidityModel,
    decode_temperature,
    decode_voltage,
    humidity_percent,
    parse_ds2438_rom,
    validate_page,
    validate_voltage_pair,
)
from eisy_onewire.onewire_discovery import dallas_crc8


ROM = bytes.fromhex("2666778899AABB54")


def page(*, status: int = 0, temperature_raw: int = 0, voltage_raw: int = 0) -> bytes:
    body = bytes([status]) + temperature_raw.to_bytes(2, "little", signed=True)
    body += voltage_raw.to_bytes(2, "little") + bytes(3)
    return body + bytes([dallas_crc8(body)])


class Ds2438Tests(unittest.TestCase):
    def test_rom_page_crc_and_signed_temperature(self) -> None:
        self.assertEqual(parse_ds2438_rom(ROM.hex()), ROM)
        positive = validate_page(page(temperature_raw=int(23.875 * 256)))
        negative = validate_page(page(temperature_raw=int(-10.125 * 256)))
        self.assertEqual(decode_temperature(positive), 23.875)
        self.assertEqual(decode_temperature(negative), -10.125)

    def test_invalid_rom_crc_short_page_and_bad_page_crc_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            parse_ds2438_rom("2666778899AABB00")
        with self.assertRaises(Ds2438CrcError):
            validate_page(bytes(8))
        corrupt = bytearray(page())
        corrupt[2] ^= 1
        with self.assertRaises(Ds2438CrcError):
            validate_page(bytes(corrupt))

    def test_voltage_decoding_and_documented_ranges(self) -> None:
        self.assertEqual(decode_voltage(validate_page(page(voltage_raw=198))), 1.98)
        validate_voltage_pair(1.98, 5.21)
        with self.assertRaises(Ds2438RangeError):
            decode_voltage(validate_page(page(voltage_raw=0x8400)))
        with self.assertRaises(Ds2438RangeError):
            validate_voltage_pair(1.98, 2.3)
        with self.assertRaises(Ds2438RangeError):
            validate_voltage_pair(10.0, 4.0)

    def test_humidity_is_unavailable_without_a_physical_sensor_model(self) -> None:
        with self.assertRaises(UnsupportedHumidityModel):
            humidity_percent(model=None, temperature=23.875, vad=1.98, vdd=5.21)
        self.assertAlmostEqual(
            humidity_percent(
                model="hih3600", temperature=23.875, vad=1.98, vdd=5.21
            ),
            36.4199,
            places=3,
        )
        values = {
            model: humidity_percent(
                model=model, temperature=23.875, vad=1.98, vdd=5.21
            )
            for model in ("hih4000", "hih5030", "htm1735")
        }
        self.assertEqual(set(values), {"hih4000", "hih5030", "htm1735"})
        with self.assertRaises(UnsupportedHumidityModel):
            humidity_percent(
                model="unknown", temperature=23.875, vad=1.98, vdd=5.21
            )


if __name__ == "__main__":
    unittest.main()
