"""Explicit one-shot DS18B20 power and temperature diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Callable, Sequence, TextIO

from .ds18b20 import (
    Ds18b20Error,
    parse_ds18b20_rom,
)
from .ds2490_transport import Ds2490Error
from .temperature_service import TemperatureAdapter, TemperatureService
from .version import PLUGIN_VERSION


def main(
    argv: Sequence[str] | None = None,
    *,
    adapter_factory: Callable[[], TemperatureAdapter] | None = None,
    output: TextIO = sys.stdout,
    error_output: TextIO = sys.stderr,
) -> int:
    """Run only the explicitly selected operation for one validated ROM."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("temperature-power", "temperature-read"))
    parser.add_argument("--rom", required=True, help="16 hexadecimal wire-order ROM bytes")
    arguments = parser.parse_args(argv)

    try:
        rom = parse_ds18b20_rom(arguments.rom)
        service = (
            TemperatureService(adapter_factory)
            if adapter_factory
            else TemperatureService()
        )
        if arguments.command == "temperature-power":
            power = service.detect_power(rom)
            rendered: dict[str, object] = {
                "status": "ok",
                "version": PLUGIN_VERSION,
                "operation": "temperature-power",
                "rom": rom.hex().upper(),
                "power_mode": power.value,
                "convert_t_issued": False,
                "strong_pullup_enabled": False,
                "automatic_retries": 0,
                "iox_writes": 0,
            }
        else:
            measurement = service.read(rom)
            rendered = {
                "status": "ok",
                "version": PLUGIN_VERSION,
                "operation": "temperature-read",
                "rom": rom.hex().upper(),
                "power_mode": measurement.power_mode.value,
                "resolution_bits": measurement.resolution_bits,
                "scratchpad": measurement.scratchpad.hex().upper(),
                "scratchpad_crc_valid": True,
                "conversion_complete": True,
                "conversion_maximum_seconds": measurement.conversion_maximum_seconds,
                "conversion_polls": measurement.conversion_polls,
                "temperature_celsius": measurement.temperature_celsius,
                "temperature_fahrenheit": measurement.temperature_fahrenheit,
                "strong_pullup_enabled": False,
                "automatic_retries": 0,
                "iox_writes": 0,
            }
    except (Ds18b20Error, Ds2490Error, OSError, ValueError) as exc:
        json.dump(
            {
                "status": "error",
                "version": PLUGIN_VERSION,
                "operation": arguments.command,
                "rom": arguments.rom.upper(),
                "error_type": type(exc).__name__,
                "detail": str(exc),
                "automatic_retries": 0,
                "iox_writes": 0,
            },
            error_output,
            sort_keys=True,
        )
        error_output.write("\n")
        return 1

    json.dump(rendered, output, sort_keys=True)
    output.write("\n")
    return 0
