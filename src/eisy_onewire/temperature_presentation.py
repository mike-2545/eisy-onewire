"""Validated, USB-free temperature presentation preferences and conversion."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from enum import Enum
import math
from typing import Mapping

from .ds18b20 import InvalidRomError, parse_ds18b20_rom


TEMPERATURE_PRESENTATION_KEY = "temperature_presentation"
TEMPERATURE_PRESENTATION_SCHEMA = 1
UOM_CELSIUS = 4
UOM_FAHRENHEIT = 17
ALLOWED_DECIMAL_PLACES = (0, 1, 2, 3)


class TemperaturePresentationError(ValueError):
    """Persisted presentation state is malformed or unsupported."""


class TemperatureUnit(str, Enum):
    FAHRENHEIT = "fahrenheit"
    CELSIUS = "celsius"

    @property
    def uom(self) -> int:
        return UOM_FAHRENHEIT if self is TemperatureUnit.FAHRENHEIT else UOM_CELSIUS

    @property
    def controller_value(self) -> int:
        return 0 if self is TemperatureUnit.FAHRENHEIT else 1


@dataclass(frozen=True, slots=True)
class TemperaturePresentation:
    """One global display policy plus canonical last-valid Celsius values."""

    unit: TemperatureUnit = TemperatureUnit.FAHRENHEIT
    decimal_places: int = 1
    raw_celsius_by_rom: tuple[tuple[bytes, float], ...] = ()

    def __post_init__(self) -> None:
        if self.decimal_places not in ALLOWED_DECIMAL_PLACES:
            raise TemperaturePresentationError(
                "temperature decimal places must be one of 0, 1, 2, or 3"
            )

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": TEMPERATURE_PRESENTATION_SCHEMA,
            "unit": self.unit.value,
            "decimal_places": self.decimal_places,
            "raw_celsius_by_rom": {
                rom.hex().upper(): value for rom, value in self.raw_celsius_by_rom
            },
        }

    @classmethod
    def from_data(cls, raw: object) -> "TemperaturePresentation":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise TemperaturePresentationError(
                "temperature presentation must be an object"
            )
        if raw.get("schema_version") != TEMPERATURE_PRESENTATION_SCHEMA:
            raise TemperaturePresentationError(
                "temperature presentation schema is unsupported"
            )
        try:
            unit = TemperatureUnit(raw.get("unit"))
        except (TypeError, ValueError) as exc:
            raise TemperaturePresentationError(
                "temperature unit must be fahrenheit or celsius"
            ) from exc
        places = raw.get("decimal_places")
        if isinstance(places, bool) or not isinstance(places, int):
            raise TemperaturePresentationError(
                "temperature decimal places must be an integer"
            )
        values = raw.get("raw_celsius_by_rom")
        if not isinstance(values, Mapping):
            raise TemperaturePresentationError(
                "raw temperature cache must be an object"
            )
        parsed: list[tuple[bytes, float]] = []
        for key, value in values.items():
            try:
                rom = parse_ds18b20_rom(key)
            except (TypeError, ValueError, InvalidRomError) as exc:
                raise TemperaturePresentationError(
                    "raw temperature cache contains an invalid DS18B20 ROM"
                ) from exc
            celsius = _finite_celsius(value)
            parsed.append((rom, celsius))
        return cls(unit, places, tuple(sorted(parsed, key=lambda item: item[0])))

    def with_unit(self, unit: TemperatureUnit | str) -> "TemperaturePresentation":
        try:
            parsed = unit if isinstance(unit, TemperatureUnit) else TemperatureUnit(unit)
        except (TypeError, ValueError) as exc:
            raise TemperaturePresentationError(
                "temperature unit must be fahrenheit or celsius"
            ) from exc
        return replace(self, unit=parsed)

    def with_decimal_places(self, decimal_places: int) -> "TemperaturePresentation":
        return replace(self, decimal_places=decimal_places)

    def raw_celsius(self, rom: bytes) -> float | None:
        return next((value for key, value in self.raw_celsius_by_rom if key == rom), None)

    def with_raw_celsius(self, rom: bytes, value: object) -> "TemperaturePresentation":
        parsed_rom = parse_ds18b20_rom(rom.hex())
        celsius = _finite_celsius(value)
        values = dict(self.raw_celsius_by_rom)
        values[parsed_rom] = celsius
        return replace(
            self,
            raw_celsius_by_rom=tuple(sorted(values.items(), key=lambda item: item[0])),
        )

    def publish(self, raw_celsius: object) -> float:
        """Convert and round with decimal ROUND_HALF_EVEN semantics."""

        celsius = Decimal(str(_finite_celsius(raw_celsius)))
        value = (
            celsius * Decimal(9) / Decimal(5) + Decimal(32)
            if self.unit is TemperatureUnit.FAHRENHEIT
            else celsius
        )
        quantum = Decimal(1).scaleb(-self.decimal_places)
        return float(value.quantize(quantum, rounding=ROUND_HALF_EVEN))


def celsius_from_published(value: object, uom: object) -> float:
    """Recover the canonical unit from a legacy persisted numeric ST value."""

    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TemperaturePresentationError(
            "persisted temperature is not numeric"
        ) from exc
    if not number.is_finite():
        raise TemperaturePresentationError("persisted temperature is not finite")
    if uom == UOM_FAHRENHEIT:
        number = (number - Decimal(32)) * Decimal(5) / Decimal(9)
    elif uom != UOM_CELSIUS:
        raise TemperaturePresentationError("persisted temperature UOM is unsupported")
    return _finite_celsius(float(number))


def _finite_celsius(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise TemperaturePresentationError("raw Celsius temperature must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TemperaturePresentationError(
            "raw Celsius temperature must be numeric"
        ) from exc
    if not math.isfinite(number) or not -55.0 <= number <= 125.0:
        raise TemperaturePresentationError(
            "raw Celsius temperature is outside the supported sensor range"
        )
    return number
