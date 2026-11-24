"""Bounded DS2490 transport for native DS2438 temperature and voltages."""

from __future__ import annotations

import math
import time
from typing import Callable, Protocol

from .ds18b20_transport import Ds18b20Transport
from .ds2438 import (
    Ds2438Measurement,
    Ds2438RestorationError,
    decode_temperature,
    decode_voltage,
    parse_ds2438_rom,
    validate_page,
    validate_voltage_pair,
)
from .ds2490_transport import UsbSession, UsbTransferTimeout


MATCH_ROM = 0x55
WRITE_SCRATCHPAD = 0x4E
READ_SCRATCHPAD = 0xBE
RECALL_MEMORY = 0xB8
CONVERT_T = 0x44
CONVERT_V = 0xB4
PAGE_ZERO = 0x00
AD_SELECT_MASK = 0x08
CONVERSION_MAX_SECONDS = 0.010
CONVERSION_MARGIN_SECONDS = 0.020
POLL_INTERVAL_SECONDS = 0.002


class OneWireBlockIo(Protocol):
    """The already-reviewed bounded DS2490 1-Wire primitives used here."""

    def _reset_bus(self) -> None: ...

    def _block_io(self, payload: bytes) -> bytes: ...

    def _read_bit(self) -> int: ...


class Ds2438Transport:
    """Use only scratchpad RAM, conversions, and CRC-protected page reads."""

    def __init__(
        self,
        session: UsbSession,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        wire: OneWireBlockIo | None = None,
    ) -> None:
        self._wire = wire or Ds18b20Transport(
            session, monotonic=monotonic, sleeper=sleeper
        )
        self._monotonic = monotonic
        self._sleeper = sleeper

    def read_measurements(self, rom: bytes) -> Ds2438Measurement:
        rom = parse_ds2438_rom(rom.hex())
        original = self._read_page0(rom)
        original_status = original[0]
        try:
            self._convert(rom, CONVERT_T, "temperature")
            temperature_page = self._read_page0(rom)
            temperature = decode_temperature(temperature_page)
            vad = self._convert_voltage(rom, original_status, vdd=False)
            vdd = self._convert_voltage(rom, original_status, vdd=True)
            validate_voltage_pair(vad, vdd)
            return Ds2438Measurement(
                rom,
                temperature,
                temperature * 9.0 / 5.0 + 32.0,
                vad,
                vdd,
            )
        finally:
            # Recall restores persisted page 0 into scratchpad RAM. No Copy
            # Scratchpad command is ever implemented or issued.
            restored = self._read_page0(rom)
            if restored[0] != original_status:
                raise Ds2438RestorationError(
                    "DS2438 page-0 status was not restored after diagnostic"
                )

    def _convert_voltage(self, rom: bytes, original_status: int, *, vdd: bool) -> float:
        desired = (original_status | AD_SELECT_MASK) if vdd else (
            original_status & ~AD_SELECT_MASK
        )
        self._write_status_scratchpad(rom, desired)
        self._convert(rom, CONVERT_V, "voltage")
        page = self._read_page0(rom)
        return decode_voltage(page)

    def _read_page0(self, rom: bytes) -> bytes:
        self._wire._reset_bus()
        self._wire._block_io(bytes([MATCH_ROM]) + rom + bytes([RECALL_MEMORY, PAGE_ZERO]))
        self._wire._reset_bus()
        response = self._wire._block_io(
            bytes([MATCH_ROM])
            + rom
            + bytes([READ_SCRATCHPAD, PAGE_ZERO])
            + bytes([0xFF]) * 9
        )
        return validate_page(response[-9:])

    def _write_status_scratchpad(self, rom: bytes, status: int) -> None:
        self._wire._reset_bus()
        self._wire._block_io(
            bytes([MATCH_ROM, *rom, WRITE_SCRATCHPAD, PAGE_ZERO, status])
        )
        self._wire._reset_bus()
        response = self._wire._block_io(
            bytes([MATCH_ROM])
            + rom
            + bytes([READ_SCRATCHPAD, PAGE_ZERO])
            + bytes([0xFF]) * 9
        )
        page = validate_page(response[-9:])
        if page[0] != status:
            raise Ds2438RestorationError(
                "DS2438 scratchpad AD selector write did not verify"
            )

    def _convert(self, rom: bytes, command: int, label: str) -> None:
        self._wire._reset_bus()
        self._wire._block_io(bytes([MATCH_ROM]) + rom + bytes([command]))
        deadline = self._monotonic() + CONVERSION_MAX_SECONDS + CONVERSION_MARGIN_SECONDS
        polls = math.ceil(
            (CONVERSION_MAX_SECONDS + CONVERSION_MARGIN_SECONDS)
            / POLL_INTERVAL_SECONDS
        ) + 1
        for _ in range(polls):
            if self._wire._read_bit():
                return
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            self._sleeper(min(POLL_INTERVAL_SECONDS, remaining))
        raise UsbTransferTimeout(f"DS2438 {label} conversion did not complete in 30 ms")
