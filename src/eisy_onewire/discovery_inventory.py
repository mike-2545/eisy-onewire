"""Validated, persistent inventory for manual 1-Wire discovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .ds2409_transport import BranchHop, Ds2409Branch, branch_path_label
from .onewire_discovery import DiscoveryReport, OneWireDevice, dallas_crc8
from .topology_discovery import EventFlagAcknowledgement, TopologyDiscoveryReport


INVENTORY_KEY = "discovery_inventory"
INVENTORY_SCHEMA_VERSION = 2
ROOT_SEGMENT_SCOPE = "selected-adapter-accessible-segment"
TOPOLOGY_SCOPE = "selected-adapter-ds2409-topology"
DS18B20_FAMILY = 0x28
DS2438_FAMILY = 0x26
DS2409_FAMILY = 0x1F
DS9490_ID_FAMILY = 0x81


class DiscoveryInventoryError(ValueError):
    """Persisted or newly returned inventory data is unsafe to accept."""


@dataclass(frozen=True, slots=True)
class KnownDevice:
    rom: bytes
    first_seen: str
    last_seen: str
    adapter_rom: bytes | None = None
    path: tuple[BranchHop, ...] = ()

    @property
    def family_code(self) -> int:
        return self.rom[0]


@dataclass(frozen=True, slots=True)
class LastScan:
    timestamp: str
    segment_scope: str
    reachable_roms: tuple[str, ...]
    complete: bool
    branches_not_scanned: tuple[str, ...]
    adapter_rom: str | None = None
    scanned_branches: tuple[str, ...] = ()
    event_flags_acknowledged: tuple[EventFlagAcknowledgement, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoveryInventory:
    devices: tuple[KnownDevice, ...] = ()
    last_scan: LastScan | None = None

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "devices": [
                {
                    "rom": item.rom.hex().upper(),
                    "family_code": f"0x{item.family_code:02X}",
                    "first_seen": item.first_seen,
                    "last_seen": item.last_seen,
                    "adapter_rom": (
                        item.adapter_rom.hex().upper()
                        if item.adapter_rom is not None
                        else None
                    ),
                    "path": [
                        {
                            "coupler_rom": hop.coupler_rom.hex().upper(),
                            "branch": hop.branch.value,
                        }
                        for hop in item.path
                    ],
                }
                for item in self.devices
            ],
        }
        if self.last_scan is not None:
            data["last_scan"] = {
                "timestamp": self.last_scan.timestamp,
                "segment_scope": self.last_scan.segment_scope,
                "reachable_roms": list(self.last_scan.reachable_roms),
                "complete": self.last_scan.complete,
                "branches_not_scanned": list(self.last_scan.branches_not_scanned),
                "adapter_rom": self.last_scan.adapter_rom,
                "scanned_branches": list(self.last_scan.scanned_branches),
                "event_flags_acknowledged": [
                    {
                        "coupler_rom": item.coupler_rom.hex().upper(),
                        "raw_status": f"0x{item.raw_status:02X}",
                        "main_event": item.main_event,
                        "aux_event": item.aux_event,
                        "irreversible_loss": True,
                    }
                    for item in self.last_scan.event_flags_acknowledged
                ],
            }
        return data

    @classmethod
    def from_data(cls, raw: object) -> "DiscoveryInventory":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise DiscoveryInventoryError("discovery inventory must be an object")
        schema_version = raw.get("schema_version")
        if schema_version not in (1, INVENTORY_SCHEMA_VERSION):
            raise DiscoveryInventoryError("discovery inventory schema is unsupported")
        device_rows = raw.get("devices")
        if not isinstance(device_rows, list):
            raise DiscoveryInventoryError("discovery inventory devices must be a list")

        devices: list[KnownDevice] = []
        seen: set[bytes] = set()
        for row in device_rows:
            if not isinstance(row, Mapping):
                raise DiscoveryInventoryError("discovery inventory device must be an object")
            rom = _parse_rom(row.get("rom"))
            if rom in seen:
                raise DiscoveryInventoryError("discovery inventory contains duplicate ROMs")
            seen.add(rom)
            if row.get("family_code") != f"0x{rom[0]:02X}":
                raise DiscoveryInventoryError("discovery inventory family code does not match ROM")
            first_seen = _timestamp(row.get("first_seen"), "first_seen")
            last_seen = _timestamp(row.get("last_seen"), "last_seen")
            if _parse_timestamp(first_seen, "first_seen") > _parse_timestamp(
                last_seen, "last_seen"
            ):
                raise DiscoveryInventoryError("device first_seen is after last_seen")
            adapter_rom = None
            path: tuple[BranchHop, ...] = ()
            if schema_version >= 2:
                raw_adapter = row.get("adapter_rom")
                if raw_adapter is not None:
                    adapter_rom = _parse_adapter_rom(raw_adapter)
                path = _parse_path(row.get("path"))
                if path and adapter_rom is None:
                    raise DiscoveryInventoryError(
                        "branched device is missing its adapter identity"
                    )
            devices.append(KnownDevice(rom, first_seen, last_seen, adapter_rom, path))

        last_scan_raw = raw.get("last_scan")
        if (
            schema_version == 1
            and type(last_scan_raw) is dict
            and not last_scan_raw
        ):
            # udi_interface 3.4.9 Custom._deep_update could initialize this
            # nested value but skip populating it after an earlier change.
            # Device rows remain independently validated; scan evidence is lost.
            last_scan_raw = None
        last_scan = None
        if last_scan_raw is not None:
            if not isinstance(last_scan_raw, Mapping):
                raise DiscoveryInventoryError("last_scan must be an object")
            timestamp = _timestamp(last_scan_raw.get("timestamp"), "timestamp")
            segment_scope = _required_text(
                last_scan_raw.get("segment_scope"), "segment_scope"
            )
            if segment_scope not in (ROOT_SEGMENT_SCOPE, TOPOLOGY_SCOPE):
                raise DiscoveryInventoryError("last_scan segment scope is unsupported")
            reachable = _rom_text_list(last_scan_raw.get("reachable_roms"), "reachable_roms")
            complete = last_scan_raw.get("complete")
            if not isinstance(complete, bool):
                raise DiscoveryInventoryError("last_scan complete must be Boolean")

            if segment_scope == ROOT_SEGMENT_SCOPE:
                branches = _rom_text_list(
                    last_scan_raw.get("branches_not_scanned"), "branches_not_scanned"
                )
                if any(bytes.fromhex(item)[0] != DS2409_FAMILY for item in branches):
                    raise DiscoveryInventoryError(
                        "last_scan unscanned branch is not family 0x1F"
                    )
                adapter_text = None
                scanned: tuple[str, ...] = ()
            else:
                branches = _branch_labels(
                    last_scan_raw.get("branches_not_scanned"), "branches_not_scanned"
                )
                raw_adapter = last_scan_raw.get("adapter_rom")
                if raw_adapter is None:
                    raise DiscoveryInventoryError(
                        "topology scan is missing its adapter identity"
                    )
                adapter_text = _parse_adapter_rom(raw_adapter).hex().upper()
                scanned = _branch_labels(
                    last_scan_raw.get("scanned_branches"), "scanned_branches"
                )
                if set(scanned) & set(branches):
                    raise DiscoveryInventoryError(
                        "a DS2409 branch is both scanned and unscanned"
                    )
            acknowledgements = _event_flag_acknowledgements(
                last_scan_raw.get("event_flags_acknowledged", [])
            )
            if segment_scope == ROOT_SEGMENT_SCOPE and acknowledgements:
                raise DiscoveryInventoryError(
                    "root-only scan cannot acknowledge DS2409 event flags"
                )

            if complete == bool(branches):
                raise DiscoveryInventoryError(
                    "last_scan completeness conflicts with unscanned branches"
                )
            known_roms = {item.rom.hex().upper() for item in devices}
            known_root_couplers = {
                item.rom.hex().upper()
                for item in devices
                if item.family_code == DS2409_FAMILY and not item.path
            }
            if not {
                item.coupler_rom.hex().upper() for item in acknowledgements
            }.issubset(known_root_couplers):
                raise DiscoveryInventoryError(
                    "acknowledged DS2409 is absent from known root couplers"
                )
            if not set(reachable).issubset(known_roms):
                raise DiscoveryInventoryError(
                    "last_scan reachable ROM is absent from known devices"
                )
            if segment_scope == ROOT_SEGMENT_SCOPE and not set(branches).issubset(
                set(reachable)
            ):
                raise DiscoveryInventoryError(
                    "last_scan unscanned branch was not reachable"
                )
            last_scan = LastScan(
                timestamp,
                segment_scope,
                reachable,
                complete,
                branches,
                adapter_text,
                scanned,
                acknowledgements,
            )

        return cls(tuple(sorted(devices, key=lambda item: item.rom)), last_scan)


def update_inventory(
    previous: DiscoveryInventory,
    report: DiscoveryReport,
    *,
    timestamp: str,
) -> DiscoveryInventory:
    """Merge one legacy accessible-segment scan for backward compatibility."""

    if report.invalid_responses:
        reasons = "; ".join(item.reason for item in report.invalid_responses)
        raise DiscoveryInventoryError(f"Search ROM returned invalid responses: {reasons}")
    timestamp = _timestamp(timestamp, "timestamp")
    known = {item.rom: item for item in previous.devices}
    for device in report.devices:
        _validate_device(device)
        old = known.get(device.rom)
        known[device.rom] = KnownDevice(
            device.rom,
            old.first_seen if old is not None else timestamp,
            timestamp,
            old.adapter_rom if old is not None else None,
            old.path if old is not None else (),
        )

    reachable = tuple(sorted({device.rom_address for device in report.devices}))
    branches = tuple(
        sorted(
            device.rom_address
            for device in report.devices
            if device.family_code == DS2409_FAMILY
        )
    )
    scan = LastScan(
        timestamp=timestamp,
        segment_scope=ROOT_SEGMENT_SCOPE,
        reachable_roms=reachable,
        complete=not branches,
        branches_not_scanned=branches,
    )
    return DiscoveryInventory(
        tuple(sorted(known.values(), key=lambda item: item.rom)), scan
    )


def update_topology_inventory(
    previous: DiscoveryInventory,
    report: TopologyDiscoveryReport,
    *,
    timestamp: str,
) -> DiscoveryInventory:
    """Merge one safely restored DS2409 topology scan."""

    if report.invalid_responses:
        reasons = "; ".join(item.reason for item in report.invalid_responses)
        raise DiscoveryInventoryError(f"topology returned invalid responses: {reasons}")
    timestamp = _timestamp(timestamp, "timestamp")
    adapter_rom = _parse_adapter_rom(report.adapter_rom.hex().upper())
    known = {item.rom: item for item in previous.devices}
    seen: set[bytes] = set()
    for located in report.devices:
        device = located.device
        _validate_device(device)
        if located.adapter_rom != adapter_rom:
            raise DiscoveryInventoryError("device adapter identity conflicts with scan")
        if device.rom in seen:
            raise DiscoveryInventoryError("topology contains duplicate device ROMs")
        seen.add(device.rom)
        old = known.get(device.rom)
        known[device.rom] = KnownDevice(
            device.rom,
            old.first_seen if old is not None else timestamp,
            timestamp,
            adapter_rom,
            located.path,
        )

    scanned = tuple(branch_path_label(path) for path in report.scanned_branches)
    unscanned = tuple(branch_path_label(path) for path in report.branches_not_scanned)
    if set(scanned) & set(unscanned):
        raise DiscoveryInventoryError("a DS2409 branch is both scanned and unscanned")
    scan = LastScan(
        timestamp=timestamp,
        segment_scope=TOPOLOGY_SCOPE,
        reachable_roms=tuple(sorted(item.device.rom_address for item in report.devices)),
        complete=not unscanned,
        branches_not_scanned=tuple(sorted(unscanned)),
        adapter_rom=adapter_rom.hex().upper(),
        scanned_branches=tuple(sorted(scanned)),
        event_flags_acknowledged=report.event_flags_acknowledged,
    )
    return DiscoveryInventory(
        tuple(sorted(known.values(), key=lambda item: item.rom)), scan
    )


def default_sensor_name(rom: bytes) -> str:
    """Return a unique, readable name without making configuration mandatory."""

    parsed = _parse_rom(rom.hex().upper())
    if parsed[0] != DS18B20_FAMILY:
        raise DiscoveryInventoryError("default temperature name requires family 0x28")
    return f"DS18B20 {parsed[0]:02X}.{parsed[1:7].hex().upper()}"


def _validate_device(device: OneWireDevice) -> None:
    if len(device.rom) != 8 or dallas_crc8(device.rom) != 0:
        raise DiscoveryInventoryError("discovered ROM failed length or CRC validation")


def _parse_rom(value: object) -> bytes:
    if not isinstance(value, str) or len(value) != 16:
        raise DiscoveryInventoryError("inventory ROM must be 16 hexadecimal characters")
    try:
        rom = bytes.fromhex(value)
    except ValueError as exc:
        raise DiscoveryInventoryError("inventory ROM is not hexadecimal") from exc
    if len(rom) != 8 or dallas_crc8(rom) != 0:
        raise DiscoveryInventoryError("inventory ROM CRC-8 is invalid")
    return rom


def _parse_adapter_rom(value: object) -> bytes:
    rom = _parse_rom(value)
    if rom[0] != DS9490_ID_FAMILY:
        raise DiscoveryInventoryError("adapter identity must use family 0x81")
    return rom


def _parse_path(value: object) -> tuple[BranchHop, ...]:
    if not isinstance(value, list):
        raise DiscoveryInventoryError("device path must be a list")
    path: list[BranchHop] = []
    for raw_hop in value:
        if not isinstance(raw_hop, Mapping):
            raise DiscoveryInventoryError("device path hop must be an object")
        coupler = _parse_rom(raw_hop.get("coupler_rom"))
        if coupler[0] != DS2409_FAMILY:
            raise DiscoveryInventoryError("device path coupler must use family 0x1F")
        try:
            branch = Ds2409Branch(raw_hop.get("branch"))
        except (TypeError, ValueError) as exc:
            raise DiscoveryInventoryError(
                "device path branch must be main or aux"
            ) from exc
        path.append(BranchHop(coupler, branch))
    return tuple(path)


def _branch_labels(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise DiscoveryInventoryError(f"{field} must be a list")
    parsed = tuple(sorted(_validate_branch_label(item) for item in value))
    if len(parsed) != len(set(parsed)):
        raise DiscoveryInventoryError(f"{field} contains duplicate paths")
    return parsed


def _validate_branch_label(value: object) -> str:
    rendered = _required_text(value, "branch path")
    for part in rendered.split(">"):
        try:
            rom_text, branch_text = part.split("/", 1)
        except ValueError as exc:
            raise DiscoveryInventoryError("branch path has invalid syntax") from exc
        rom = _parse_rom(rom_text)
        if rom[0] != DS2409_FAMILY:
            raise DiscoveryInventoryError("branch path coupler must use family 0x1F")
        try:
            Ds2409Branch(branch_text)
        except ValueError as exc:
            raise DiscoveryInventoryError("branch path must select main or aux") from exc
    return rendered


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DiscoveryInventoryError(f"{field} must be non-empty text")
    return value


def _timestamp(value: object, field: str) -> str:
    rendered = _required_text(value, field)
    _parse_timestamp(rendered, field)
    return rendered


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DiscoveryInventoryError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise DiscoveryInventoryError(f"{field} timestamp must include a timezone")
    return parsed


def _rom_text_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise DiscoveryInventoryError(f"{field} must be a list")
    parsed = tuple(sorted(_parse_rom(item).hex().upper() for item in value))
    if len(parsed) != len(set(parsed)):
        raise DiscoveryInventoryError(f"{field} contains duplicate ROMs")
    return parsed


def _event_flag_acknowledgements(
    value: object,
) -> tuple[EventFlagAcknowledgement, ...]:
    if not isinstance(value, list):
        raise DiscoveryInventoryError(
            "event_flags_acknowledged must be a list"
        )
    result: list[EventFlagAcknowledgement] = []
    seen: set[bytes] = set()
    for row in value:
        if not isinstance(row, Mapping):
            raise DiscoveryInventoryError(
                "event flag acknowledgement must be an object"
            )
        coupler = _parse_rom(row.get("coupler_rom"))
        if coupler[0] != DS2409_FAMILY:
            raise DiscoveryInventoryError(
                "event flag acknowledgement must identify a DS2409"
            )
        if coupler in seen:
            raise DiscoveryInventoryError(
                "event flag acknowledgements contain a duplicate DS2409"
            )
        seen.add(coupler)
        raw_text = row.get("raw_status")
        if (
            not isinstance(raw_text, str)
            or len(raw_text) != 4
            or not raw_text.startswith("0x")
        ):
            raise DiscoveryInventoryError(
                "acknowledged DS2409 raw status must use 0xNN syntax"
            )
        try:
            raw = int(raw_text[2:], 16)
        except ValueError as exc:
            raise DiscoveryInventoryError(
                "acknowledged DS2409 raw status is not hexadecimal"
            ) from exc
        main_event = row.get("main_event")
        aux_event = row.get("aux_event")
        if not isinstance(main_event, bool) or not isinstance(aux_event, bool):
            raise DiscoveryInventoryError(
                "acknowledged DS2409 event flags must be Boolean"
            )
        if not main_event and not aux_event:
            raise DiscoveryInventoryError(
                "event flag acknowledgement contains no recorded event"
            )
        if bool(raw & 0x10) is not main_event or bool(raw & 0x20) is not aux_event:
            raise DiscoveryInventoryError(
                "acknowledged DS2409 event flags conflict with raw status"
            )
        if not bool(raw & 0x01) and not bool(raw & 0x04):
            raise DiscoveryInventoryError(
                "acknowledged DS2409 raw status activates both branches"
            )
        if row.get("irreversible_loss") is not True:
            raise DiscoveryInventoryError(
                "event flag acknowledgement must record irreversible loss"
            )
        result.append(
            EventFlagAcknowledgement(coupler, raw, main_event, aux_event)
        )
    return tuple(sorted(result, key=lambda item: item.coupler_rom))
