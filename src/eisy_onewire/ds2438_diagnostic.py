"""Explicit one-shot DS2438 diagnostic through one validated DS2409 path."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .adapter_exclusion import AdapterAccessLock
from .branch_temperature import BranchEventPolicyError, BranchRestorationError
from .discovery_inventory import DiscoveryInventory
from .ds2409_transport import BranchHop, Ds2409Branch, Ds2409State, Ds2409Transport
from .ds2438 import Ds2438Measurement, parse_ds2438_rom
from .ds2438_transport import Ds2438Transport
from .ds2490_transport import CtypesLibUsbSessionFactory, Ds2490Transport, UsbSessionFactory
from .inventory_evidence import (
    installed_evidence_path,
    load_inventory_evidence,
    validate_ds2438_target,
)
from .manual_discovery import AdapterBusyError
from .onewire_discovery import dallas_crc8
from .version import PLUGIN_VERSION


@dataclass(frozen=True, slots=True)
class Ds2438BranchTarget:
    rom: bytes
    adapter_rom: bytes
    path: tuple[BranchHop, ...]
    evidence_timestamp: str

    def __post_init__(self) -> None:
        parse_ds2438_rom(self.rom.hex())
        if (
            len(self.adapter_rom) != 8
            or self.adapter_rom[0] != 0x81
            or dallas_crc8(self.adapter_rom) != 0
        ):
            raise ValueError("adapter ROM must be CRC-valid family 0x81")
        if len(self.path) != 1:
            raise ValueError("DS2438 diagnostic supports exactly one DS2409 hop")
        if not isinstance(self.evidence_timestamp, str) or not self.evidence_timestamp:
            raise ValueError("DS2438 diagnostic requires persisted topology evidence")


@dataclass(frozen=True, slots=True)
class Ds2438RequestedTarget:
    rom: bytes
    adapter_rom: bytes
    coupler_rom: bytes
    branch: Ds2409Branch

    def __post_init__(self) -> None:
        parse_ds2438_rom(self.rom.hex())
        BranchHop(self.coupler_rom, self.branch)
        if (
            len(self.adapter_rom) != 8
            or self.adapter_rom[0] != 0x81
            or dallas_crc8(self.adapter_rom) != 0
        ):
            raise ValueError("adapter ROM must be CRC-valid family 0x81")


@dataclass(frozen=True, slots=True)
class Ds2438DiagnosticResult:
    measurement: Ds2438Measurement
    original_state: Ds2409State
    restored_state: Ds2409State
    event_evidence: tuple[dict[str, object], ...]
    event_flags_cleared: bool
    evidence_timestamp: str


class Ds2438Reader(Protocol):
    def read_measurements(self, rom: bytes) -> Ds2438Measurement: ...


class CouplerControl(Protocol):
    def read_status(self, coupler_rom: bytes): ...

    def select_branch(self, coupler_rom: bytes, branch: Ds2409Branch) -> int: ...

    def restore_state(self, coupler_rom: bytes, state: Ds2409State) -> None: ...


class Ds2438BranchDiagnostic:
    def __init__(
        self,
        factory: UsbSessionFactory | None = None,
        *,
        adapter_lock: AdapterAccessLock | None = None,
        evidence_path: Path | None = None,
        inventory_loader: Callable[[Path], DiscoveryInventory] = load_inventory_evidence,
        target_validator: Callable[..., tuple[BranchHop, str]] = validate_ds2438_target,
    ) -> None:
        self._factory = factory
        self._adapter_lock = adapter_lock or AdapterAccessLock()
        self._evidence_path = evidence_path or installed_evidence_path()
        self._inventory_loader = inventory_loader
        self._target_validator = target_validator

    def read(
        self, request: Ds2438RequestedTarget, *, allow_event_clear: bool = False
    ) -> Ds2438DiagnosticResult:
        if not self._adapter_lock.acquire(blocking=False):
            raise AdapterBusyError(
                "DS9490 is busy in the running plugin or another diagnostic process"
            )
        try:
            inventory = self._inventory_loader(self._evidence_path)
            hop, evidence_timestamp = self._target_validator(
                inventory,
                sensor_rom=request.rom,
                adapter_rom=request.adapter_rom,
                coupler_rom=request.coupler_rom,
                branch=request.branch,
            )
            target = Ds2438BranchTarget(
                request.rom,
                request.adapter_rom,
                (hop,),
                evidence_timestamp,
            )
            factory = self._factory or CtypesLibUsbSessionFactory.for_search_diagnostic()
            with factory.open() as session:
                Ds2490Transport(session).initialize()
                coupler = Ds2409Transport(session)
                result = read_branch_ds2438(
                    target,
                    coupler,
                    Ds2438Transport(session),
                    allow_event_clear=allow_event_clear,
                )
                return result
        finally:
            self._adapter_lock.release()


def read_branch_ds2438(
    target: Ds2438BranchTarget,
    coupler: CouplerControl,
    reader: Ds2438Reader,
    *,
    allow_event_clear: bool = False,
) -> Ds2438DiagnosticResult:
    """Run one branch transaction, then restore and verify before accepting it."""

    hop = target.path[0]
    original = coupler.read_status(hop.coupler_rom)
    evidence = [_status("original", original)]
    _reject_line_fault(original)
    if original.state is Ds2409State.OFF and not allow_event_clear:
        raise BranchEventPolicyError(
            "originally-off restoration requires explicit event-latch acknowledgement"
        )

    operation_error: Exception | None = None
    measurement: Ds2438Measurement | None = None
    try:
        if original.state.value != hop.branch.value:
            coupler.select_branch(hop.coupler_rom, hop.branch)
            selected = coupler.read_status(hop.coupler_rom)
            evidence.append(_status("selected", selected))
            if selected.state.value != hop.branch.value:
                raise RuntimeError("DS2409 did not confirm selected branch")
        measurement = reader.read_measurements(target.rom)
    except Exception as exc:
        operation_error = exc

    try:
        before = coupler.read_status(hop.coupler_rom)
        evidence.append(_status("before_restore", before))
        coupler.restore_state(hop.coupler_rom, original.state)
        final = coupler.read_status(hop.coupler_rom)
        evidence.append(_status("restored", final))
        if final.state is not original.state:
            raise RuntimeError("restoration readback did not match original state")
        if original.state is not Ds2409State.OFF and (
            (original.main_event and not final.main_event)
            or (original.aux_event and not final.aux_event)
        ):
            raise RuntimeError("event latch was unexpectedly cleared")
    except Exception as exc:
        raise BranchRestorationError(f"DS2409 restoration failed: {exc}") from exc
    if operation_error is not None:
        raise operation_error
    assert measurement is not None
    flags_cleared = original.state is Ds2409State.OFF and any(
        bool(item["main_event"] or item["aux_event"]) for item in evidence[:-1]
    )
    return Ds2438DiagnosticResult(
        measurement,
        original.state,
        final.state,
        tuple(evidence),
        flags_cleared,
        target.evidence_timestamp,
    )


def _status(stage, status) -> dict[str, object]:
    return {
        "stage": stage,
        "raw_status": f"0x{status.raw:02X}",
        "state": status.state.value,
        "main_event": status.main_event,
        "aux_event": status.aux_event,
    }


def _reject_line_fault(status) -> None:
    if (
        (status.main_level_valid and not status.main_level_high)
        or (status.aux_level_valid and not status.aux_level_high)
    ):
        raise RuntimeError("DS2409 inactive branch sensed low")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("ds2438-read",))
    parser.add_argument("--rom", required=True)
    parser.add_argument("--adapter-rom", required=True)
    parser.add_argument("--coupler-rom", required=True)
    parser.add_argument("--branch", choices=("main", "aux"), required=True)
    parser.add_argument("--acknowledge-event-clear", action="store_true")
    args = parser.parse_args(argv)
    try:
        request = Ds2438RequestedTarget(
            parse_ds2438_rom(args.rom),
            bytes.fromhex(args.adapter_rom),
            bytes.fromhex(args.coupler_rom),
            Ds2409Branch(args.branch),
        )
        result = Ds2438BranchDiagnostic().read(
            request, allow_event_clear=args.acknowledge_event_clear
        )
        measurement = result.measurement
        report = {
            "status": "ok",
            "version": PLUGIN_VERSION,
            "rom": measurement.rom.hex().upper(),
            "temperature_celsius": measurement.temperature_celsius,
            "temperature_fahrenheit": measurement.temperature_fahrenheit,
            "vad_volts": measurement.vad_volts,
            "vdd_volts": measurement.vdd_volts,
            "humidity_percent": None,
            "humidity_status": "unsupported_model_unidentified",
            "original_state": result.original_state.value,
            "restored_state": result.restored_state.value,
            "restoration_verified": True,
            "topology_evidence_timestamp": result.evidence_timestamp,
            "event_flags_cleared": result.event_flags_cleared,
            "event_evidence": result.event_evidence,
            "automatic_retries": 0,
            "eeprom_writes": 0,
        }
        print(json.dumps(report, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "version": PLUGIN_VERSION,
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                    "automatic_retries": 0,
                    "eeprom_writes": 0,
                },
                sort_keys=True,
            )
        )
        return 1
