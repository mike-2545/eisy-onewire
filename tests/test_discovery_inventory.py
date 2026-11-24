from __future__ import annotations

import threading
import unittest
from datetime import datetime, timezone

from eisy_onewire.discovery_inventory import (
    DS18B20_FAMILY,
    DS2438_FAMILY,
    DiscoveryInventory,
    DiscoveryInventoryError,
    TOPOLOGY_SCOPE,
    default_sensor_name,
    update_inventory,
    update_topology_inventory,
)
from eisy_onewire.ds2409_transport import BranchHop, Ds2409Branch
from eisy_onewire.manual_discovery import AdapterBusyError, ManualDiscoveryService
from eisy_onewire.onewire_discovery import DiscoveryReport, InvalidRom, OneWireDevice
from eisy_onewire.topology_discovery import (
    EventFlagAcknowledgement,
    LocatedDevice,
    TopologyDiscoveryReport,
)


ROMS = tuple(
    bytes.fromhex(value)
    for value in (
        "2811223344556656",
        "2822334455667727",
        "28334455667788E7",
        "1F5566778899AA38",
        "814455667788999C",
    )
)


class DiscoveryInventoryTests(unittest.TestCase):
    def test_five_device_inventory_round_trips_with_partial_branch_scope(self) -> None:
        inventory = update_inventory(
            DiscoveryInventory(),
            DiscoveryReport(tuple(OneWireDevice(rom) for rom in ROMS), ()),
            timestamp="2026-11-22T12:00:00Z",
        )

        restored = DiscoveryInventory.from_data(inventory.to_data())

        self.assertEqual(restored, inventory)
        self.assertEqual(len(restored.devices), 5)
        self.assertFalse(restored.last_scan.complete)
        self.assertEqual(
            restored.last_scan.branches_not_scanned, ("1F5566778899AA38",)
        )

    def test_empty_scan_retains_known_devices_but_marks_none_reachable(self) -> None:
        first = update_inventory(
            DiscoveryInventory(),
            DiscoveryReport((OneWireDevice(ROMS[0]),), ()),
            timestamp="2026-11-22T12:00:00Z",
        )

        empty = update_inventory(
            first,
            DiscoveryReport((), ()),
            timestamp="2026-11-22T13:00:00Z",
        )

        self.assertEqual([item.rom for item in empty.devices], [ROMS[0]])
        self.assertEqual(empty.last_scan.reachable_roms, ())
        self.assertEqual(empty.devices[0].last_seen, "2026-11-22T12:00:00Z")

    def test_invalid_response_and_corrupt_persisted_rom_fail_closed(self) -> None:
        with self.assertRaisesRegex(DiscoveryInventoryError, "invalid responses"):
            update_inventory(
                DiscoveryInventory(),
                DiscoveryReport((), (InvalidRom(b"bad", "bad length"),)),
                timestamp="2026-11-22T12:00:00Z",
            )

        with self.assertRaisesRegex(DiscoveryInventoryError, "CRC-8"):
            DiscoveryInventory.from_data(
                {
                    "schema_version": 1,
                    "devices": [
                        {
                            "rom": "2811223344556600",
                            "family_code": "0x28",
                            "first_seen": "one",
                            "last_seen": "two",
                        }
                    ],
                }
            )

    def test_default_ds18b20_names_are_unique_rom_based_and_family_limited(self) -> None:
        names = [default_sensor_name(rom) for rom in ROMS[:3]]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(name.startswith("DS18B20 28.") for name in names))
        self.assertEqual(ROMS[0][0], DS18B20_FAMILY)
        with self.assertRaisesRegex(DiscoveryInventoryError, "family 0x28"):
            default_sensor_name(ROMS[3])

    def test_manual_service_releases_shared_lock_after_success_and_error(self) -> None:
        lock = threading.Lock()
        service = ManualDiscoveryService(
            lock,
            search=lambda **kwargs: DiscoveryReport((), ()),
            clock=lambda: datetime(2026, 11, 22, tzinfo=timezone.utc),
        )
        service.run_once()
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

        committed_while_locked = []

        def fail_commit(_report, _timestamp):
            committed_while_locked.append(not lock.acquire(blocking=False))
            raise OSError("mirror failed")

        service = ManualDiscoveryService(
            lock,
            search=lambda **kwargs: DiscoveryReport((), ()),
            clock=lambda: datetime(2026, 11, 22, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(OSError, "mirror failed"):
            service.run_once(commit=fail_commit)
        self.assertEqual(committed_while_locked, [True])
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

        def fail(**kwargs):
            raise TimeoutError("timeout")

        service = ManualDiscoveryService(lock, search=fail)
        with self.assertRaises(TimeoutError):
            service.run_once()
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_manual_service_rejects_busy_without_search_or_retry(self) -> None:
        lock = threading.Lock()
        calls = []
        service = ManualDiscoveryService(
            lock,
            search=lambda **kwargs: calls.append(True) or DiscoveryReport((), ()),
        )
        lock.acquire()
        try:
            with self.assertRaises(AdapterBusyError):
                service.run_once()
        finally:
            lock.release()
        self.assertEqual(calls, [])

    def test_topology_round_trip_preserves_adapter_path_and_ds2438_inventory(self) -> None:
        adapter = ROMS[4]
        coupler = ROMS[3]
        ds2438 = bytes.fromhex("2666778899AABB54")
        path = (BranchHop(coupler, Ds2409Branch.MAIN),)
        report = TopologyDiscoveryReport(
            adapter_rom=adapter,
            devices=(
                LocatedDevice(OneWireDevice(adapter), adapter),
                LocatedDevice(OneWireDevice(coupler), adapter),
                LocatedDevice(OneWireDevice(ROMS[0]), adapter),
                LocatedDevice(OneWireDevice(ds2438), adapter, path),
            ),
            scanned_branches=(
                path,
                (BranchHop(coupler, Ds2409Branch.AUX),),
            ),
            branches_not_scanned=(),
            event_flags_acknowledged=(
                EventFlagAcknowledgement(coupler, 0x1F, True, False),
            ),
        )

        inventory = update_topology_inventory(
            DiscoveryInventory(), report, timestamp="2026-11-22T12:00:00Z"
        )
        restored = DiscoveryInventory.from_data(inventory.to_data())

        self.assertEqual(restored, inventory)
        self.assertEqual(restored.last_scan.segment_scope, TOPOLOGY_SCOPE)
        self.assertTrue(restored.last_scan.complete)
        self.assertEqual(
            restored.last_scan.event_flags_acknowledged,
            (EventFlagAcknowledgement(coupler, 0x1F, True, False),),
        )
        stored_ds2438 = next(
            item for item in restored.devices if item.family_code == DS2438_FAMILY
        )
        self.assertEqual(stored_ds2438.adapter_rom, adapter)
        self.assertEqual(stored_ds2438.path, path)

    def test_schema_one_inventory_migrates_without_inventing_topology(self) -> None:
        legacy = {
            "schema_version": 1,
            "devices": [
                {
                    "rom": ROMS[0].hex().upper(),
                    "family_code": "0x28",
                    "first_seen": "2026-11-22T12:00:00Z",
                    "last_seen": "2026-11-22T12:00:00Z",
                }
            ],
            "last_scan": {
                "timestamp": "2026-11-22T12:00:00Z",
                "segment_scope": "selected-adapter-accessible-segment",
                "reachable_roms": [ROMS[0].hex().upper()],
                "complete": True,
                "branches_not_scanned": [],
            },
        }

        restored = DiscoveryInventory.from_data(legacy)

        self.assertIsNone(restored.devices[0].adapter_rom)
        self.assertEqual(restored.devices[0].path, ())

    def test_live_schema_one_empty_last_scan_preserves_validated_devices(self) -> None:
        legacy = {
            "schema_version": 1,
            "devices": [
                {
                    "rom": rom.hex().upper(),
                    "family_code": f"0x{rom[0]:02X}",
                    "first_seen": "2026-11-22T23:58:54.576548Z",
                    "last_seen": "2026-11-23T00:00:11.688713Z",
                }
                for rom in ROMS
            ],
            "last_scan": {},
        }

        restored = DiscoveryInventory.from_data(legacy)

        self.assertEqual(tuple(item.rom for item in restored.devices), tuple(sorted(ROMS)))
        self.assertTrue(
            all(
                item.first_seen == "2026-11-22T23:58:54.576548Z"
                and item.last_seen == "2026-11-23T00:00:11.688713Z"
                for item in restored.devices
            )
        )
        self.assertIsNone(restored.last_scan)

    def test_empty_last_scan_migration_is_narrow_and_other_damage_fails_closed(self) -> None:
        device = {
            "rom": ROMS[0].hex().upper(),
            "family_code": "0x28",
            "first_seen": "2026-11-22T12:00:00Z",
            "last_seen": "2026-11-22T12:00:00Z",
        }
        for schema_version, last_scan in (
            (2, {}),
            (1, {"timestamp": "2026-11-22T12:00:00Z"}),
            (1, []),
        ):
            with self.subTest(schema_version=schema_version, last_scan=last_scan):
                with self.assertRaises(DiscoveryInventoryError):
                    DiscoveryInventory.from_data(
                        {
                            "schema_version": schema_version,
                            "devices": [device],
                            "last_scan": last_scan,
                        }
                    )


if __name__ == "__main__":
    unittest.main()
