"""Explicit, one-shot DS2490 EP1 status diagnostic.

The diagnostic deliberately issues no control or bulk transfers. It requires
USB configuration 1 to be active, preserves the current alternate setting,
claims interface 0, reads EP1 once, and releases all native resources.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from typing import Callable, Sequence, TextIO

from .ds2490_transport import (
    STATUS_DATA_IN_COUNT_OFFSET,
    STATUS_FLAGS_OFFSET,
    STATUS_MINIMUM_SIZE,
    STATUS_SIZE,
    CtypesLibUsbSessionFactory,
    Ds2490Error,
    Ds2490ProtocolError,
    UsbSessionFactory,
)
from .version import PLUGIN_VERSION

COMMAND_BUFFER_COUNT_OFFSET = 11
DATA_OUT_COUNT_OFFSET = 12
MAX_COMMAND_BUFFER_COUNT = 16
MAX_DATA_BUFFER_COUNT = 128
ENABLE_FLAGS_ALLOWED = 0x05
STATUS_FLAGS_ALLOWED = 0xBF
MAX_SPEED_CODE = 0x02


@dataclass(frozen=True, slots=True)
class AdapterStatus:
    """Validated fields from one DS2490 status interrupt transfer."""

    transfer_length: int
    enable_flags: int
    speed_code: int
    strong_pullup_duration: int
    pulldown_slew_rate: int
    write_one_low_time: int
    data_sample_offset: int
    status_flags: int
    command_byte_1: int
    command_byte_2: int
    command_buffer_count: int
    data_out_buffer_count: int
    data_in_buffer_count: int
    result_bytes: tuple[int, ...]


class Ds2490StatusDiagnostic:
    """Perform exactly one non-command EP1 read in a claimed session."""

    def __init__(self, factory: UsbSessionFactory | None = None) -> None:
        self._factory = factory or CtypesLibUsbSessionFactory.for_status_diagnostic()

    def run(self) -> AdapterStatus:
        with self._factory.open() as session:
            raw = session.interrupt_in(STATUS_SIZE)
            return parse_adapter_status(raw)


def parse_adapter_status(raw: bytes) -> AdapterStatus:
    """Validate the fixed 16-byte state area and optional result bytes."""

    if not STATUS_MINIMUM_SIZE <= len(raw) <= STATUS_SIZE:
        raise Ds2490ProtocolError(
            f"DS2490 status length {len(raw)} is outside the documented 16..32 bytes"
        )
    if raw[0] & ~ENABLE_FLAGS_ALLOWED:
        raise Ds2490ProtocolError(
            f"DS2490 enable flags contain reserved bits: 0x{raw[0]:02x}"
        )
    if raw[1] > MAX_SPEED_CODE:
        raise Ds2490ProtocolError(
            f"DS2490 speed code is undefined: 0x{raw[1]:02x}"
        )
    if raw[STATUS_FLAGS_OFFSET] & ~STATUS_FLAGS_ALLOWED:
        raise Ds2490ProtocolError(
            "DS2490 device status contains a reserved flag: "
            f"0x{raw[STATUS_FLAGS_OFFSET]:02x}"
        )
    if raw[COMMAND_BUFFER_COUNT_OFFSET] > MAX_COMMAND_BUFFER_COUNT:
        raise Ds2490ProtocolError("DS2490 command buffer count exceeds 16 bytes")
    if raw[DATA_OUT_COUNT_OFFSET] > MAX_DATA_BUFFER_COUNT:
        raise Ds2490ProtocolError("DS2490 data-out buffer count exceeds 128 bytes")
    if raw[STATUS_DATA_IN_COUNT_OFFSET] > MAX_DATA_BUFFER_COUNT:
        raise Ds2490ProtocolError("DS2490 data-in buffer count exceeds 128 bytes")

    return AdapterStatus(
        transfer_length=len(raw),
        enable_flags=raw[0],
        speed_code=raw[1],
        strong_pullup_duration=raw[2],
        pulldown_slew_rate=raw[4],
        write_one_low_time=raw[5],
        data_sample_offset=raw[6],
        status_flags=raw[STATUS_FLAGS_OFFSET],
        command_byte_1=raw[9],
        command_byte_2=raw[10],
        command_buffer_count=raw[COMMAND_BUFFER_COUNT_OFFSET],
        data_out_buffer_count=raw[DATA_OUT_COUNT_OFFSET],
        data_in_buffer_count=raw[STATUS_DATA_IN_COUNT_OFFSET],
        result_bytes=tuple(raw[16:]),
    )


def diagnostic_report(status: AdapterStatus) -> dict[str, object]:
    """Create a stable, auditable report without unrelated USB information."""

    return {
        "status": "ok",
        "version": PLUGIN_VERSION,
        "usb": {
            "configuration": 1,
            "interface": 0,
            "endpoint": "0x81",
            "configuration_changed": False,
            "alternate_setting_changed": False,
            "control_transfers": 0,
            "bulk_transfers": 0,
            "interrupt_reads": 1,
        },
        "adapter_status": asdict(status),
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    diagnostic_factory: Callable[[], Ds2490StatusDiagnostic] = Ds2490StatusDiagnostic,
    output: TextIO = sys.stdout,
    error_output: TextIO = sys.stderr,
) -> int:
    """Run only when the explicit ``adapter-status`` subcommand is supplied."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("adapter-status",))
    arguments = parser.parse_args(argv)
    if arguments.command != "adapter-status":  # defensive; argparse enforces this
        return 2
    try:
        report = diagnostic_report(diagnostic_factory().run())
    except (Ds2490Error, OSError) as exc:
        json.dump(
            {
                "status": "error",
                "version": PLUGIN_VERSION,
                "error_type": type(exc).__name__,
                "detail": str(exc),
            },
            error_output,
            sort_keys=True,
        )
        error_output.write("\n")
        return 1
    json.dump(report, output, sort_keys=True)
    output.write("\n")
    return 0
