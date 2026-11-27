from __future__ import annotations

import math
import unittest

from eisy_onewire.ds2438 import Ds2438Measurement
from eisy_onewire.ds2438_analog import (
    AnalogVoltageReading,
    Ds2438AnalogError,
    Ds2438AnalogMeasurementCache,
    SolarCalibration,
    SolarIrradianceReading,
    analog_reading_from_measurement,
    solar_reading_from_analog,
)


ROM = bytes.fromhex("2666778899AABB54")


def measurement(vad: float = 0.74, vdd: float = 4.97) -> Ds2438Measurement:
    return Ds2438Measurement(ROM, 17.96875, 64.34375, vad, vdd)


class Ds2438AnalogTests(unittest.TestCase):
    def test_generic_voltage_preserves_canonical_ds2438_values(self) -> None:
        reading = analog_reading_from_measurement(measurement())
        self.assertEqual(reading, AnalogVoltageReading(ROM, 17.96875, 0.74, 4.97))

    def test_solar_multiplier_offset_and_dark_vectors_are_explicit(self) -> None:
        calibration = SolarCalibration(250, -1.5)
        illuminated = solar_reading_from_analog(
            analog_reading_from_measurement(measurement()), calibration
        )
        dark = solar_reading_from_analog(
            analog_reading_from_measurement(measurement(0.01)), calibration
        )
        self.assertEqual(illuminated.irradiance_w_m2, 183.5)
        self.assertEqual(dark.irradiance_w_m2, 1.0)

    def test_small_negative_is_evidence_not_silently_clamped(self) -> None:
        reading = solar_reading_from_analog(
            analog_reading_from_measurement(measurement(0.0)),
            SolarCalibration(250, -0.4),
        )
        self.assertEqual(reading.irradiance_w_m2, -0.4)

    def test_invalid_calibration_voltage_and_irradiance_fail_closed(self) -> None:
        for multiplier in (0, -1, math.nan, math.inf, "bad"):
            with self.subTest(multiplier=multiplier), self.assertRaises(Ds2438AnalogError):
                SolarCalibration(multiplier)
        with self.assertRaises(Ds2438AnalogError):
            analog_reading_from_measurement(measurement(-0.01))
        with self.assertRaises(Ds2438AnalogError):
            solar_reading_from_analog(
                analog_reading_from_measurement(measurement(10.0, 10.0)),
                SolarCalibration(1000),
            )

    def test_cache_round_trip_revalidates_application_and_derived_value(self) -> None:
        analog = analog_reading_from_measurement(measurement(2.40, 4.98))
        solar = solar_reading_from_analog(
            analog_reading_from_measurement(measurement()), SolarCalibration(250)
        )
        cache = Ds2438AnalogMeasurementCache().with_reading(analog)
        cache = cache.with_reading(solar)
        restored = Ds2438AnalogMeasurementCache.from_data(cache.to_data())
        self.assertIsInstance(restored.reading(ROM), SolarIrradianceReading)
        damaged = cache.to_data()
        damaged["readings"][ROM.hex().upper()]["irradiance_w_m2"] = 999
        with self.assertRaises(Ds2438AnalogError):
            Ds2438AnalogMeasurementCache.from_data(damaged)


if __name__ == "__main__":
    unittest.main()
