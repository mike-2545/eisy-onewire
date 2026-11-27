"""Validated DS2438 native measurements; humidity requires an explicit model."""

from __future__ import annotations

from dataclasses import dataclass

from .onewire_discovery import dallas_crc8


DS2438_FAMILY = 0x26


class Ds2438Error(RuntimeError):
    """A DS2438 response cannot be accepted."""


class Ds2438CrcError(Ds2438Error):
    """A scratchpad CRC is invalid."""


class Ds2438RangeError(Ds2438Error):
    """A decoded value is outside the documented operating range."""


class UnsupportedHumidityModel(Ds2438Error):
    """Humidity cannot be inferred without the attached analog element model."""


class Ds2438RestorationError(Ds2438Error):
    """The temporary page-zero selector could not be proven restored."""


@dataclass(frozen=True, slots=True)
class Ds2438Measurement:
    rom: bytes
    temperature_celsius: float
    temperature_fahrenheit: float
    vad_volts: float
    vdd_volts: float
    page_crc_valid: bool = True


def parse_ds2438_rom(text: str) -> bytes:
    try:
        rom = bytes.fromhex(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("DS2438 ROM must be 16 hexadecimal characters") from exc
    if len(rom) != 8 or rom[0] != DS2438_FAMILY or dallas_crc8(rom) != 0:
        raise ValueError("DS2438 ROM must be CRC-valid family 0x26")
    return rom


def validate_page(raw: bytes) -> bytes:
    if len(raw) != 9:
        raise Ds2438CrcError(f"DS2438 scratchpad returned {len(raw)} bytes; expected 9")
    if raw == bytes([0xFF]) * 9 or dallas_crc8(raw) != 0:
        raise Ds2438CrcError("DS2438 scratchpad CRC-8 validation failed")
    return raw[:8]


def decode_temperature(page: bytes) -> float:
    if len(page) != 8:
        raise ValueError("page 0 must contain eight bytes")
    raw = int.from_bytes(page[1:3], "little", signed=True)
    if raw & 0x07:
        raise Ds2438RangeError("DS2438 temperature reserved low bits are nonzero")
    value = raw / 256.0
    if not -55.0 <= value <= 125.0:
        raise Ds2438RangeError("DS2438 temperature is outside -55 to 125 C")
    return value


def decode_voltage(page: bytes) -> float:
    if len(page) != 8:
        raise ValueError("page 0 must contain eight bytes")
    raw = int.from_bytes(page[3:5], "little", signed=False)
    if raw & ~0x03FF:
        raise Ds2438RangeError("DS2438 voltage register has reserved bits set")
    return raw * 0.01


def validate_voltage_pair(vad: float, vdd: float) -> None:
    if not 2.4 <= vdd <= 10.0:
        raise Ds2438RangeError("DS2438 VDD is outside the documented 2.4-10.0 V range")
    if not 0.0 <= vad <= 10.0 or vad > 2.0 * vdd:
        raise Ds2438RangeError(
            "DS2438 VAD is outside the documented 0-10.0 V conversion and 2*VDD limits"
        )


def humidity_percent(
    *, model: str | None, temperature: float, vad: float, vdd: float
) -> float:
    """Reproduce a named OWFS formula; never infer the physical sensor model."""

    if model is None:
        raise UnsupportedHumidityModel(
            "relative humidity is unavailable until the attached analog sensor "
            "model and calibration are identified"
        )
    compensation = 1.0546 - 0.00216 * temperature
    if model == "hih3600":
        return ((vad / vdd) - (0.8 / vdd)) / 0.0062 / compensation
    if model == "hih4000":
        return ((vad / vdd) - 0.16) / 0.0062 / compensation
    if model == "hih5030":
        return ((vad / vdd) - 0.1515) / 0.00636 / compensation
    if model == "htm1735":
        return 38.92 * vad - 41.98
    raise UnsupportedHumidityModel(f"unsupported analog humidity model: {model}")
