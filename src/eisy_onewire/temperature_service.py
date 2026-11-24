"""Shared fresh-temperature operation used by diagnostics and IoX nodes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .ds18b20 import (
    PowerMode,
    StaleConversionError,
    UnsupportedPowerModeError,
    decode_scratchpad,
)
from .ds18b20_transport import ConversionReceipt, Ds18b20Adapter


class TemperatureAdapter(Protocol):
    def detect_power(self, rom: bytes) -> PowerMode: ...

    def read_temperature(
        self, rom: bytes
    ) -> tuple[PowerMode, bytes, ConversionReceipt]: ...


@dataclass(frozen=True, slots=True)
class FreshTemperature:
    """A CRC-valid temperature tied to a completed conversion receipt."""

    rom: bytes
    power_mode: PowerMode
    resolution_bits: int
    scratchpad: bytes
    temperature_celsius: float
    temperature_fahrenheit: float
    conversion_maximum_seconds: float
    conversion_polls: int


class TemperatureService:
    """Perform one bounded fresh read through the accepted DS18B20 adapter."""

    def __init__(
        self,
        adapter_factory: Callable[[], TemperatureAdapter] = Ds18b20Adapter,
    ) -> None:
        self._adapter_factory = adapter_factory

    def detect_power(self, rom: bytes) -> PowerMode:
        return self._adapter_factory().detect_power(rom)

    def read(self, rom: bytes) -> FreshTemperature:
        power, raw, receipt = self._adapter_factory().read_temperature(rom)
        if power is not PowerMode.EXTERNAL:
            raise UnsupportedPowerModeError(
                "fresh temperature publishing requires verified external power"
            )
        decoded = decode_scratchpad(raw, conversion_completed=True)
        if receipt.rom != rom or receipt.resolution_bits != decoded.resolution_bits:
            raise StaleConversionError(
                "conversion receipt does not match the selected ROM and scratchpad"
            )
        return FreshTemperature(
            rom=rom,
            power_mode=power,
            resolution_bits=decoded.resolution_bits,
            scratchpad=decoded.raw,
            temperature_celsius=decoded.temperature_celsius,
            temperature_fahrenheit=decoded.temperature_fahrenheit,
            conversion_maximum_seconds=receipt.maximum_seconds,
            conversion_polls=receipt.polls,
        )
