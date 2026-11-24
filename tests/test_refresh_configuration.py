from __future__ import annotations

import unittest

from eisy_onewire.discovery_inventory import DiscoveryInventory, update_inventory
from eisy_onewire.onewire_discovery import DiscoveryReport, OneWireDevice
from eisy_onewire.refresh_configuration import (
    REFRESH_CONFIGURATION_KEY,
    RefreshConfiguration,
    RefreshConfigurationError,
    SensorRefreshConfiguration,
    resolved_sensor_configuration,
)


SENSOR_A = bytes.fromhex("2811223344556656")
LAB = bytes.fromhex("2822334455667727")
UNKNOWN = bytes.fromhex("28334455667788E7")


def inventory() -> DiscoveryInventory:
    return update_inventory(
        DiscoveryInventory(),
        DiscoveryReport(
            (OneWireDevice(SENSOR_A), OneWireDevice(LAB)), ()
        ),
        timestamp="2026-11-23T12:00:00Z",
    )


class RefreshConfigurationTests(unittest.TestCase):
    def test_missing_configuration_is_master_off_with_no_invented_intervals(self) -> None:
        config = RefreshConfiguration.from_data(None, inventory())

        self.assertFalse(config.master_enabled)
        self.assertFalse(config.scheduler_armed)
        self.assertEqual(config.sensors, ())
        self.assertEqual(REFRESH_CONFIGURATION_KEY, "automatic_refresh_configuration")

    def test_inventory_backed_settings_round_trip(self) -> None:
        sensor_a = SensorRefreshConfiguration(
            SENSOR_A, "28112233445566", 60, "Test Sensor", "Test Sensor"
        )
        lab = SensorRefreshConfiguration(
            LAB, "28223344556677", 0, "Lab Probe", "Lab Probe"
        )
        config = RefreshConfiguration(True, (sensor_a, lab))

        restored = RefreshConfiguration.from_data(config.to_data(), inventory())

        self.assertEqual(restored, config)
        self.assertEqual(restored.with_interval(SENSOR_A, 300).sensor(SENSOR_A).interval_seconds, 300)
        self.assertFalse(restored.with_master(False).master_enabled)
        self.assertTrue(restored.with_master(True).scheduler_armed)

    def test_schema_one_master_true_migrates_to_explicitly_disarmed_schema_two(self) -> None:
        raw = {
            "schema_version": 1,
            "master_enabled": True,
            "sensors": [
                {
                    "rom": SENSOR_A.hex().upper(),
                    "address": "28112233445566",
                    "interval_seconds": 60,
                    "legacy_name_override": "Test Sensor",
                    "effective_name": "Test Sensor",
                }
            ],
        }

        migrated = RefreshConfiguration.from_data(raw, inventory())

        self.assertTrue(RefreshConfiguration.requires_migration(raw))
        self.assertFalse(migrated.master_enabled)
        self.assertFalse(migrated.scheduler_armed)
        self.assertEqual(migrated.sensor(SENSOR_A).interval_seconds, 60)
        self.assertEqual(migrated.to_data()["schema_version"], 2)

    def test_schema_two_rejects_armed_without_master(self) -> None:
        raw = RefreshConfiguration().to_data()
        raw["scheduler_armed"] = True

        with self.assertRaisesRegex(RefreshConfigurationError, "cannot be true"):
            RefreshConfiguration.from_data(raw, inventory())

    def test_invalid_or_uninventoried_settings_fail_closed(self) -> None:
        valid_row = {
            "rom": SENSOR_A.hex().upper(),
            "address": "28112233445566",
            "interval_seconds": 30,
            "legacy_name_override": "Test Sensor",
            "effective_name": "Test Sensor",
        }
        invalid = (
            {"schema_version": 1, "master_enabled": "yes", "sensors": []},
            {
                "schema_version": 1,
                "master_enabled": False,
                "sensors": [{**valid_row, "interval_seconds": 45}],
            },
            {
                "schema_version": 1,
                "master_enabled": False,
                "sensors": [{**valid_row, "address": "wrong"}],
            },
            {
                "schema_version": 1,
                "master_enabled": False,
                "sensors": [
                    {
                        **valid_row,
                        "rom": UNKNOWN.hex().upper(),
                        "address": "28334455667788",
                    }
                ],
            },
            {
                "schema_version": 1,
                "master_enabled": False,
                "sensors": [valid_row, valid_row],
            },
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(RefreshConfigurationError):
                    RefreshConfiguration.from_data(raw, inventory())

    def test_rename_precedence_preserves_legacy_edits_and_iox_restart_names(self) -> None:
        first = resolved_sensor_configuration(
            SENSOR_A,
            legacy_name_override="Test Sensor",
            database_name="Test Sensor",
            default_name="DS18B20 28.112233445566",
            previous=None,
        )
        renamed = resolved_sensor_configuration(
            SENSOR_A,
            legacy_name_override="Test Sensor",
            database_name="Garden Bin",
            default_name="ignored",
            previous=first,
        )
        self.assertEqual(renamed.effective_name, "Garden Bin")

        legacy_changed = resolved_sensor_configuration(
            SENSOR_A,
            legacy_name_override="Test Sensor West",
            database_name="Garden Bin",
            default_name="ignored",
            previous=renamed,
        )
        self.assertEqual(legacy_changed.effective_name, "Test Sensor West")
        self.assertEqual(legacy_changed.interval_seconds, 0)


if __name__ == "__main__":
    unittest.main()
