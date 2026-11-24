from __future__ import annotations

import unittest

from eisy_onewire.temperature_presentation import (
    TemperaturePresentation,
    TemperaturePresentationError,
    TemperatureUnit,
    UOM_CELSIUS,
    UOM_FAHRENHEIT,
    celsius_from_published,
)


ROM = bytes.fromhex("2811223344556656")


class TemperaturePresentationTests(unittest.TestCase):
    def test_default_and_celsius_conversion_preserve_canonical_input(self) -> None:
        default = TemperaturePresentation()
        self.assertEqual((default.unit, default.decimal_places), (TemperatureUnit.FAHRENHEIT, 1))
        self.assertEqual(default.publish(24.125), 75.4)
        celsius = default.with_unit(TemperatureUnit.CELSIUS).with_decimal_places(3)
        self.assertEqual(celsius.publish(24.125), 24.125)
        self.assertEqual((default.unit.uom, celsius.unit.uom), (UOM_FAHRENHEIT, UOM_CELSIUS))

    def test_every_supported_decimal_selection_uses_half_even_rounding(self) -> None:
        celsius = TemperaturePresentation(unit=TemperatureUnit.CELSIUS)
        expected = {0: 24.0, 1: 24.1, 2: 24.12, 3: 24.125}
        for places, value in expected.items():
            with self.subTest(places=places):
                self.assertEqual(celsius.with_decimal_places(places).publish(24.125), value)

        zero = celsius.with_decimal_places(0)
        self.assertEqual(zero.publish(2.5), 2.0)
        self.assertEqual(zero.publish(3.5), 4.0)
        self.assertEqual(zero.publish(-2.5), -2.0)
        self.assertEqual(zero.publish(-3.5), -4.0)
        self.assertEqual(
            TemperaturePresentation(decimal_places=1).publish(24.125),
            75.4,
            "75.425 F must round to 75.4 at one decimal",
        )

    def test_atomic_shape_round_trips_raw_precision_and_rejects_malformed_data(self) -> None:
        original = TemperaturePresentation().with_raw_celsius(ROM, 10.0625)
        restored = TemperaturePresentation.from_data(original.to_data())
        self.assertEqual(restored, original)
        self.assertEqual(restored.raw_celsius(ROM), 10.0625)

        malformed = original.to_data()
        malformed["decimal_places"] = 4
        with self.assertRaises(TemperaturePresentationError):
            TemperaturePresentation.from_data(malformed)
        malformed = original.to_data()
        malformed["unit"] = "kelvin"
        with self.assertRaises(TemperaturePresentationError):
            TemperaturePresentation.from_data(malformed)

    def test_legacy_published_values_are_recovered_by_their_actual_uom(self) -> None:
        self.assertAlmostEqual(celsius_from_published(50.1125, UOM_FAHRENHEIT), 10.0625)
        self.assertEqual(celsius_from_published(-10.125, UOM_CELSIUS), -10.125)
        with self.assertRaises(TemperaturePresentationError):
            celsius_from_published(20, 0)


if __name__ == "__main__":
    unittest.main()
