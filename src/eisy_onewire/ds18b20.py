"""Protocol-independent DS18B20 validation and temperature decoding."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .onewire_discovery import dallas_crc8


DS18B20_FAMILY = 0x28
SCRATCHPAD_SIZE = 9


class Ds18b20Error(RuntimeError):
    """Base class for a rejected DS18B20 diagnostic."""


class InvalidRomError(Ds18b20Error):
    """The requested ROM is not a validated DS18B20 identifier."""


class DeviceUnavailableError(Ds18b20Error):
    """The selected device did not respond to the transaction."""


class ScratchpadError(Ds18b20Error):
    """The scratchpad is incomplete, malformed, or fails CRC validation."""


class ConversionTimeoutError(Ds18b20Error):
    """The selected sensor did not report conversion completion in time."""


class UnsupportedPowerModeError(Ds18b20Error):
    """The requested operation requires an unimplemented power mode."""


class StaleConversionError(Ds18b20Error):
    """Temperature data was requested without a completed conversion."""


class PowerMode(str, Enum):
    EXTERNAL = "external"
    PARASITE = "parasite"


@dataclass(frozen=True, slots=True)
class Scratchpad:
    raw: bytes
    resolution_bits: int
    temperature_celsius: float
    temperature_fahrenheit: float


def parse_ds18b20_rom(text: str) -> bytes:
    """Parse one complete wire-order ROM and validate family and CRC-8."""

    normalized = text.strip()
    if len(normalized) != 16:
        raise InvalidRomError("ROM must contain exactly 16 hexadecimal characters")
    try:
        rom = bytes.fromhex(normalized)
    except ValueError as exc:
        raise InvalidRomError("ROM must contain only hexadecimal characters") from exc
    if rom[0] != DS18B20_FAMILY:
        raise InvalidRomError(
            f"ROM family is 0x{rom[0]:02X}; DS18B20 family 0x28 is required"
        )
    if dallas_crc8(rom) != 0:
        raise InvalidRomError("ROM CRC-8 is invalid")
    return rom


def resolution_from_scratchpad(raw: bytes) -> int:
    """Return the configured DS18B20 resolution after validating the scratchpad."""

    _validate_scratchpad(raw)
    return 9 + ((raw[4] >> 5) & 0x03)


def decode_scratchpad(raw: bytes, *, conversion_completed: bool) -> Scratchpad:
    """Validate and decode a scratchpad from a confirmed fresh conversion."""

    if not conversion_completed:
        raise StaleConversionError(
            "temperature scratchpad was not preceded by a completed conversion"
        )
    resolution = resolution_from_scratchpad(raw)
    unsigned = int.from_bytes(raw[0:2], "little", signed=False)
    masks = {9: 0xFFF8, 10: 0xFFFC, 11: 0xFFFE, 12: 0xFFFF}
    unsigned &= masks[resolution]
    signed = unsigned - 0x10000 if unsigned & 0x8000 else unsigned
    celsius = signed / 16.0
    if not -55.0 <= celsius <= 125.0:
        raise ScratchpadError(
            f"decoded temperature {celsius:g} C is outside the DS18B20 range"
        )
    fahrenheit = celsius * 9.0 / 5.0 + 32.0
    return Scratchpad(raw, resolution, celsius, fahrenheit)


def _validate_scratchpad(raw: bytes) -> None:
    if len(raw) != SCRATCHPAD_SIZE:
        raise ScratchpadError(
            f"scratchpad returned {len(raw)} bytes; exactly {SCRATCHPAD_SIZE} are required"
        )
    if dallas_crc8(raw) != 0:
        raise ScratchpadError("scratchpad CRC-8 is invalid")

