from __future__ import annotations

import json
import unittest
from collections.abc import Iterable
from pathlib import Path

from eisy_onewire.iox_profile import build_runtime_json_profile
from eisy_onewire.temperature_presentation import TemperaturePresentation, TemperatureUnit

ROOT = Path(__file__).resolve().parents[1]
TRUSTED_FIXTURE = ROOT / "tests" / "fixtures" / "udi-ring-profile.json"


def _required_keys(items: Iterable[dict[str, object]]) -> set[str]:
    key_sets = [set(item) for item in items]
    return set.intersection(*key_sets)


def _allowed_keys(items: Iterable[dict[str, object]]) -> set[str]:
    result: set[str] = set()
    for item in items:
        result.update(item)
    return result


def _assert_keys_follow_fixture(
    test: unittest.TestCase,
    candidate_items: list[dict[str, object]],
    fixture_items: list[dict[str, object]],
) -> None:
    required = _required_keys(fixture_items)
    allowed = _allowed_keys(fixture_items)
    for item in candidate_items:
        test.assertLessEqual(required, set(item))
        test.assertLessEqual(set(item), allowed)


class IoxProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = build_runtime_json_profile()
        self.fixture = json.loads(TRUSTED_FIXTURE.read_text(encoding="utf-8"))

    def test_runtime_shape_matches_trusted_udi_dynamic_profile(self) -> None:
        self.assertEqual(set(self.profile), set(self.fixture))
        _assert_keys_follow_fixture(
            self,
            self.profile["editors"],
            self.fixture["editors"],
        )

        fixture_ranges = [
            item
            for editor in self.fixture["editors"]
            for item in editor["ranges"]
        ]
        candidate_ranges = [
            item
            for editor in self.profile["editors"]
            for item in editor["ranges"]
        ]
        _assert_keys_follow_fixture(self, candidate_ranges, fixture_ranges)
        for item in candidate_ranges:
            self.assertIsInstance(item["uom"], str)

        _assert_keys_follow_fixture(
            self,
            self.profile["nodedefs"],
            self.fixture["nodedefs"],
        )
        fixture_properties = [
            item
            for node in self.fixture["nodedefs"]
            for item in node["properties"]
        ]
        candidate_properties = [
            item
            for node in self.profile["nodedefs"]
            for item in node["properties"]
        ]
        _assert_keys_follow_fixture(self, candidate_properties, fixture_properties)
        for item in candidate_properties:
            self.assertIsInstance(item["editor"], str)
            self.assertNotIn("is_settable", item)

        fixture_commands = [
            item
            for node in self.fixture["nodedefs"]
            for direction in ("sends", "accepts")
            for item in node["cmds"][direction]
        ]
        candidate_commands = [
            item
            for node in self.profile["nodedefs"]
            for direction in ("sends", "accepts")
            for item in node["cmds"][direction]
        ]
        _assert_keys_follow_fixture(self, candidate_commands, fixture_commands)
        for node in self.profile["nodedefs"]:
            self.assertEqual(set(node["cmds"]), {"sends", "accepts"})
            self.assertEqual(set(node["links"]), {"ctl", "rsp"})
            self.assertNotIn("commands", node)

    def test_profile_semantics_are_fahrenheit_boolean_and_manual_refresh(self) -> None:
        editors = {item["id"]: item for item in self.profile["editors"]}
        self.assertEqual(editors["temperature"]["ranges"][0]["uom"], "17")
        self.assertEqual(editors["temperature"]["ranges"][0]["prec"], 1)
        self.assertEqual(editors["reading_current"]["ranges"][0]["uom"], "2")
        self.assertEqual(editors["reading_current"]["ranges"][0]["subset"], "0,1")
        self.assertEqual(editors["refresh_interval"]["ranges"][0]["uom"], "25")
        self.assertEqual(
            editors["refresh_interval"]["ranges"][0]["subset"],
            "0,30,60,120,300",
        )

        self.assertEqual(self.profile["linkdefs"], [])
        self.assertEqual(
            [node["id"] for node in self.profile["nodedefs"]],
            ["onewire_controller", "ds18b20", "ds2438_hih4000", "ds2438_analog_voltage", "ds2438_solar_irradiance"],
        )
        controller, node, humidity_node, analog_node, solar_node = self.profile["nodedefs"]
        self.assertEqual(controller["properties"][0]["editor"], "reading_current")
        self.assertEqual(
            [item["id"] for item in controller["cmds"]["accepts"]],
            ["DISCOVER", "DS2409_STATUS", "VERIFY_ADAPTER", "AUTO_REFRESH_ENABLE", "AUTO_REFRESH_DISABLE", "ADAPTER_RECOVERY_ENABLE", "ADAPTER_RECOVERY_DISABLE", "USE_FAHRENHEIT", "USE_CELSIUS", "PRECISION_0", "PRECISION_1", "PRECISION_2", "PRECISION_3"],
        )
        properties = {item["id"]: item for item in node["properties"]}
        self.assertEqual(properties["ST"]["editor"], "temperature")
        self.assertEqual(properties["GV0"]["editor"], "reading_current")
        self.assertEqual(properties["GV1"]["editor"], "refresh_interval")
        self.assertEqual(node["cmds"]["sends"], [])
        self.assertEqual(
            [item["id"] for item in node["cmds"]["accepts"]],
            ["REFRESH", "REFRESH_OFF", "REFRESH_30", "REFRESH_60", "REFRESH_120", "REFRESH_300"],
        )
        humidity_properties = {item["id"]: item for item in humidity_node["properties"]}
        self.assertEqual(humidity_properties["ST"]["editor"], "temperature")
        self.assertEqual(humidity_properties["GV2"]["editor"], "relative_humidity")
        self.assertEqual(editors["relative_humidity"]["ranges"][0]["uom"], "51")
        self.assertEqual(editors["relative_humidity"]["ranges"][0]["prec"], 1)
        self.assertEqual(editors["analog_voltage"]["ranges"][0]["uom"], "72")
        self.assertEqual(editors["solar_irradiance"]["ranges"][0]["uom"], "74")
        self.assertEqual(analog_node["properties"][0]["editor"], "analog_voltage")
        self.assertEqual(solar_node["properties"][0]["editor"], "solar_irradiance")

    def test_runtime_profile_is_not_local_store_metadata_shape(self) -> None:
        metadata = json.loads(
            (ROOT / "eisy-onewire.iox_plugin.json").read_text(encoding="utf-8")
        )

        self.assertIn("uom", metadata["editors"][0])
        self.assertNotIn("ranges", metadata["editors"][0])
        self.assertIn("ranges", self.profile["editors"][0])
        self.assertNotEqual(self.profile["nodedefs"], metadata["nodedefs"])

    def test_each_build_is_independent_for_request_id_mutation(self) -> None:
        first = build_runtime_json_profile()
        first["requestId"] = "mutated"

        self.assertNotIn("requestId", build_runtime_json_profile())

    def test_temperature_editor_tracks_global_unit_and_precision(self) -> None:
        profile = build_runtime_json_profile(
            TemperaturePresentation(TemperatureUnit.CELSIUS, 3)
        )
        editor = next(item for item in profile["editors"] if item["id"] == "temperature")
        self.assertEqual(
            editor["ranges"],
            [{"uom": "4", "prec": 3, "step": 0.001, "min": -55, "max": 125}],
        )
        controller = profile["nodedefs"][0]
        properties = {item["id"]: item for item in controller["properties"]}
        self.assertEqual(properties["GV1"]["editor"], "temperature_unit")
        self.assertEqual(properties["GV2"]["editor"], "decimal_precision")


if __name__ == "__main__":
    unittest.main()
