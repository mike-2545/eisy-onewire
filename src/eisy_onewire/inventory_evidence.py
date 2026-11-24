"""Atomic, plugin-generated mirror of validated PG3 discovery customdata."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from .discovery_inventory import (
    DS2409_FAMILY,
    DS2438_FAMILY,
    DS9490_ID_FAMILY,
    TOPOLOGY_SCOPE,
    DiscoveryInventory,
    DiscoveryInventoryError,
)
from .ds2409_transport import BranchHop, Ds2409Branch, branch_path_label


EVIDENCE_FILENAME = ".eisy-onewire-discovery-inventory.json"
MAXIMUM_EVIDENCE_BYTES = 256 * 1024


class InventoryEvidenceError(ValueError):
    """The diagnostic target is not proven by persisted discovery evidence."""


def installed_evidence_path() -> Path:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path(tempfile.gettempdir()) / f"{EVIDENCE_FILENAME}.{uid}"


def persist_inventory_evidence(path: Path, inventory: DiscoveryInventory) -> None:
    payload = json.dumps(inventory.to_data(), sort_keys=True, separators=(",", ":"))
    _atomic_write_text(path, payload + "\n")


def _atomic_write_text(path: Path, payload: str) -> None:
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def invalidate_inventory_evidence(path: Path, detail: str) -> None:
    payload = {"status": "unavailable", "detail": detail}
    _atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n")


def load_inventory_evidence(path: Path) -> DiscoveryInventory:
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise InventoryEvidenceError("discovery evidence is not a regular file")
        if hasattr(os, "getuid") and details.st_uid != os.getuid():
            raise InventoryEvidenceError(
                "discovery evidence is not owned by this plugin account"
            )
        if os.name == "posix" and details.st_mode & 0o077:
            raise InventoryEvidenceError("discovery evidence permissions are too broad")
        if details.st_size > MAXIMUM_EVIDENCE_BYTES:
            raise InventoryEvidenceError("discovery evidence is oversized")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            raw = json.load(stream)
        return DiscoveryInventory.from_data(raw)
    except (OSError, json.JSONDecodeError, DiscoveryInventoryError) as exc:
        raise InventoryEvidenceError(f"discovery evidence is unavailable: {exc}") from exc
    finally:
        if "descriptor" in locals() and descriptor >= 0:
            os.close(descriptor)


def validate_ds2438_target(
    inventory: DiscoveryInventory,
    *,
    sensor_rom: bytes,
    adapter_rom: bytes,
    coupler_rom: bytes,
    branch: Ds2409Branch,
) -> tuple[BranchHop, str]:
    """Require an exact, fresh adapter/coupler/path/sensor topology tuple."""

    scan = inventory.last_scan
    if scan is None or scan.segment_scope != TOPOLOGY_SCOPE or not scan.complete:
        raise InventoryEvidenceError("a complete persisted topology scan is required")
    if scan.adapter_rom != adapter_rom.hex().upper():
        raise InventoryEvidenceError("requested adapter differs from persisted topology")
    hop = BranchHop(coupler_rom, branch)
    label = branch_path_label((hop,))
    if label not in scan.scanned_branches:
        raise InventoryEvidenceError("requested DS2409 branch was not scanned")

    matching = [item for item in inventory.devices if item.rom == sensor_rom]
    if len(matching) != 1 or matching[0].family_code != DS2438_FAMILY:
        raise InventoryEvidenceError("requested DS2438 is absent or ambiguous")
    sensor = matching[0]
    if sensor.adapter_rom != adapter_rom or sensor.path != (hop,):
        raise InventoryEvidenceError("requested DS2438 topology path does not match")

    adapters = [
        item
        for item in inventory.devices
        if item.rom == adapter_rom
        and item.family_code == DS9490_ID_FAMILY
        and not item.path
    ]
    couplers = [
        item
        for item in inventory.devices
        if item.rom == coupler_rom
        and item.family_code == DS2409_FAMILY
        and not item.path
    ]
    if len(adapters) != 1 or len(couplers) != 1:
        raise InventoryEvidenceError("adapter or root coupler identity is not unique")
    current_roms = set(scan.reachable_roms)
    required = {
        sensor_rom.hex().upper(),
        adapter_rom.hex().upper(),
        coupler_rom.hex().upper(),
    }
    if not required.issubset(current_roms):
        raise InventoryEvidenceError("requested topology was not reachable in the last scan")
    if any(item.last_seen != scan.timestamp for item in (sensor, adapters[0], couplers[0])):
        raise InventoryEvidenceError("requested topology evidence is stale")
    return hop, scan.timestamp
