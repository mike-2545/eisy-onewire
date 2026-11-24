from __future__ import annotations

import math
import unittest

from eisy_onewire.ds2438 import Ds2438Measurement
from eisy_onewire.ds2438_humidity import (
    Ds2438MeasurementCache,
    Hih4000MeasurementError,
    Hih4000Reading,
    hih4000_relative_humidity,
    reading_from_measurement,
)


ROM = bytes.fromhex("2666778899AABB54")


class Hih4000Tests(unittest.TestCase):
    def test_official_nominal_transfer_and_temperature_compensation(self) -> None:
        value = hih4000_relative_humidity(
            temperature_celsius=23.875, vad_volts=1.98, vdd_volts=5.21
        )
        expected = ((1.98 / 5.21) - 0.16) / 0.0062 / (
            1.0546 - 0.00216 * 23.875
        )
        self.assertAlmostEqual(value, expected, places=12)
        self.assertAlmostEqual(value, 35.3829, places=3)

    def test_measurement_adapter_preserves_canonical_values(self) -> None:
        measurement = Ds2438Measurement(ROM, 23.875, 74.975, 1.98, 5.21)
        reading = reading_from_measurement(measurement)
        self.assertEqual(reading.rom, ROM)
        self.assertEqual(reading.temperature_celsius, 23.875)
        self.assertEqual(reading.vad_volts, 1.98)
        self.assertEqual(reading.vdd_volts, 5.21)

    def test_invalid_inputs_and_implausible_outputs_are_rejected_not_clamped(self) -> None:
        cases = (
            {"temperature_celsius": math.nan, "vad_volts": 1.98, "vdd_volts": 5.21},
            {"temperature_celsius": 23.0, "vad_volts": 1.98, "vdd_volts": 0.0},
            {"temperature_celsius": 23.0, "vad_volts": 1.98, "vdd_volts": 3.9},
            {"temperature_celsius": 23.0, "vad_volts": 6.0, "vdd_volts": 5.0},
            {"temperature_celsius": 23.0, "vad_volts": 0.0, "vdd_volts": 5.0},
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(Hih4000MeasurementError):
                hih4000_relative_humidity(**values)

    def test_cache_round_trip_and_tamper_rejection(self) -> None:
        reading = reading_from_measurement(
            Ds2438Measurement(ROM, 23.875, 74.975, 1.98, 5.21)
        )
        cache = Ds2438MeasurementCache().with_reading(reading)
        self.assertEqual(Ds2438MeasurementCache.from_data(cache.to_data()), cache)
        damaged = cache.to_data()
        damaged["readings"][ROM.hex().upper()]["humidity_percent"] = 90.0
        with self.assertRaises(Hih4000MeasurementError):
            Ds2438MeasurementCache.from_data(damaged)


if __name__ == "__main__":
    unittest.main()
