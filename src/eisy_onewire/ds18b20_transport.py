"""Bounded DS2490 transactions for one explicitly selected DS18B20."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable

from .ds18b20 import (
    ConversionTimeoutError,
    DeviceUnavailableError,
    PowerMode,
    UnsupportedPowerModeError,
    resolution_from_scratchpad,
)
from .ds2490_transport import (
    COMMUNICATION_COMMAND,
    COMM_FLUSH_ON_ERROR,
    COMM_IMMEDIATE,
    DEFAULT_STATUS_POLLS,
    Ds2490ProtocolError,
    Ds2490Status,
    Ds2490Transport,
    UsbSession,
    UsbSessionFactory,
    UsbTransferTimeout,
    CtypesLibUsbSessionFactory,
    RESULT_DEVICE_DETECT,
    RESULT_NO_RESPONSE,
)


COMM_BIT_IO = 0x0020
COMM_DATA = 0x0008
COMM_ONE_WIRE_RESET = 0x0042
COMM_BLOCK_IO = 0x0074

MATCH_ROM = 0x55
CONVERT_T = 0x44
READ_POWER_SUPPLY = 0xB4
READ_SCRATCHPAD = 0xBE

MAX_BLOCK_BYTES = 128
POLL_INTERVAL_SECONDS = 0.010
CONVERSION_MARGIN_SECONDS = 0.100
CONVERSION_SECONDS = {9: 0.09375, 10: 0.1875, 11: 0.375, 12: 0.750}


@dataclass(frozen=True, slots=True)
class ConversionReceipt:
    rom: bytes
    resolution_bits: int
    maximum_seconds: float
    polls: int


class Ds18b20Transport:
    """Issue only the reviewed reset, Match ROM, power, and temperature commands."""

    def __init__(
        self,
        session: UsbSession,
        *,
        max_status_polls: int = DEFAULT_STATUS_POLLS,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = session
        self._status = Ds2490Transport(session, max_status_polls=max_status_polls)
        self._max_status_polls = max_status_polls
        self._monotonic = monotonic
        self._sleeper = sleeper

    def read_power_supply(self, rom: bytes) -> PowerMode:
        """Select one ROM and read its power-supply bit without converting."""

        self._reset_bus()
        self._block_io(bytes([MATCH_ROM]) + rom + bytes([READ_POWER_SUPPLY]))
        return PowerMode.EXTERNAL if self._read_bit() else PowerMode.PARASITE

    def read_scratchpad(self, rom: bytes) -> bytes:
        """Select one ROM and return its complete CRC-protected scratchpad."""

        self._reset_bus()
        request = bytes([MATCH_ROM]) + rom + bytes([READ_SCRATCHPAD]) + bytes([0xFF]) * 9
        response = self._block_io(request)
        scratchpad = response[-9:]
        if scratchpad == bytes([0xFF]) * 9:
            raise DeviceUnavailableError("selected ROM did not return a scratchpad")
        # Validate here so malformed data never controls conversion timing.
        resolution_from_scratchpad(scratchpad)
        return scratchpad

    def convert_temperature_external(
        self, rom: bytes, *, resolution_bits: int
    ) -> ConversionReceipt:
        """Start and poll an externally powered conversion; never enable SPU."""

        if resolution_bits not in CONVERSION_SECONDS:
            raise ValueError("resolution_bits must be 9, 10, 11, or 12")
        self._reset_bus()
        self._block_io(bytes([MATCH_ROM]) + rom + bytes([CONVERT_T]))
        maximum = CONVERSION_SECONDS[resolution_bits]
        deadline = self._monotonic() + maximum + CONVERSION_MARGIN_SECONDS
        max_polls = math.ceil((maximum + CONVERSION_MARGIN_SECONDS) / POLL_INTERVAL_SECONDS) + 1
        for poll in range(1, max_polls + 1):
            if self._read_bit():
                return ConversionReceipt(rom, resolution_bits, maximum, poll)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            self._sleeper(min(POLL_INTERVAL_SECONDS, remaining))
        raise ConversionTimeoutError(
            f"DS18B20 conversion did not complete within {maximum:g} seconds plus margin"
        )

    @staticmethod
    def reject_parasite_conversion() -> None:
        """Fail before Convert T until DS2490 strong-pull-up timing is approved."""

        raise UnsupportedPowerModeError(
            "parasite-powered conversion is disabled pending DS2490 strong-pull-up verification"
        )

    def _reset_bus(self) -> None:
        self._session.control(
            COMMUNICATION_COMMAND,
            COMM_ONE_WIRE_RESET | COMM_IMMEDIATE | COMM_FLUSH_ON_ERROR,
            0,
        )
        status, no_response = self._wait_until_idle()
        if no_response:
            raise DeviceUnavailableError("1-Wire reset detected no presence pulse")
        if status.data_in_count:
            raise Ds2490ProtocolError("1-Wire reset unexpectedly queued EP3 data")

    def _block_io(self, payload: bytes) -> bytes:
        if not payload or len(payload) > MAX_BLOCK_BYTES:
            raise ValueError("DS2490 block length must be between 1 and 128 bytes")
        self._session.bulk_out(payload)
        self._session.control(
            COMMUNICATION_COMMAND,
            COMM_BLOCK_IO | COMM_IMMEDIATE | COMM_FLUSH_ON_ERROR,
            len(payload),
        )
        status, no_response = self._wait_until_idle()
        if no_response:
            raise DeviceUnavailableError("selected device did not respond to block I/O")
        if status.data_in_count != len(payload):
            raise Ds2490ProtocolError(
                "DS2490 block I/O final status reported "
                f"{status.data_in_count} bytes; expected {len(payload)}"
            )
        response = self._session.bulk_in(len(payload))
        if len(response) != len(payload):
            raise Ds2490ProtocolError(
                f"DS2490 block I/O returned {len(response)} bytes; expected {len(payload)}"
            )
        return response

    def _read_bit(self) -> bool:
        self._session.control(
            COMMUNICATION_COMMAND,
            COMM_BIT_IO | COMM_IMMEDIATE | COMM_DATA | COMM_FLUSH_ON_ERROR,
            0,
        )
        status, no_response = self._wait_until_idle()
        if no_response:
            raise DeviceUnavailableError("selected device did not respond to read time slot")
        if status.data_in_count != 1:
            raise Ds2490ProtocolError(
                f"DS2490 bit I/O queued {status.data_in_count} bytes; expected 1"
            )
        response = self._session.bulk_in(1)
        if len(response) != 1:
            raise Ds2490ProtocolError(
                f"DS2490 bit I/O returned {len(response)} bytes; expected 1"
            )
        return bool(response[0] & 0x01)

    def _wait_until_idle(self) -> tuple[Ds2490Status, bool]:
        no_response = False
        for _ in range(self._max_status_polls):
            status = self._status._read_status()
            self._status._raise_for_status(status)
            no_response |= any(
                result & RESULT_NO_RESPONSE
                for result in status.results
                if result != RESULT_DEVICE_DETECT
            )
            if status.idle:
                return status, no_response
            if status.halted:
                raise Ds2490ProtocolError("DS2490 halted during DS18B20 transaction")
        raise UsbTransferTimeout("DS2490 did not become idle during DS18B20 transaction")


class Ds18b20Adapter:
    """Own one claimed DS2490 session for one explicit diagnostic."""

    def __init__(self, factory: UsbSessionFactory | None = None) -> None:
        self._factory = factory or CtypesLibUsbSessionFactory.for_search_diagnostic()

    def detect_power(self, rom: bytes) -> PowerMode:
        with self._factory.open() as session:
            Ds2490Transport(session).initialize()
            return Ds18b20SessionAdapter(session).detect_power(rom)

    def read_temperature(self, rom: bytes) -> tuple[PowerMode, bytes, ConversionReceipt]:
        with self._factory.open() as session:
            Ds2490Transport(session).initialize()
            return Ds18b20SessionAdapter(session).read_temperature(rom)


class Ds18b20SessionAdapter:
    """Reuse the accepted temperature sequence inside an already owned session."""

    def __init__(self, session: UsbSession) -> None:
        self._transport = Ds18b20Transport(session)

    def detect_power(self, rom: bytes) -> PowerMode:
        self._transport.read_scratchpad(rom)
        return self._transport.read_power_supply(rom)

    def read_temperature(
        self, rom: bytes
    ) -> tuple[PowerMode, bytes, ConversionReceipt]:
        before = self._transport.read_scratchpad(rom)
        power = self._transport.read_power_supply(rom)
        if power is PowerMode.PARASITE:
            self._transport.reject_parasite_conversion()
        resolution = resolution_from_scratchpad(before)
        receipt = self._transport.convert_temperature_external(
            rom, resolution_bits=resolution
        )
        after = self._transport.read_scratchpad(rom)
        return power, after, receipt
