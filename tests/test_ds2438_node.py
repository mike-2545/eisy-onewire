from __future__ import annotations

from copy import deepcopy
import threading
import unittest

from eisy_onewire.ds2438_humidity import Hih4000Reading
from eisy_onewire.ds2438_node import create_ds2438_node_class
from eisy_onewire.ds2438_service import Ds2438Acquisition
from eisy_onewire.temperature_presentation import TemperaturePresentation, TemperatureUnit


ROM = bytes.fromhex("2666778899AABB54")


class Notices(dict):
    def delete(self, key):
        self.pop(key, None)


class Poly:
    def __init__(self):
        self.Notices = Notices()
        self.added = []

    def addNode(self, node):
        self.added.append(deepcopy(node.drivers))


class Node:
    drivers = []

    def __init__(self, poly, primary, address, name):
        self.poly, self.primary, self.address, self.name = poly, primary, address, name
        self.drivers = deepcopy(type(self).drivers)
        self.reports = []

    def setDriver(self, driver, value, report=True, force=False, uom=None):
        del force
        row = next(item for item in self.drivers if item["driver"] == driver)
        row["value"] = value
        if uom is not None:
            row["uom"] = uom
        if report:
            self.reportDriver(driver, True)

    def reportDriver(self, driver, force):
        del force
        row = next(item for item in self.drivers if item["driver"] == driver)
        self.reports.append((driver, row["value"], row["uom"]))

    def runCmd(self, command):
        return self.commands[command["cmd"]](self, command)


class Logger:
    def info(self, *args): pass
    def error(self, *args): pass
    def warning(self, *args): pass


class Service:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def read(self, rom):
        self.calls.append(rom)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def reading():
    return Hih4000Reading(ROM, 23.875, 35.357612345, 1.98, 5.21)


class Ds2438NodeTests(unittest.TestCase):
    def make_node(self, service, **kwargs):
        cls = create_ds2438_node_class(Node, logger=Logger(), root_service_factory=lambda: service)
        poly = Poly()
        saved = []
        node = cls(
            poly, primary="onewire", address="2666778899aabb", name="Humidity",
            rom=ROM, adapter_lock=threading.Lock(),
            record_measurement=lambda item: saved.append(item) or True,
            **kwargs,
        )
        return node, poly, saved

    def test_fresh_refresh_publishes_temperature_humidity_and_validity(self):
        service = Service(Ds2438Acquisition(reading()))
        node, poly, saved = self.make_node(service)
        self.assertTrue(node.refresh_measurements())
        values = {row["driver"]: row for row in node.drivers}
        self.assertEqual(values["ST"]["value"], 75.0)
        self.assertEqual(values["ST"]["uom"], 17)
        self.assertEqual(values["GV2"]["value"], 35.4)
        self.assertEqual(values["GV2"]["uom"], 51)
        self.assertEqual(values["GV0"]["value"], 1)
        self.assertEqual(saved, [reading()])
        self.assertEqual(service.calls, [ROM])
        self.assertEqual(len(poly.added), 1)

    def test_restart_cache_is_stale_and_presentation_never_touches_humidity_or_usb(self):
        service = Service(AssertionError("USB must not be called"))
        node, _, _ = self.make_node(service, cached_reading=reading())
        before_humidity = next(row.copy() for row in node.drivers if row["driver"] == "GV2")
        node.set_temperature_presentation(TemperaturePresentation(TemperatureUnit.CELSIUS, 3))
        values = {row["driver"]: row for row in node.drivers}
        self.assertEqual(values["ST"]["value"], 23.875)
        self.assertEqual(values["ST"]["uom"], 4)
        self.assertEqual(values["GV2"], before_humidity)
        self.assertEqual(values["GV0"]["value"], 0)
        self.assertEqual(service.calls, [])

    def test_failure_retains_stale_measurements_and_marks_invalid(self):
        service = Service(RuntimeError("CRC failure"))
        node, _, _ = self.make_node(service, cached_reading=reading())
        self.assertFalse(node.refresh_measurements())
        values = {row["driver"]: row for row in node.drivers}
        self.assertEqual(values["ST"]["value"], 75.0)
        self.assertEqual(values["GV2"]["value"], 35.4)
        self.assertEqual(values["GV0"]["value"], 0)

    def test_interval_commands_are_configuration_only(self):
        service = Service(AssertionError("USB must not be called"))
        accepted = []
        cls = create_ds2438_node_class(Node, logger=Logger(), root_service_factory=lambda: service)
        node = cls(
            Poly(), primary="onewire", address="2666778899aabb", name="Humidity",
            rom=ROM, adapter_lock=threading.Lock(),
            set_refresh_interval=lambda seconds: accepted.append(seconds) or True,
        )
        self.assertTrue(node.runCmd({"cmd": "REFRESH_120"}))
        self.assertEqual(accepted, [120])
        self.assertEqual(service.calls, [])

    def test_unassigned_application_blocks_manual_access_without_usb(self):
        service = Service(AssertionError("USB must not be called"))
        node, _, _ = self.make_node(service, cached_reading=reading())

        node.set_application_authorized(False)

        self.assertFalse(node.refresh_measurements())
        self.assertEqual(service.calls, [])
        values = {row["driver"]: row for row in node.drivers}
        self.assertEqual(values["GV0"]["value"], 0)
        self.assertEqual(values["ST"]["value"], 75.0)


if __name__ == "__main__":
    unittest.main()
