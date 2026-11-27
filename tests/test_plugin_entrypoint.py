from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from eisy_onewire.version import PLUGIN_VERSION

ROOT = Path(__file__).resolve().parents[1]


class PluginEntrypointTests(unittest.TestCase):
    def test_diagnostic_script_without_subcommand_performs_no_native_open(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "diagnostic.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("adapter-status", result.stderr)
        self.assertIn("search-rom", result.stderr)
        self.assertIn("temperature-power", result.stderr)
        self.assertIn("temperature-read", result.stderr)
        self.assertNotIn("native DS2490 transport", result.stderr)

    def test_temperature_command_requires_explicit_rom_before_native_open(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "diagnostic.py"), "temperature-power"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --rom", result.stderr)
        self.assertNotIn("native DS2490 transport", result.stderr)

    def test_every_sensor_or_status_diagnostic_requires_adapter_identity(self) -> None:
        commands = (
            ["adapter-status"],
            ["search-rom"],
            ["temperature-power", "--rom", "280102030405069E"],
            ["temperature-read", "--rom", "280102030405069E"],
            [
                "ds2438-read",
                "--rom", "26010203040506D6",
                "--coupler-rom", "1F01020304050694",
                "--branch", "main",
            ],
        )
        for arguments in commands:
            with self.subTest(command=arguments[0]):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "diagnostic.py"), *arguments],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("adapter-rom", result.stderr)
                self.assertNotIn("native DS2490 transport", result.stderr)

    def test_runtime_dependency_is_declared_and_installed_per_user(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        metadata = json.loads(
            (ROOT / "eisy-onewire.iox_plugin.json").read_text(encoding="utf-8")
        )
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")

        self.assertEqual(requirements, ["udi_interface==3.4.9"])
        self.assertEqual(metadata["plugin"]["requirements"], requirements)
        self.assertIn("python3 -m pip install", installer)
        self.assertIn("--user", installer)
        self.assertIn("--requirement requirements.txt", installer)
        self.assertIn(
            "find src/eisy_onewire -type d -name __pycache__ -prune "
            "-exec rm -rf {} +",
            installer,
        )
        self.assertLess(installer.index("find src/eisy_onewire"), installer.index("pip install"))
        self.assertNotIn("sudo", installer)

    def test_real_entrypoint_starts_when_user_scoped_dependency_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            helper_dir = Path(directory)
            events_file = helper_dir / "events.txt"
            (helper_dir / "udi_interface.py").write_text(
                textwrap.dedent(
                    """
                    import os

                    _EVENTS = os.environ["EISY_ONEWIRE_ENTRYPOINT_EVENTS"]

                    def _record(value):
                        with open(_EVENTS, "a", encoding="utf-8") as stream:
                            stream.write(value + "\\n")

                    class _Logger:
                        def info(self, message, *args):
                            _record("log")

                        def warning(self, message, *args):
                            _record("warning")

                        def error(self, message, *args):
                            _record("error")

                    LOGGER = _Logger()

                    class _Notices(dict):
                        def delete(self, key):
                            self.pop(key, None)

                    class Node:
                        drivers = []

                        def __init__(self, poly, primary, address, name):
                            self.poly = poly
                            self.primary = primary
                            self.address = address
                            self.name = name
                            self.drivers = [dict(value) for value in type(self).drivers]

                        def getDriver(self, driver):
                            return next((value["value"] for value in self.drivers if value["driver"] == driver), None)

                        def setDriver(self, driver, value, report=True, force=False, uom=None):
                            del report, force
                            item = next(value for value in self.drivers if value["driver"] == driver)
                            item["value"] = value
                            if uom is not None:
                                item["uom"] = uom

                        def reportDriver(self, driver, force):
                            pass

                    class Custom:
                        def __init__(self, poly, namespace):
                            self.values = {}

                        def load(self, data, save=False):
                            self.values = dict(data)

                        def update(self, data, save=False):
                            self.values.update(data)

                        def items(self):
                            return self.values.items()

                        def __setitem__(self, key, value):
                            self.values[key] = value

                    class Interface:
                        CUSTOMDATA = 6
                        CUSTOMPARAMS = 7

                        def __init__(self, node_classes):
                            if [node.id for node in node_classes] != [
                                "onewire_controller", "ds18b20", "ds2438_hih4000",
                                "ds2438_analog_voltage", "ds2438_solar_irradiance",
                            ]:
                                raise AssertionError("entry point did not register generic node classes")
                            self.Notices = _Notices()
                            self.nodes = {}
                            self.custom_callbacks = {}
                            _record("interface")

                        def start(self, options):
                            _record("start:" + options["version"])

                        def ready(self):
                            _record("ready")
                            self.custom_callbacks[self.CUSTOMPARAMS]({})
                            self.custom_callbacks[self.CUSTOMDATA]({})

                        def setCustomParamsDoc(self):
                            _record("customParamsDoc")

                        def subscribe(self, topic, callback):
                            if topic not in (self.CUSTOMPARAMS, self.CUSTOMDATA):
                                raise AssertionError("unexpected subscription")
                            self.custom_callbacks[topic] = callback
                            _record(
                                "subscribe:customparams"
                                if topic == self.CUSTOMPARAMS
                                else "subscribe:customdata"
                            )

                        def getNode(self, address):
                            return self.nodes.get(address)

                        def addNode(self, node, rename=False):
                            if rename:
                                raise AssertionError("reconciliation must preserve IoX renames")
                            self.nodes[node.address] = node
                            _record("addNode:" + node.address)

                        def updateJsonProfile(self, profile, options=None):
                            if options != {"waitResponse": True}:
                                raise AssertionError("profile update did not wait for response")
                            if [item["id"] for item in profile["editors"]] != ["temperature", "reading_current", "refresh_interval", "temperature_unit", "decimal_precision", "relative_humidity", "analog_voltage", "solar_irradiance"]:
                                raise AssertionError("unexpected profile editors")
                            if [item["id"] for item in profile["nodedefs"]] != ["onewire_controller", "ds18b20", "ds2438_hih4000", "ds2438_analog_voltage", "ds2438_solar_irradiance"]:
                                raise AssertionError("unexpected node definitions")
                            if any("ranges" not in item or "uom" in item for item in profile["editors"]):
                                raise AssertionError("editors do not use runtime ranges")
                            node = profile["nodedefs"][1]
                            if "cmds" not in node or "links" not in node or "commands" in node:
                                raise AssertionError("node does not use runtime command/link shape")
                            if any(not isinstance(item["editor"], str) for item in node["properties"]):
                                raise AssertionError("property editor is not a string id")
                            if [item["id"] for item in node["cmds"]["accepts"]] != ["REFRESH", "REFRESH_OFF", "REFRESH_30", "REFRESH_60", "REFRESH_120", "REFRESH_300"]:
                                raise AssertionError("sensor commands must be fixed and parameterless")
                            controller = profile["nodedefs"][0]
                            if [item["id"] for item in controller["cmds"]["accepts"]] != ["DISCOVER", "DS2409_STATUS", "VERIFY_ADAPTER", "AUTO_REFRESH_ENABLE", "AUTO_REFRESH_DISABLE", "ADAPTER_RECOVERY_ENABLE", "ADAPTER_RECOVERY_DISABLE", "USE_FAHRENHEIT", "USE_CELSIUS", "PRECISION_0", "PRECISION_1", "PRECISION_2", "PRECISION_3"]:
                                raise AssertionError("controller commands must be parameterless")
                            if "delete" in profile:
                                raise AssertionError("profile update must be additive")
                            profile["requestId"] = "entrypoint-profile"
                            _record("updateJsonProfile")
                            return {"success": True, "requestId": "entrypoint-profile"}

                        def getNodesFromDb(self, address):
                            return []

                        def runForever(self):
                            _record("runForever")

                        def stop(self):
                            _record("stop")
                    """
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(helper_dir)
            environment["EISY_ONEWIRE_ENTRYPOINT_EVENTS"] = str(events_file)

            result = subprocess.run(
                [sys.executable, str(ROOT / "plugin.py")],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                events_file.read_text(encoding="utf-8").splitlines(),
                [
                    "interface",
                    f"start:{PLUGIN_VERSION}",
                    "customParamsDoc",
                    "subscribe:customparams",
                    "subscribe:customdata",
                    "log",
                    "ready",
                    "log",
                    "updateJsonProfile",
                    "addNode:onewire",
                    "log",
                    "runForever",
                    "stop",
                ],
            )


if __name__ == "__main__":
    unittest.main()
