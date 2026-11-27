"""Validated generic-voltage and calibrated-solar DS2438 application data."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Mapping

from .ds2438 import Ds2438Measurement, parse_ds2438_rom


DS2438_ANALOG_CACHE_KEY = "ds2438_analog_measurements"
DS2438_ANALOG_CACHE_SCHEMA = 1
SOLAR_MULTIPLIER_UNASSIGNED = "unassigned"
DS2438_MINIMUM_VOLTAGE = 0.0
DS2438_MAXIMUM_VOLTAGE = 10.0
MAXIMUM_IRRADIANCE_W_M2 = 5000.0


class Ds2438AnalogError(ValueError):
    """An analog reading or solar calibration is unsafe to publish."""


@dataclass(frozen=True, slots=True)
class SolarCalibration:
    """Operator-supplied linear calibration for one analog pyranometer."""

    multiplier_w_m2_per_volt: float
    offset_w_m2: float = 0.0

    def __post_init__(self) -> None:
        multiplier = _finite(self.multiplier_w_m2_per_volt, "solar multiplier")
        offset = _finite(self.offset_w_m2, "solar offset")
        if multiplier <= 0.0 or multiplier > 100_000.0:
            raise Ds2438AnalogError(
                "solar multiplier must be greater than 0 and no more than 100000 W/m2/V"
            )
        if abs(offset) > MAXIMUM_IRRADIANCE_W_M2:
            raise Ds2438AnalogError(
                "solar offset magnitude must not exceed 5000 W/m2"
            )
        object.__setattr__(self, "multiplier_w_m2_per_volt", multiplier)
        object.__setattr__(self, "offset_w_m2", offset)


@dataclass(frozen=True, slots=True)
class AnalogVoltageReading:
    rom: bytes
    temperature_celsius: float
    vad_volts: float
    vdd_volts: float


@dataclass(frozen=True, slots=True)
class SolarIrradianceReading:
    rom: bytes
    temperature_celsius: float
    vad_volts: float
    vdd_volts: float
    irradiance_w_m2: float
    calibration: SolarCalibration


CachedAnalogReading = AnalogVoltageReading | SolarIrradianceReading


@dataclass(frozen=True, slots=True)
class Ds2438AnalogMeasurementCache:
    readings: tuple[CachedAnalogReading, ...] = ()

    def to_data(self) -> dict[str, object]:
        rows: dict[str, object] = {}
        for item in self.readings:
            row: dict[str, object] = {
                "application": (
                    "solar_irradiance"
                    if isinstance(item, SolarIrradianceReading)
                    else "analog_voltage"
                ),
                "temperature_celsius": item.temperature_celsius,
                "vad_volts": item.vad_volts,
                "vdd_volts": item.vdd_volts,
            }
            if isinstance(item, SolarIrradianceReading):
                row.update(
                    {
                        "irradiance_w_m2": item.irradiance_w_m2,
                        "multiplier_w_m2_per_volt": (
                            item.calibration.multiplier_w_m2_per_volt
                        ),
                        "offset_w_m2": item.calibration.offset_w_m2,
                    }
                )
            rows[item.rom.hex().upper()] = row
        return {"schema_version": DS2438_ANALOG_CACHE_SCHEMA, "readings": rows}

    @classmethod
    def from_data(cls, raw: object) -> "Ds2438AnalogMeasurementCache":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise Ds2438AnalogError("DS2438 analog cache must be an object")
        if raw.get("schema_version") != DS2438_ANALOG_CACHE_SCHEMA:
            raise Ds2438AnalogError("DS2438 analog cache schema is unsupported")
        rows = raw.get("readings")
        if not isinstance(rows, Mapping):
            raise Ds2438AnalogError("DS2438 analog cache readings must be an object")
        readings: list[CachedAnalogReading] = []
        for key, value in rows.items():
            if not isinstance(value, Mapping):
                raise Ds2438AnalogError("DS2438 analog cache row is malformed")
            rom = parse_ds2438_rom(key)
            application = value.get("application")
            measurement = AnalogVoltageReading(
                rom,
                _temperature(value.get("temperature_celsius")),
                _voltage(value.get("vad_volts"), "VAD"),
                _voltage(value.get("vdd_volts"), "VDD"),
            )
            validate_analog_reading(measurement)
            if application == "analog_voltage":
                reading: CachedAnalogReading = measurement
            elif application == "solar_irradiance":
                calibration = SolarCalibration(
                    value.get("multiplier_w_m2_per_volt"),
                    value.get("offset_w_m2"),
                )
                reading = solar_reading_from_analog(measurement, calibration)
                cached = _finite(value.get("irradiance_w_m2"), "irradiance")
                if not math.isclose(
                    cached, reading.irradiance_w_m2, rel_tol=0.0, abs_tol=1e-9
                ):
                    raise Ds2438AnalogError(
                        "cached irradiance does not match raw voltage and calibration"
                    )
            else:
                raise Ds2438AnalogError("DS2438 analog cache application is unsupported")
            readings.append(reading)
        return cls(tuple(sorted(readings, key=lambda item: item.rom)))

    def reading(self, rom: bytes) -> CachedAnalogReading | None:
        return next((item for item in self.readings if item.rom == rom), None)

    def with_reading(
        self, reading: CachedAnalogReading
    ) -> "Ds2438AnalogMeasurementCache":
        validated: CachedAnalogReading
        if isinstance(reading, SolarIrradianceReading):
            validated = solar_reading_from_analog(
                AnalogVoltageReading(
                    reading.rom,
                    reading.temperature_celsius,
                    reading.vad_volts,
                    reading.vdd_volts,
                ),
                reading.calibration,
            )
            if not math.isclose(
                validated.irradiance_w_m2,
                reading.irradiance_w_m2,
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                raise Ds2438AnalogError("irradiance does not match voltage and calibration")
        else:
            validated = validate_analog_reading(reading)
        rows = {item.rom: item for item in self.readings}
        rows[validated.rom] = validated
        return replace(self, readings=tuple(sorted(rows.values(), key=lambda item: item.rom)))


def analog_reading_from_measurement(
    measurement: Ds2438Measurement,
) -> AnalogVoltageReading:
    return validate_analog_reading(
        AnalogVoltageReading(
            parse_ds2438_rom(measurement.rom.hex()),
            _temperature(measurement.temperature_celsius),
            _voltage(measurement.vad_volts, "VAD"),
            _voltage(measurement.vdd_volts, "VDD"),
        )
    )


def validate_analog_reading(reading: AnalogVoltageReading) -> AnalogVoltageReading:
    parse_ds2438_rom(reading.rom.hex())
    _temperature(reading.temperature_celsius)
    vad = _voltage(reading.vad_volts, "VAD")
    vdd = _voltage(reading.vdd_volts, "VDD")
    if not 2.4 <= vdd <= 10.0:
        raise Ds2438AnalogError("VDD is outside the DS2438 2.4-10 V operating range")
    if vad > 2.0 * vdd:
        raise Ds2438AnalogError("VAD must not exceed the DS2438 2*VDD limit")
    return reading


def solar_irradiance_w_m2(vad_volts: object, calibration: SolarCalibration) -> float:
    vad = _voltage(vad_volts, "VAD")
    irradiance = vad * calibration.multiplier_w_m2_per_volt + calibration.offset_w_m2
    if not math.isfinite(irradiance):
        raise Ds2438AnalogError("calculated irradiance must be finite")
    # A negative result is retained as evidence of calibration/zero offset, but
    # values outside this documented diagnostic range are rejected.
    if not -MAXIMUM_IRRADIANCE_W_M2 <= irradiance <= MAXIMUM_IRRADIANCE_W_M2:
        raise Ds2438AnalogError("calculated irradiance is outside +/-5000 W/m2")
    return irradiance


def solar_reading_from_analog(
    reading: AnalogVoltageReading, calibration: SolarCalibration
) -> SolarIrradianceReading:
    validate_analog_reading(reading)
    return SolarIrradianceReading(
        reading.rom,
        reading.temperature_celsius,
        reading.vad_volts,
        reading.vdd_volts,
        solar_irradiance_w_m2(reading.vad_volts, calibration),
        calibration,
    )


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise Ds2438AnalogError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise Ds2438AnalogError(f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise Ds2438AnalogError(f"{label} must be finite")
    return number


def _temperature(value: object) -> float:
    number = _finite(value, "temperature")
    if not -55.0 <= number <= 125.0:
        raise Ds2438AnalogError("DS2438 temperature is outside -55 to 125 C")
    return number


def _voltage(value: object, label: str) -> float:
    number = _finite(value, label)
    if not DS2438_MINIMUM_VOLTAGE <= number <= DS2438_MAXIMUM_VOLTAGE:
        raise Ds2438AnalogError(f"{label} is outside the DS2438 0-10 V range")
    return number
