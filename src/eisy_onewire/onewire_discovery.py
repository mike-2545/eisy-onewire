"""Protocol-independent validation of 1-Wire Search ROM responses."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol


OWFS_IDENTIFIER = re.compile(
    r"^(?P<family>[0-9A-Fa-f]{2})\.(?P<serial>[0-9A-Fa-f]{12})"
    r"(?:\.(?P<crc>[0-9A-Fa-f]{2}))?$"
)


class RomSearchTransport(Protocol):
    """Transport boundary required by discovery."""

    def search_roms(self, max_devices: int) -> tuple[bytes, ...]: ...


@dataclass(frozen=True, slots=True)
class OneWireDevice:
    """A CRC-validated 64-bit 1-Wire ROM identifier."""

    rom: bytes

    @property
    def family_code(self) -> int:
        return self.rom[0]

    @property
    def rom_address(self) -> str:
        return self.rom.hex().upper()

    @property
    def serial_number(self) -> str:
        return self.rom[1:7].hex().upper()

    @property
    def crc8(self) -> int:
        return self.rom[7]

    @property
    def owfs_identifier(self) -> str:
        return f"{self.family_code:02X}.{self.serial_number}"


@dataclass(frozen=True, slots=True)
class InvalidRom:
    """A Search ROM response rejected by length or Dallas CRC-8."""

    raw: bytes
    reason: str


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    """Accepted devices and explicitly reported rejected responses."""

    devices: tuple[OneWireDevice, ...]
    invalid_responses: tuple[InvalidRom, ...]


def dallas_crc8(data: bytes) -> int:
    """Return the Dallas/Maxim reflected CRC-8 (polynomial x^8+x^5+x^4+1)."""

    crc = 0
    for value in data:
        current = value
        for _ in range(8):
            mix = (crc ^ current) & 0x01
            crc >>= 1
            if mix:
                crc ^= 0x8C
            current >>= 1
    return crc


def rom_from_owfs_identifier(identifier: str) -> bytes:
    """Convert canonical OWFS family.ID[.CRC] text to wire-order ROM bytes."""

    match = OWFS_IDENTIFIER.fullmatch(identifier)
    if match is None:
        raise ValueError("OWFS identifier must use FF.SSSSSSSSSSSS[.CC] format")
    prefix = bytes.fromhex(match.group("family") + match.group("serial"))
    expected_crc = dallas_crc8(prefix)
    supplied_crc = match.group("crc")
    if supplied_crc is not None and int(supplied_crc, 16) != expected_crc:
        raise ValueError(
            f"OWFS identifier CRC is 0x{supplied_crc.upper()}, expected 0x{expected_crc:02X}"
        )
    return prefix + bytes([expected_crc])


def discover_devices(
    transport: RomSearchTransport, *, max_devices: int = 15
) -> DiscoveryReport:
    """Search once and accept only unique, correctly CRC-protected ROMs."""

    devices: list[OneWireDevice] = []
    invalid: list[InvalidRom] = []
    accepted: set[bytes] = set()
    for raw in transport.search_roms(max_devices):
        if len(raw) != 8:
            invalid.append(InvalidRom(raw, "ROM response is not 8 bytes"))
            continue
        if dallas_crc8(raw) != 0:
            invalid.append(InvalidRom(raw, "ROM CRC-8 is invalid"))
            continue
        if raw in accepted:
            invalid.append(InvalidRom(raw, "duplicate ROM response"))
            continue
        accepted.add(raw)
        devices.append(OneWireDevice(raw))
    return DiscoveryReport(tuple(devices), tuple(invalid))
