#!/usr/bin/env python3
"""Explicit command-line entry point for reviewed eisy-onewire diagnostics."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from eisy_onewire.search_diagnostic import main as search_main  # noqa: E402
from eisy_onewire.status_diagnostic import main as status_main  # noqa: E402
from eisy_onewire.temperature_diagnostic import main as temperature_main  # noqa: E402
from eisy_onewire.ds2438_diagnostic import main as ds2438_main  # noqa: E402
from eisy_onewire.adapter_identity import main as adapter_identity_main  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch only an explicitly selected reviewed diagnostic."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "adapter-status",
            "search-rom",
            "temperature-power",
            "temperature-read",
            "ds2438-read",
            "adapter-enumerate",
            "adapter-identify",
        ),
    )
    parser.add_argument("--rom")
    parser.add_argument("--adapter-rom")
    parser.add_argument("--coupler-rom")
    parser.add_argument("--branch", choices=("main", "aux"))
    parser.add_argument("--acknowledge-event-clear", action="store_true")
    parser.add_argument("--bus-number", type=int)
    parser.add_argument("--device-address", type=int)
    parser.add_argument("--port-path")
    arguments = parser.parse_args(argv)
    if arguments.command == "adapter-status":
        if arguments.adapter_rom is None:
            parser.error("adapter-status requires --adapter-rom")
        return status_main(
            [arguments.command, "--adapter-rom", arguments.adapter_rom]
        )
    if arguments.command == "adapter-enumerate":
        return adapter_identity_main([arguments.command])
    if arguments.command == "adapter-identify":
        if arguments.bus_number is None or arguments.device_address is None:
            parser.error("adapter-identify requires --bus-number and --device-address")
        forwarded = [
            arguments.command,
            "--bus-number", str(arguments.bus_number),
            "--device-address", str(arguments.device_address),
        ]
        if arguments.port_path:
            forwarded.extend(("--port-path", arguments.port_path))
        return adapter_identity_main(forwarded)
    if arguments.command == "search-rom":
        if arguments.adapter_rom is None:
            parser.error("search-rom requires --adapter-rom")
        return search_main(
            [arguments.command, "--adapter-rom", arguments.adapter_rom]
        )
    if arguments.command == "ds2438-read":
        required = (arguments.rom, arguments.adapter_rom, arguments.coupler_rom, arguments.branch)
        if any(value is None for value in required):
            parser.error(
                "ds2438-read requires --rom, --adapter-rom, --coupler-rom, and --branch"
            )
        forwarded = [
            arguments.command,
            "--rom", arguments.rom,
            "--adapter-rom", arguments.adapter_rom,
            "--coupler-rom", arguments.coupler_rom,
            "--branch", arguments.branch,
        ]
        if arguments.acknowledge_event_clear:
            forwarded.append("--acknowledge-event-clear")
        return ds2438_main(forwarded)
    if arguments.rom is None or arguments.adapter_rom is None:
        parser.error(f"{arguments.command} requires --rom and --adapter-rom")
    return temperature_main(
        [
            arguments.command,
            "--rom",
            arguments.rom,
            "--adapter-rom",
            arguments.adapter_rom,
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
