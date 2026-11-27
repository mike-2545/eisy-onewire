from __future__ import annotations

import importlib.util
import json
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path

from eisy_onewire.version import PLUGIN_VERSION

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_plugin_package.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("build_plugin_package", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("package builder could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PluginPackageTests(unittest.TestCase):
    def test_archive_contains_only_the_reviewed_plugin_allowlist(self) -> None:
        builder = load_builder()

        with tempfile.TemporaryDirectory() as directory:
            archive = builder.build(Path(directory))
            with tarfile.open(archive, "r:gz") as package:
                names = package.getnames()
                modes = {member.name: member.mode for member in package.getmembers()}
                manifest = json.load(package.extractfile("eisy-onewire.iox_plugin.json"))

        self.assertEqual(names, list(builder.PACKAGE_FILES))
        self.assertEqual(len(names), 49)
        self.assertIn("LICENSE", names)
        self.assertIn("src/eisy_onewire/onewire_discovery.py", names)
        self.assertIn("src/eisy_onewire/search_diagnostic.py", names)
        self.assertIn("src/eisy_onewire/ds18b20.py", names)
        self.assertIn("src/eisy_onewire/ds18b20_transport.py", names)
        self.assertIn("src/eisy_onewire/temperature_diagnostic.py", names)
        self.assertIn("src/eisy_onewire/temperature_service.py", names)
        self.assertIn("src/eisy_onewire/temperature_presentation.py", names)
        self.assertIn("src/eisy_onewire/presentation_worker.py", names)
        self.assertIn("src/eisy_onewire/controller_node.py", names)
        self.assertIn("src/eisy_onewire/ds18b20_node.py", names)
        self.assertIn("src/eisy_onewire/branch_temperature.py", names)
        self.assertIn("src/eisy_onewire/node_registry.py", names)
        self.assertIn("src/eisy_onewire/refresh_configuration.py", names)
        self.assertIn("src/eisy_onewire/automatic_refresh.py", names)
        self.assertIn("src/eisy_onewire/adapter_exclusion.py", names)
        self.assertIn("src/eisy_onewire/inventory_evidence.py", names)
        self.assertIn("src/eisy_onewire/discovery_inventory.py", names)
        self.assertIn("src/eisy_onewire/manual_discovery.py", names)
        self.assertIn("src/eisy_onewire/ds2409_transport.py", names)
        self.assertIn("src/eisy_onewire/ds2409_status.py", names)
        self.assertIn("src/eisy_onewire/ds2438.py", names)
        self.assertIn("src/eisy_onewire/ds2438_transport.py", names)
        self.assertIn("src/eisy_onewire/ds2438_diagnostic.py", names)
        self.assertIn("src/eisy_onewire/ds2438_humidity.py", names)
        self.assertIn("src/eisy_onewire/ds2438_analog.py", names)
        self.assertIn("src/eisy_onewire/ds2438_service.py", names)
        self.assertIn("src/eisy_onewire/ds2438_node.py", names)
        self.assertIn("src/eisy_onewire/ds2438_analog_node.py", names)
        self.assertIn("src/eisy_onewire/topology_discovery.py", names)
        self.assertIn("src/eisy_onewire/sensor_config.py", names)
        self.assertIn("default-sensors.json", names)
        self.assertIn("POLYGLOT_CONFIG.md", names)
        self.assertIn("THIRD_PARTY_NOTICES.md", names)
        self.assertIn("src/eisy_onewire/iox_profile.py", names)
        self.assertEqual(
            manifest["plugin"]["licenseLink"],
            "https://github.com/mike-2545/eisy-onewire/blob/main/LICENSE",
        )
        self.assertEqual(modes["plugin.py"], 0o755)
        self.assertEqual(modes["diagnostic.py"], 0o755)
        self.assertEqual(modes["install.sh"], 0o755)
        self.assertEqual(manifest["plugin"]["hardwareConfig"]["filename"], "ds9490.conf")
        self.assertEqual(
            [node["id"] for node in manifest["nodedefs"]],
            ["onewire_controller", "ds18b20", "ds2438_hih4000", "ds2438_analog_voltage", "ds2438_solar_irradiance"],
        )
        self.assertEqual(manifest["plugin"]["version"], PLUGIN_VERSION)

    def test_archive_build_is_reproducible(self) -> None:
        builder = load_builder()

        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_archive = builder.build(Path(first))
            second_archive = builder.build(Path(second))

            self.assertEqual(first_archive.read_bytes(), second_archive.read_bytes())

    def test_version_is_consistent_across_package_sources(self) -> None:
        metadata = json.loads(
            (ROOT / "eisy-onewire.iox_plugin.json").read_text(encoding="utf-8")
        )
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(PLUGIN_VERSION, "0.3.17")
        self.assertEqual(metadata["plugin"]["version"], PLUGIN_VERSION)
        self.assertEqual(project["project"]["version"], PLUGIN_VERSION)
        self.assertEqual(project["project"]["dependencies"], ["udi_interface==3.4.9"])
        self.assertEqual(project["project"]["license"], {"text": "MIT"})

    def test_archive_contains_project_and_third_party_license_notices(self) -> None:
        builder = load_builder()

        with tempfile.TemporaryDirectory() as directory:
            archive = builder.build(Path(directory))
            with tarfile.open(archive, "r:gz") as package:
                license_text = package.extractfile("LICENSE").read().decode("utf-8")
                notices = package.extractfile("THIRD_PARTY_NOTICES.md").read().decode(
                    "utf-8"
                )

        self.assertIn("MIT License", license_text)
        self.assertIn("Copyright (c) 2026 Mike Mummert", license_text)
        self.assertIn("udi_interface==3.4.9", notices)


if __name__ == "__main__":
    unittest.main()
