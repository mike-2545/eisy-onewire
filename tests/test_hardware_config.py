from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class HardwareConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = (ROOT / "ds9490.conf").read_text(encoding="utf-8")
        self.metadata = json.loads(
            (ROOT / "eisy-onewire.iox_plugin.json").read_text(encoding="utf-8")
        )

    def test_metadata_uses_official_hardware_config_filename(self) -> None:
        self.assertEqual(
            self.metadata["plugin"]["hardwareConfig"],
            {"filename": "ds9490.conf"},
        )

    def test_rule_matches_only_the_ds9490_vid_and_pid(self) -> None:
        matches = re.findall(r'match\s+"([^"]+)"\s+"([^"]+)";', self.rule)

        self.assertEqual(
            matches,
            [
                ("system", "USB"),
                ("subsystem", "DEVICE"),
                ("type", "ATTACH"),
                ("vendor", "0x04fa"),
                ("product", "0x2490"),
            ],
        )

    def test_rule_uses_usb_notification_instead_of_generic_attach(self) -> None:
        self.assertRegex(self.rule, r"(?m)^notify 100 \{")
        self.assertNotRegex(self.rule, r"(?m)^attach\s")

    def test_rule_targets_cdev_and_preserves_both_placeholders(self) -> None:
        actions = re.findall(r'^\s*action\s+"([^"]+)";', self.rule, re.MULTILINE)

        self.assertEqual(
            actions,
            [
                "chown UDX_OWNER_PLACE_HOLDER /dev/$cdev "
                "&& chmod UDX_PERMISSION_PLACE_HOLDER /dev/$cdev"
            ],
        )
        self.assertNotIn("$ugen", self.rule)
        self.assertEqual(self.rule.count("UDX_OWNER_PLACE_HOLDER"), 1)
        self.assertEqual(self.rule.count("UDX_PERMISSION_PLACE_HOLDER"), 1)

    def test_rule_has_no_serial_alias_or_broad_permission_fallback(self) -> None:
        self.assertNotIn("pg3.serial.dev.ops", self.rule)
        self.assertNotIn("/dev/tty", self.rule)
        self.assertNotRegex(self.rule, r"chmod\s+(666|777)")
        self.assertNotIn("detach", self.rule.lower())
        self.assertNotIn("ln ", self.rule)
        self.assertNotIn("INTERFACE", self.rule)

    def test_manifest_defines_controller_and_reusable_temperature_child(self) -> None:
        plugin = self.metadata["plugin"]

        self.assertEqual(
            [node["id"] for node in self.metadata["nodedefs"]],
            ["onewire_controller", "ds18b20", "ds2438_hih4000", "ds2438_analog_voltage", "ds2438_solar_irradiance"],
        )
        controller, node, humidity_node, analog_node, solar_node = self.metadata["nodedefs"]
        self.assertEqual(
            [item["id"] for item in controller["properties"]],
            ["ST", "GV0", "GV1", "GV2", "GV3"],
        )
        self.assertEqual(node["id"], "ds18b20")
        self.assertEqual(
            [item["id"] for item in node["properties"]], ["ST", "GV0", "GV1"]
        )
        self.assertEqual(
            [item["id"] for item in node["commands"]["accepts"]],
            ["REFRESH", "REFRESH_OFF", "REFRESH_30", "REFRESH_60", "REFRESH_120", "REFRESH_300"],
        )
        self.assertEqual(node["commands"]["sends"], [])
        self.assertEqual(humidity_node["id"], "ds2438_hih4000")
        self.assertEqual(analog_node["properties"][0]["editor"]["idref"], "analog_voltage")
        self.assertEqual(solar_node["properties"][0]["editor"]["idref"], "solar_irradiance")
        self.assertEqual(
            [item["id"] for item in humidity_node["properties"]],
            ["ST", "GV0", "GV1", "GV2"],
        )
        self.assertFalse(plugin["requiresIoXAccess"])
        self.assertEqual(plugin["requirements"], ["udi_interface==3.4.9"])
        self.assertEqual(plugin["name"], "eisy-onewire")


if __name__ == "__main__":
    unittest.main()
