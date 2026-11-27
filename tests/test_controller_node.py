from __future__ import annotations

from copy import deepcopy
import unittest

from eisy_onewire.controller_node import (
    ADAPTER_RECOVERY_DISABLE_COMMAND,
    ADAPTER_RECOVERY_ENABLE_COMMAND,
    AUTOMATIC_REFRESH_DISABLE_COMMAND,
    AUTOMATIC_REFRESH_ENABLE_COMMAND,
    DISCOVER_COMMAND,
    DS2409_STATUS_COMMAND,
    VERIFY_ADAPTER_COMMAND,
    USE_CELSIUS_COMMAND,
    USE_FAHRENHEIT_COMMAND,
    create_controller_node_class,
)
from eisy_onewire.temperature_presentation import TemperaturePresentation, TemperatureUnit


class FakeNode:
    drivers = []

    def __init__(self, poly, primary, address, name) -> None:
        self.poly = poly
        self.primary = primary
        self.address = address
        self.name = name
        self.drivers = deepcopy(type(self).drivers)
        self.reports = []

    def setDriver(self, driver, value, report=True, force=False, uom=None) -> None:
        del report, force
        item = next(item for item in self.drivers if item["driver"] == driver)
        item["value"] = value
        if uom is not None:
            item["uom"] = uom

    def reportDriver(self, driver, force) -> None:
        del force
        self.reports.append(driver)


class ControllerNodeTests(unittest.TestCase):
    def test_controller_is_stable_self_primary_and_reports_probe_state(self) -> None:
        node_class = create_controller_node_class(FakeNode)
        calls = []
        accessible = node_class(
            object(),
            adapter_accessible=True,
            discover=lambda: calls.append("discover") or True,
            read_ds2409_status=lambda: calls.append("status") or True,
            verify_adapter_identity=lambda: calls.append("verify") or True,
        )
        unavailable = node_class(
            object(),
            adapter_accessible=False,
            discover=lambda: False,
            read_ds2409_status=lambda: False,
        )

        self.assertEqual(
            (accessible.address, accessible.primary, accessible.name, accessible.id),
            ("onewire", "onewire", "1-Wire Controller", "onewire_controller"),
        )
        self.assertEqual(
            accessible.drivers,
            [
                {"driver": "ST", "value": 1, "uom": 2},
                {"driver": "GV0", "value": 0, "uom": 2},
                {"driver": "GV1", "value": 0, "uom": 25},
                {"driver": "GV2", "value": 1, "uom": 25},
                {"driver": "GV3", "value": 0, "uom": 2},
            ],
        )
        self.assertEqual(
            unavailable.drivers,
            [
                {"driver": "ST", "value": 0, "uom": 2},
                {"driver": "GV0", "value": 0, "uom": 2},
                {"driver": "GV1", "value": 0, "uom": 25},
                {"driver": "GV2", "value": 1, "uom": 25},
                {"driver": "GV3", "value": 0, "uom": 2},
            ],
        )
        accessible.query()
        self.assertEqual(accessible.reports, ["ST", "GV0", "GV1", "GV2", "GV3"])
        self.assertTrue(accessible.discover_devices())
        self.assertTrue(accessible.ds2409_status())
        self.assertTrue(accessible.verify_adapter_identity())
        self.assertEqual(calls, ["discover", "status", "verify"])
        self.assertEqual(
            accessible.commands,
            {
                DISCOVER_COMMAND: accessible.discover_devices.__func__,
                DS2409_STATUS_COMMAND: accessible.ds2409_status.__func__,
                VERIFY_ADAPTER_COMMAND: accessible.verify_adapter_identity.__func__,
                AUTOMATIC_REFRESH_ENABLE_COMMAND: accessible.automatic_refresh_enable.__func__,
                AUTOMATIC_REFRESH_DISABLE_COMMAND: accessible.automatic_refresh_disable.__func__,
                ADAPTER_RECOVERY_ENABLE_COMMAND: accessible.adapter_recovery_enable.__func__,
                ADAPTER_RECOVERY_DISABLE_COMMAND: accessible.adapter_recovery_disable.__func__,
                USE_FAHRENHEIT_COMMAND: accessible.use_fahrenheit.__func__,
                USE_CELSIUS_COMMAND: accessible.use_celsius.__func__,
                "PRECISION_0": accessible.precision_0.__func__,
                "PRECISION_1": accessible.precision_1.__func__,
                "PRECISION_2": accessible.precision_2.__func__,
                "PRECISION_3": accessible.precision_3.__func__,
            },
        )

    def test_master_commands_only_persist_and_display_desired_state(self) -> None:
        node_class = create_controller_node_class(FakeNode)
        calls = []
        node = node_class(
            object(),
            adapter_accessible=True,
            discover=lambda: False,
            read_ds2409_status=lambda: False,
            automatic_refresh_enabled=False,
            set_automatic_refresh=lambda enabled: calls.append(enabled) or True,
        )

        self.assertTrue(node.automatic_refresh_enable())
        self.assertEqual(node.drivers[1]["value"], 1)
        self.assertTrue(node.automatic_refresh_disable())
        self.assertEqual(node.drivers[1]["value"], 0)
        self.assertEqual(calls, [True, False])

    def test_unit_and_precision_commands_delegate_without_hardware_behavior(self) -> None:
        calls = []
        node = create_controller_node_class(FakeNode)(
            object(),
            adapter_accessible=True,
            discover=lambda: self.fail("unit command must not discover"),
            read_ds2409_status=lambda: self.fail("unit command must not read status"),
            set_temperature_unit=lambda unit: calls.append(("unit", unit)) or True,
            set_temperature_precision=lambda places: calls.append(("precision", places)) or True,
        )
        self.assertTrue(node.use_celsius())
        self.assertTrue(node.precision_3())
        node.set_temperature_presentation(
            TemperaturePresentation(TemperatureUnit.CELSIUS, 3)
        )
        self.assertEqual(
            calls,
            [("unit", TemperatureUnit.CELSIUS), ("precision", 3)],
        )
        self.assertEqual(node.drivers[2]["value"], 1)
        self.assertEqual(node.drivers[3]["value"], 3)

    def test_adapter_recovery_commands_persist_and_display_authorization(self) -> None:
        calls = []
        node = create_controller_node_class(FakeNode)(
            object(),
            adapter_accessible=False,
            discover=lambda: False,
            read_ds2409_status=lambda: False,
            adapter_recovery_enabled=False,
            set_adapter_recovery=lambda enabled: calls.append(enabled) or True,
        )
        self.assertTrue(node.adapter_recovery_enable())
        self.assertEqual(node.drivers[4]["value"], 1)
        self.assertTrue(node.adapter_recovery_disable())
        self.assertEqual(node.drivers[4]["value"], 0)
        self.assertEqual(calls, [True, False])


if __name__ == "__main__":
    unittest.main()
