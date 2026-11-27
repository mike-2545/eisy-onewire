from __future__ import annotations

from copy import deepcopy
import threading
import unittest

from eisy_onewire.ds2438 import Ds2438Measurement
from eisy_onewire.ds2438_analog import SolarCalibration
from eisy_onewire.ds2438_analog_node import (
    create_ds2438_analog_node_class,
    create_ds2438_solar_node_class,
)
from eisy_onewire.ds2438_service import Ds2438RawAcquisition


ROM = bytes.fromhex("2666778899AABB54")


class Notices(dict):
    def delete(self, key): self.pop(key, None)


class Poly:
    def __init__(self): self.Notices, self.added = Notices(), []
    def addNode(self, node): self.added.append(deepcopy(node.drivers))


class Node:
    drivers = []
    def __init__(self, poly, primary, address, name):
        self.poly, self.primary, self.address, self.name = poly, primary, address, name
        self.drivers, self.reports = deepcopy(type(self).drivers), []
    def setDriver(self, driver, value, report=True, force=False, uom=None):
        del force
        row = next(item for item in self.drivers if item["driver"] == driver)
        row["value"] = value
        if uom is not None: row["uom"] = uom
        if report: self.reportDriver(driver, True)
    def reportDriver(self, driver, force):
        del force
        row = next(item for item in self.drivers if item["driver"] == driver)
        self.reports.append((driver, row["value"], row["uom"]))


class Logger:
    def info(self, *args): pass
    def error(self, *args): pass


class Service:
    def __init__(self): self.calls = []
    def read(self, rom):
        self.calls.append(rom)
        return Ds2438RawAcquisition(Ds2438Measurement(rom, 18, 64.4, 0.74, 4.97))


class Ds2438AnalogNodeTests(unittest.TestCase):
    def make_node(self, *, solar=False, calibration=None, error=None):
        service, saved, poly = Service(), [], Poly()
        factory = create_ds2438_solar_node_class if solar else create_ds2438_analog_node_class
        cls = factory(Node, logger=Logger(), root_service_factory=lambda: service)
        node = cls(
            poly, primary="onewire", address="2666778899aabb", name="Sensor",
            rom=ROM, adapter_lock=threading.Lock(), calibration=calibration,
            configuration_error=error,
            record_measurement=lambda reading: saved.append(reading) or True,
        )
        return node, service, saved

    def test_generic_refresh_publishes_vad_vdd_with_official_voltage_uom(self):
        node, service, saved = self.make_node()
        self.assertTrue(node.refresh_measurements())
        values = {row["driver"]: row for row in node.drivers}
        self.assertEqual((values["ST"]["value"], values["ST"]["uom"]), (0.74, 72))
        self.assertEqual((values["GV2"]["value"], values["GV2"]["uom"]), (4.97, 72))
        self.assertEqual(values["GV0"]["value"], 1)
        self.assertEqual(service.calls, [ROM])
        self.assertEqual(len(saved), 1)

    def test_solar_refresh_uses_explicit_calibration_and_watts_per_square_meter(self):
        node, _, _ = self.make_node(solar=True, calibration=SolarCalibration(250, -1.5))
        self.assertTrue(node.refresh_measurements())
        values = {row["driver"]: row for row in node.drivers}
        self.assertEqual((values["ST"]["value"], values["ST"]["uom"]), (183.5, 74))
        self.assertEqual((values["GV2"]["value"], values["GV2"]["uom"]), (0.74, 72))

    def test_missing_or_invalid_calibration_blocks_before_usb(self):
        for calibration, error in ((None, None), (None, "invalid multiplier")):
            node, service, _ = self.make_node(solar=True, calibration=calibration, error=error)
            self.assertFalse(node.refresh_measurements())
            self.assertEqual(service.calls, [])

    def test_calibration_change_marks_cached_result_stale_without_usb(self):
        node, service, _ = self.make_node(solar=True, calibration=SolarCalibration(250))
        self.assertTrue(node.refresh_measurements())
        node.set_calibration(SolarCalibration(300), None)
        values = {row["driver"]: row for row in node.drivers}
        self.assertEqual(values["GV0"]["value"], 0)
        self.assertEqual(values["ST"]["value"], 185.0)
        self.assertEqual(service.calls, [ROM])


if __name__ == "__main__":
    unittest.main()
