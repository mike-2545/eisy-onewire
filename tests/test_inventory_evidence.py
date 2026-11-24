from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path

from eisy_onewire.discovery_inventory import DiscoveryInventory
from eisy_onewire.ds2409_transport import Ds2409Branch
from eisy_onewire.inventory_evidence import (
    InventoryEvidenceError,
    invalidate_inventory_evidence,
    load_inventory_evidence,
    persist_inventory_evidence,
    validate_ds2438_target,
)


SENSOR = bytes.fromhex("2666778899AABB54")
ADAPTER = bytes.fromhex("814455667788999C")
COUPLER = bytes.fromhex("1F5566778899AA38")
STAMP = "2026-11-22T12:00:00+00:00"
FIRST = "2026-11-20T12:00:00+00:00"


def evidence(*, sensor_adapter: bytes = ADAPTER, sensor_last_seen: str = STAMP):
    rows = [
        (ADAPTER, None, []),
        (COUPLER, None, []),
        (
            SENSOR,
            sensor_adapter.hex().upper(),
            [{"coupler_rom": COUPLER.hex().upper(), "branch": "main"}],
        ),
    ]
    return DiscoveryInventory.from_data(
        {
            "schema_version": 2,
            "devices": [
                {
                    "rom": rom.hex().upper(),
                    "family_code": f"0x{rom[0]:02X}",
                    "first_seen": FIRST,
                    "last_seen": sensor_last_seen if rom == SENSOR else STAMP,
                    "adapter_rom": adapter,
                    "path": path,
                }
                for rom, adapter, path in rows
            ],
            "last_scan": {
                "timestamp": STAMP,
                "segment_scope": "selected-adapter-ds2409-topology",
                "reachable_roms": [item.hex().upper() for item, _, _ in rows],
                "complete": True,
                "branches_not_scanned": [],
                "adapter_rom": ADAPTER.hex().upper(),
                "scanned_branches": [f"{COUPLER.hex().upper()}/main"],
                "event_flags_acknowledged": [],
            },
        }
    )


class InventoryEvidenceTests(unittest.TestCase):
    def test_atomic_snapshot_round_trips_and_exact_tuple_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.json"
            persist_inventory_evidence(path, evidence())
            loaded = load_inventory_evidence(path)
            hop, timestamp = validate_ds2438_target(
                loaded,
                sensor_rom=SENSOR,
                adapter_rom=ADAPTER,
                coupler_rom=COUPLER,
                branch=Ds2409Branch.MAIN,
            )

            self.assertEqual(hop.coupler_rom, COUPLER)
            self.assertIs(hop.branch, Ds2409Branch.MAIN)
            self.assertEqual(timestamp, STAMP)
            if sys.platform != "win32":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_mismatched_path_identity_and_stale_device_fail_before_usb(self) -> None:
        with self.assertRaises(InventoryEvidenceError):
            validate_ds2438_target(
                evidence(),
                sensor_rom=SENSOR,
                adapter_rom=ADAPTER,
                coupler_rom=COUPLER,
                branch=Ds2409Branch.AUX,
            )
        other_adapter = bytes.fromhex("8194063200000014")
        with self.assertRaises(InventoryEvidenceError):
            validate_ds2438_target(
                evidence(sensor_adapter=other_adapter),
                sensor_rom=SENSOR,
                adapter_rom=ADAPTER,
                coupler_rom=COUPLER,
                branch=Ds2409Branch.MAIN,
            )
        with self.assertRaises(InventoryEvidenceError):
            validate_ds2438_target(
                evidence(sensor_last_seen="2026-11-21T12:00:00+00:00"),
                sensor_rom=SENSOR,
                adapter_rom=ADAPTER,
                coupler_rom=COUPLER,
                branch=Ds2409Branch.MAIN,
            )

    def test_invalidated_or_malformed_snapshot_is_never_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.json"
            invalidate_inventory_evidence(path, "customdata invalid")
            with self.assertRaises(InventoryEvidenceError):
                load_inventory_evidence(path)
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(InventoryEvidenceError):
                load_inventory_evidence(path)


if __name__ == "__main__":
    unittest.main()
