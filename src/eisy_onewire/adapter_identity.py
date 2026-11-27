"""Explicit DS9490 inventory and selected-adapter identity diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Callable, Sequence, TextIO

from .adapter_exclusion import AdapterAccessLock
from .ds2490_transport import CtypesLibUsbSessionFactory, Ds2490Adapter, Ds2490Error
from .onewire_discovery import DiscoveryReport, discover_devices
from .usb_detector import (
    UsbDeviceDescriptor,
    UsbDeviceSelector,
    UsbEnumerationResult,
    enumerate_ds9490,
)
from .version import PLUGIN_VERSION


def _location(descriptor: UsbDeviceDescriptor) -> dict[str, object]:
    return {
        "bus_number": descriptor.bus_number,
        "device_address": descriptor.device_address,
        "port_path": list(descriptor.port_path) if descriptor.port_path else None,
        "vendor_id": f"{descriptor.vendor_id:04x}",
        "product_id": f"{descriptor.product_id:04x}",
    }


def enumeration_report(result: UsbEnumerationResult) -> dict[str, object]:
    """Render a descriptor-only inventory; this operation opens no adapter."""

    return {
        "status": "ok" if result.error_code is None else "error",
        "version": PLUGIN_VERSION,
        "matching_adapter_count": result.matching_adapter_count,
        "adapters": [_location(item) for item in result.descriptors],
        "error_code": result.error_code,
        "detail": result.detail,
        "usb_operations": {
            "handles_opened": 0,
            "interfaces_claimed": 0,
            "transfers": 0,
        },
    }


@dataclass(slots=True)
class AdapterIdentityDiagnostic:
    """Identify one deliberately selected adapter with one bounded root search."""

    selector: UsbDeviceSelector
    lock: AdapterAccessLock
    adapter_factory: Callable[[UsbDeviceSelector], tuple[Ds2490Adapter, CtypesLibUsbSessionFactory]]

    @classmethod
    def native(cls, selector: UsbDeviceSelector) -> "AdapterIdentityDiagnostic":
        def create(
            selected: UsbDeviceSelector,
        ) -> tuple[Ds2490Adapter, CtypesLibUsbSessionFactory]:
            factory = CtypesLibUsbSessionFactory.for_search_diagnostic(
                selector=selected
            )
            return Ds2490Adapter(factory), factory

        return cls(selector, AdapterAccessLock(), create)

    def run(self) -> dict[str, object]:
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("the DS9490 adapter is busy")
        try:
            adapter, factory = self.adapter_factory(self.selector)
            report: DiscoveryReport = discover_devices(adapter, max_devices=15)
            if report.invalid_responses:
                raise RuntimeError("selected-adapter identity search returned invalid ROM data")
            identities = [item for item in report.devices if item.family_code == 0x81]
            if len(identities) != 1:
                raise RuntimeError(
                    "selected adapter did not return exactly one CRC-valid family-0x81 ROM"
                )
            descriptor = factory.selected_descriptor
            return {
                "status": "ok",
                "version": PLUGIN_VERSION,
                "selected_location": (
                    _location(descriptor) if descriptor is not None else {
                        "bus_number": self.selector.bus_number,
                        "device_address": self.selector.device_address,
                        "port_path": (
                            list(self.selector.port_path)
                            if self.selector.port_path else None
                        ),
                    }
                ),
                "adapter_identity_rom": identities[0].rom_address,
                "family_code": "0x81",
                "root_device_count": len(report.devices),
                "operation": {
                    "search_commands": 1,
                    "branch_commands": 0,
                    "automatic_retries": 0,
                    "iox_writes": 0,
                },
            }
        finally:
            self.lock.release()


def main(
    argv: Sequence[str] | None = None,
    *,
    enumerator: Callable[[], UsbEnumerationResult] = enumerate_ds9490,
    identity_factory: Callable[[UsbDeviceSelector], AdapterIdentityDiagnostic] = AdapterIdentityDiagnostic.native,
    output: TextIO = sys.stdout,
    error_output: TextIO = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("adapter-enumerate")
    identify = subparsers.add_parser("adapter-identify")
    identify.add_argument("--bus-number", required=True, type=int)
    identify.add_argument("--device-address", required=True, type=int)
    identify.add_argument("--port-path")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "adapter-enumerate":
            rendered = enumeration_report(enumerator())
        else:
            path = None
            if arguments.port_path:
                path = tuple(int(item) for item in arguments.port_path.split("."))
            selector = UsbDeviceSelector(
                arguments.bus_number, arguments.device_address, path
            )
            rendered = identity_factory(selector).run()
    except (Ds2490Error, OSError, RuntimeError, ValueError) as exc:
        json.dump(
            {
                "status": "error",
                "version": PLUGIN_VERSION,
                "error_type": type(exc).__name__,
                "detail": str(exc),
                "automatic_retries": 0,
            },
            error_output,
            sort_keys=True,
        )
        error_output.write("\n")
        return 1
    json.dump(rendered, output, sort_keys=True)
    output.write("\n")
    return 0 if rendered["status"] == "ok" else 1
