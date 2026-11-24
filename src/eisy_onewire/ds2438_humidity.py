"""Validated HIH-4000 application math and persistent DS2438 measurements."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Mapping

from .ds2438 import Ds2438Measurement, parse_ds2438_rom


DS2438_MEASUREMENT_CACHE_KEY = "ds2438_measurements"
DS2438_MEASUREMENT_CACHE_SCHEMA = 1
HIH4000_MINIMUM_SUPPLY_VOLTS = 4.0
HIH4000_MAXIMUM_SUPPLY_VOLTS = 5.8


class Hih4000MeasurementError(ValueError):
    """A DS2438/HIH-4000 measurement cannot be published safely."""


@dataclass(frozen=True, slots=True)
class Hih4000Reading:
    rom: bytes
    temperature_celsius: float
    humidity_percent: float
    vad_volts: float
    vdd_volts: float


@dataclass(frozen=True, slots=True)
class Ds2438MeasurementCache:
    readings: tuple[Hih4000Reading, ...] = ()

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": DS2438_MEASUREMENT_CACHE_SCHEMA,
            "readings": {
                item.rom.hex().upper(): {
                    "temperature_celsius": item.temperature_celsius,
                    "humidity_percent": item.humidity_percent,
                    "vad_volts": item.vad_volts,
                    "vdd_volts": item.vdd_volts,
                }
                for item in self.readings
            },
        }

    @classmethod
    def from_data(cls, raw: object) -> "Ds2438MeasurementCache":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise Hih4000MeasurementError("DS2438 measurement cache must be an object")
        if raw.get("schema_version") != DS2438_MEASUREMENT_CACHE_SCHEMA:
            raise Hih4000MeasurementError("DS2438 measurement cache schema is unsupported")
        rows = raw.get("readings")
        if not isinstance(rows, Mapping):
            raise Hih4000MeasurementError("DS2438 measurement cache readings must be an object")
        readings: list[Hih4000Reading] = []
        seen: set[bytes] = set()
        for key, value in rows.items():
            try:
                rom = parse_ds2438_rom(key)
            except (TypeError, ValueError) as exc:
                raise Hih4000MeasurementError(
                    "DS2438 measurement cache contains an invalid ROM"
                ) from exc
            if rom in seen or not isinstance(value, Mapping):
                raise Hih4000MeasurementError(
                    "DS2438 measurement cache contains a duplicate or malformed row"
                )
            seen.add(rom)
            reading = Hih4000Reading(
                rom,
                _temperature(value.get("temperature_celsius")),
                _humidity(value.get("humidity_percent")),
                _voltage(value.get("vad_volts"), "VAD"),
                _voltage(value.get("vdd_volts"), "VDD"),
            )
            readings.append(validate_hih4000_reading(reading))
        return cls(tuple(sorted(readings, key=lambda item: item.rom)))

    def reading(self, rom: bytes) -> Hih4000Reading | None:
        return next((item for item in self.readings if item.rom == rom), None)

    def with_reading(self, reading: Hih4000Reading) -> "Ds2438MeasurementCache":
        rows = {item.rom: item for item in self.readings}
        rows[parse_ds2438_rom(reading.rom.hex())] = validate_hih4000_reading(reading)
        return replace(
            self,
            readings=tuple(sorted(rows.values(), key=lambda item: item.rom)),
        )


def hih4000_relative_humidity(
    *, temperature_celsius: object, vad_volts: object, vdd_volts: object
) -> float:
    """Apply Honeywell's nominal HIH-4000 transfer and compensation equations.

    VAD is the sensor output and VDD is the sensor supply for an explicitly
    configured HIH-4000 application.  Invalid results are rejected, never
    clamped into the physical range.
    """

    temperature = _temperature(temperature_celsius)
    vad = _voltage(vad_volts, "VAD")
    vdd = _voltage(vdd_volts, "VDD")
    validate_hih4000_electrical(vad, vdd)
    compensation = 1.0546 - 0.00216 * temperature
    if not math.isfinite(compensation) or compensation <= 0.0:
        raise Hih4000MeasurementError(
            "HIH-4000 temperature compensation denominator is invalid"
        )
    sensor_rh = ((vad / vdd) - 0.16) / 0.0062
    true_rh = sensor_rh / compensation
    if not math.isfinite(true_rh) or not 0.0 <= true_rh <= 100.0:
        raise Hih4000MeasurementError(
            "HIH-4000 relative humidity is outside the physical 0-100 percent range"
        )
    return true_rh


def reading_from_measurement(measurement: Ds2438Measurement) -> Hih4000Reading:
    humidity = hih4000_relative_humidity(
        temperature_celsius=measurement.temperature_celsius,
        vad_volts=measurement.vad_volts,
        vdd_volts=measurement.vdd_volts,
    )
    return Hih4000Reading(
        parse_ds2438_rom(measurement.rom.hex()),
        _temperature(measurement.temperature_celsius),
        humidity,
        _voltage(measurement.vad_volts, "VAD"),
        _voltage(measurement.vdd_volts, "VDD"),
    )


def validate_hih4000_reading(reading: Hih4000Reading) -> Hih4000Reading:
    expected = hih4000_relative_humidity(
        temperature_celsius=reading.temperature_celsius,
        vad_volts=reading.vad_volts,
        vdd_volts=reading.vdd_volts,
    )
    humidity = _humidity(reading.humidity_percent)
    if not math.isclose(humidity, expected, rel_tol=0.0, abs_tol=1e-9):
        raise Hih4000MeasurementError(
            "cached humidity does not match the validated temperature and voltages"
        )
    return reading


def validate_hih4000_electrical(vad: float, vdd: float) -> None:
    if not HIH4000_MINIMUM_SUPPLY_VOLTS <= vdd <= HIH4000_MAXIMUM_SUPPLY_VOLTS:
        raise Hih4000MeasurementError(
            "HIH-4000 supply voltage is outside the documented 4.0-5.8 V range"
        )
    if not 0.0 <= vad <= vdd:
        raise Hih4000MeasurementError(
            "HIH-4000 output voltage must be between ground and its supply"
        )


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise Hih4000MeasurementError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise Hih4000MeasurementError(f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise Hih4000MeasurementError(f"{label} must be finite")
    return number


def _temperature(value: object) -> float:
    number = _number(value, "temperature")
    if not -40.0 <= number <= 85.0:
        raise Hih4000MeasurementError(
            "HIH-4000 compensated temperature is outside -40 to 85 C"
        )
    return number


def _humidity(value: object) -> float:
    number = _number(value, "relative humidity")
    if not 0.0 <= number <= 100.0:
        raise Hih4000MeasurementError(
            "relative humidity is outside the physical 0-100 percent range"
        )
    return number


def _voltage(value: object, label: str) -> float:
    number = _number(value, label)
    if not 0.0 <= number <= 10.0:
        raise Hih4000MeasurementError(f"{label} is outside 0-10 V")
    return number
