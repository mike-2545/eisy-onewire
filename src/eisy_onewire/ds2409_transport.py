"""Bounded DS2409 branch control over an already claimed DS2490 session.

The operations in this module are never run at plugin startup.  They are used
only by the explicitly triggered topology diagnostic while its caller holds
the shared adapter lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .ds2490_transport import (
    COMMUNICATION_COMMAND,
    COMM_FLUSH_ON_ERROR,
    COMM_IMMEDIATE,
    DEFAULT_STATUS_POLLS,
    Ds2490ProtocolError,
    Ds2490Transport,
    RESULT_DEVICE_DETECT,
    RESULT_NO_RESPONSE,
    UsbSession,
    UsbTransferTimeout,
)
from .onewire_discovery import dallas_crc8


COMM_BLOCK_IO = 0x0074
COMM_ONE_WIRE_RESET = 0x0042
MATCH_ROM = 0x55
STATUS_READ_WRITE = 0x5A
ALL_LINES_OFF = 0x66
SMART_ON_MAIN = 0xCC
SMART_ON_AUX = 0x33
READ_SLOT = 0xFF
DS2409_FAMILY = 0x1F
MAX_BLOCK_BYTES = 128


class Ds2409ProtocolError(Ds2490ProtocolError):
    """The coupler returned an unsafe or contradictory result."""


class Ds2409Branch(str, Enum):
    MAIN = "main"
    AUX = "aux"


class Ds2409State(str, Enum):
    OFF = "off"
    MAIN = "main"
    AUX = "aux"


class Ds2409ControlMode(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class Ds2409StatusInfo:
    state: Ds2409State
    main_event: bool
    aux_event: bool
    raw: int
    main_level_high: bool = True
    aux_level_high: bool = True
    control_mode: Ds2409ControlMode = Ds2409ControlMode.AUTO
    control_association: Ds2409Branch | None = Ds2409Branch.MAIN
    control_output_on: bool | None = None

    @property
    def main_level_valid(self) -> bool:
        return self.state is not Ds2409State.MAIN

    @property
    def aux_level_valid(self) -> bool:
        return self.state is not Ds2409State.AUX


@dataclass(frozen=True, slots=True)
class BranchHop:
    coupler_rom: bytes
    branch: Ds2409Branch

    def __post_init__(self) -> None:
        _validate_coupler_rom(self.coupler_rom)

    @property
    def label(self) -> str:
        return f"{self.coupler_rom.hex().upper()}/{self.branch.value}"


class Ds2409Transport:
    """Issue only status, Smart-On, and All-Lines-Off commands."""

    def __init__(
        self,
        session: UsbSession,
        *,
        max_status_polls: int = DEFAULT_STATUS_POLLS,
    ) -> None:
        if max_status_polls < 1:
            raise ValueError("max_status_polls must be positive")
        self._session = session
        self._status = Ds2490Transport(session, max_status_polls=max_status_polls)
        self._max_status_polls = max_status_polls

    def read_state(self, coupler_rom: bytes) -> Ds2409State:
        """Read branch state without altering status/control configuration."""

        return self.read_status(coupler_rom).state

    def read_status(self, coupler_rom: bytes) -> Ds2409StatusInfo:
        """Read restorable branch state and non-restorable event flags."""

        _validate_coupler_rom(coupler_rom)
        self._reset_bus()
        response = self._block_io(
            bytes([MATCH_ROM])
            + coupler_rom
            + bytes([STATUS_READ_WRITE, READ_SLOT, READ_SLOT, READ_SLOT])
        )
        info, confirmation = response[-2:]
        if confirmation != info:
            raise Ds2409ProtocolError(
                "DS2409 status confirmation mismatch: "
                f"status 0x{info:02X}, confirmation 0x{confirmation:02X}"
            )
        main_active = not bool(info & 0x01)
        aux_active = not bool(info & 0x04)
        if main_active and aux_active:
            raise Ds2409ProtocolError(
                "DS2409 status reported main and auxiliary active simultaneously"
            )
        if main_active:
            state = Ds2409State.MAIN
        elif aux_active:
            state = Ds2409State.AUX
        else:
            state = Ds2409State.OFF
        control_mode = (
            Ds2409ControlMode.MANUAL
            if info & 0x80
            else Ds2409ControlMode.AUTO
        )
        control_status = bool(info & 0x40)
        return Ds2409StatusInfo(
            state=state,
            main_event=bool(info & 0x10),
            aux_event=bool(info & 0x20),
            raw=info,
            main_level_high=bool(info & 0x02),
            aux_level_high=bool(info & 0x08),
            control_mode=control_mode,
            control_association=(
                Ds2409Branch.AUX if control_status else Ds2409Branch.MAIN
            )
            if control_mode is Ds2409ControlMode.AUTO
            else None,
            control_output_on=(
                control_status
                if control_mode is Ds2409ControlMode.MANUAL
                else None
            ),
        )

    def select_branch(self, coupler_rom: bytes, branch: Ds2409Branch) -> int:
        """Select one branch with Smart-On and return its reset-response byte."""

        _validate_coupler_rom(coupler_rom)
        if not isinstance(branch, Ds2409Branch):
            raise ValueError("branch must be Ds2409Branch.MAIN or Ds2409Branch.AUX")
        command = SMART_ON_MAIN if branch is Ds2409Branch.MAIN else SMART_ON_AUX
        self._reset_bus()
        response = self._block_io(
            bytes([MATCH_ROM])
            + coupler_rom
            + bytes([command, READ_SLOT, READ_SLOT, READ_SLOT])
        )
        reset_response, confirmation = response[-2:]
        if confirmation != command:
            raise Ds2409ProtocolError(
                "DS2409 Smart-On confirmation mismatch: "
                f"expected 0x{command:02X}, received 0x{confirmation:02X}"
            )
        return reset_response

    def all_lines_off(self, coupler_rom: bytes) -> None:
        """Deactivate both outputs and verify the command echo."""

        _validate_coupler_rom(coupler_rom)
        self._reset_bus()
        response = self._block_io(
            bytes([MATCH_ROM]) + coupler_rom + bytes([ALL_LINES_OFF, READ_SLOT])
        )
        if response[-1] != ALL_LINES_OFF:
            raise Ds2409ProtocolError(
                "DS2409 All-Lines-Off confirmation mismatch: "
                f"expected 0x{ALL_LINES_OFF:02X}, received 0x{response[-1]:02X}"
            )

    def restore_state(self, coupler_rom: bytes, state: Ds2409State) -> None:
        """Restore one previously observed state without guessing."""

        if state is Ds2409State.OFF:
            self.all_lines_off(coupler_rom)
        elif state is Ds2409State.MAIN:
            self.select_branch(coupler_rom, Ds2409Branch.MAIN)
        elif state is Ds2409State.AUX:
            self.select_branch(coupler_rom, Ds2409Branch.AUX)
        else:  # pragma: no cover - Enum exhaustiveness guard
            raise ValueError(f"unsupported DS2409 state {state!r}")

    def _reset_bus(self) -> None:
        self._session.control(
            COMMUNICATION_COMMAND,
            COMM_ONE_WIRE_RESET | COMM_IMMEDIATE | COMM_FLUSH_ON_ERROR,
            0,
        )
        status, no_response = self._wait_until_idle()
        if no_response:
            raise Ds2409ProtocolError("1-Wire reset did not detect the DS2409")
        if status.data_in_count:
            raise Ds2409ProtocolError("1-Wire reset unexpectedly queued EP3 data")

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
            raise Ds2409ProtocolError("DS2409 did not respond to block I/O")
        if status.data_in_count != len(payload):
            raise Ds2409ProtocolError(
                "DS2490 DS2409 block I/O final status reported "
                f"{status.data_in_count} bytes; expected {len(payload)}"
            )
        response = self._session.bulk_in(len(payload))
        if len(response) != len(payload):
            raise Ds2409ProtocolError(
                "DS2490 DS2409 block I/O returned "
                f"{len(response)} bytes; expected {len(payload)}"
            )
        return response

    def _wait_until_idle(self):
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
                raise Ds2409ProtocolError("DS2490 halted during DS2409 transaction")
        raise UsbTransferTimeout("DS2490 did not become idle during DS2409 transaction")


def branch_path_label(path: tuple[BranchHop, ...]) -> str:
    if not path:
        return "root"
    return ">".join(hop.label for hop in path)


def _validate_coupler_rom(rom: bytes) -> None:
    if len(rom) != 8 or rom[0] != DS2409_FAMILY or dallas_crc8(rom) != 0:
        raise ValueError("coupler ROM must be a CRC-valid family-0x1F address")
