"""Explicit, one-shot DS2490 Search ROM diagnostic."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Callable, Sequence, TextIO

from .adapter_binding import AdapterBindingError
from .ds2490_transport import (
    MAX_SEARCH_DEVICES,
    Ds2490Adapter,
    Ds2490Error,
    SearchLimitExceeded,
    UsbTransportError,
)
from .onewire_discovery import (
    DiscoveryReport,
    OneWireDevice,
    discover_devices,
)
from .version import PLUGIN_VERSION
from .diagnostic_access import (
    DiagnosticAccessError,
    VerifiedDiagnosticAccess,
    parse_adapter_identity,
)


class Ds2490SearchDiagnostic:
    """Perform one initialization and one bounded Search Access command."""

    def __init__(self, adapter: Ds2490Adapter) -> None:
        self._adapter = adapter

    def run(self) -> DiscoveryReport:
        return discover_devices(self._adapter, max_devices=MAX_SEARCH_DEVICES)


def _device_report(device: OneWireDevice) -> dict[str, object]:
    return {
        "rom": device.rom_address,
        "wire_bytes": [f"{value:02X}" for value in device.rom],
        "family_code": f"0x{device.family_code:02X}",
        "serial_number": device.serial_number,
        "crc8": f"0x{device.crc8:02X}",
        "crc_valid": True,
        "owfs_identifier": device.owfs_identifier,
    }


def diagnostic_report(report: DiscoveryReport) -> dict[str, object]:
    """Render every accepted ROM and every rejected raw response."""

    if report.invalid_responses:
        status = "invalid_rom_responses"
    elif not report.devices:
        status = "empty_bus"
    else:
        status = "ok"
    return {
        "status": status,
        "version": PLUGIN_VERSION,
        "operation": {
            "search_commands": 1,
            "automatic_retries": 0,
            "max_devices": MAX_SEARCH_DEVICES,
            "alternate_setting_changed": False,
            "active_alternate_setting": None,
            "bus_timing": {
                "speed": "flexible",
                "pulldown_slew_rate_code": 4,
                "write1_low_time_code": 4,
                "data_sample_offset_code": 4,
                "verified_before_search": True,
            },
            "temperature_commands": 0,
            "iox_writes": 0,
        },
        "device_count": len(report.devices),
        "devices": [_device_report(device) for device in report.devices],
        "invalid_responses": [
            {"raw": item.raw.hex().upper(), "reason": item.reason}
            for item in report.invalid_responses
        ],
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    diagnostic_factory: Callable[[Ds2490Adapter], Ds2490SearchDiagnostic] = (
        Ds2490SearchDiagnostic
    ),
    access_factory: Callable[[], VerifiedDiagnosticAccess] = (
        VerifiedDiagnosticAccess.native
    ),
    output: TextIO = sys.stdout,
    error_output: TextIO = sys.stderr,
) -> int:
    """Run only when the explicit ``search-rom`` subcommand is supplied."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("search-rom",))
    parser.add_argument(
        "--adapter-rom",
        required=True,
        help="accepted CRC-valid family-0x81 adapter ROM",
    )
    arguments = parser.parse_args(argv)
    if arguments.command != "search-rom":  # defensive; argparse enforces this
        return 2
    try:
        expected = parse_adapter_identity(arguments.adapter_rom)
        report = access_factory().run(
            expected,
            lambda binding, _inventory: diagnostic_factory(
                Ds2490Adapter(binding)
            ).run(),
        )
        rendered = diagnostic_report(report)
        rendered["operation"]["identity_verification_searches"] = 1
        rendered["operation"]["diagnostic_searches"] = 1
        rendered["operation"]["total_searches"] = 2
        rendered["operation"]["adapter_rom"] = expected.hex().upper()
        rendered["operation"]["same_retained_handle"] = True
    except SearchLimitExceeded as exc:
        json.dump(
            {
                "status": "device_limit_exceeded",
                "version": PLUGIN_VERSION,
                "error_type": type(exc).__name__,
                "detail": str(exc),
                "search_attempts": 1,
                "automatic_retries": 0,
                "max_devices": MAX_SEARCH_DEVICES,
            },
            error_output,
            sort_keys=True,
        )
        error_output.write("\n")
        return 1
    except (
        AdapterBindingError,
        DiagnosticAccessError,
        Ds2490Error,
        OSError,
        ValueError,
    ) as exc:
        error = {
            "status": "error",
            "version": PLUGIN_VERSION,
            "error_type": type(exc).__name__,
            "detail": str(exc),
            "search_attempts": 1,
            "automatic_retries": 0,
            "alternate_setting_changed": False,
            "active_alternate_setting": None,
        }
        if isinstance(exc, UsbTransportError) and exc.requested_length is not None:
            error["usb_transfer"] = {
                "endpoint": "0x83",
                "status_reported_bytes": exc.status_reported_length,
                "requested_bytes": exc.requested_length,
                "transferred_bytes": exc.transferred_length,
                "libusb_error_code": exc.code,
            }
        json.dump(
            error,
            error_output,
            sort_keys=True,
        )
        error_output.write("\n")
        return 1
    json.dump(rendered, output, sort_keys=True)
    output.write("\n")
    return 0 if rendered["status"] in {"ok", "empty_bus"} else 1
