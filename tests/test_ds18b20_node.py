from __future__ import annotations

from copy import deepcopy
import threading
import unittest

from eisy_onewire.ds18b20 import PowerMode, ScratchpadError
from eisy_onewire.branch_temperature import (
    BranchTemperatureError,
    BranchTemperatureResult,
    BranchTemperatureTarget,
    EventLatchEvidence,
)
from eisy_onewire.ds18b20_node import (
    AVAILABILITY_DRIVER,
    REFRESH_COMMAND,
    REFRESH_INTERVAL_DRIVER,
    REFRESH_OFF_COMMAND,
    REFRESH_30_COMMAND,
    REFRESH_60_COMMAND,
    REFRESH_120_COMMAND,
    REFRESH_300_COMMAND,
    TEMPERATURE_DRIVER,
    UOM_BOOLEAN,
    UOM_FAHRENHEIT,
    create_ds18b20_node_class,
)
from eisy_onewire.ds2490_transport import UsbTransportError
from eisy_onewire.ds2409_transport import BranchHop, Ds2409Branch, Ds2409State
from eisy_onewire.temperature_service import FreshTemperature
from eisy_onewire.temperature_presentation import (
    TemperaturePresentation,
    TemperatureUnit,
    UOM_CELSIUS,
)

ROM = bytes.fromhex("2811223344556656")
ADDRESS = "28112233445566"
PRIMARY = "onewire"
ADAPTER = bytes.fromhex("814455667788999C")
PATH = (BranchHop(bytes.fromhex("1F5566778899AA38"), Ds2409Branch.MAIN),)


class FakeNotices(dict[str, str]):
    def delete(self, key: str) -> None:
        self.pop(key, None)


class FakePolyglot:
    def __init__(self, database=None) -> None:
        self.Notices = FakeNotices()
        self.database = database or {}
        self.added_nodes: list[list[dict[str, object]]] = []

    def getNodesFromDb(self, address: str) -> list[dict[str, object]]:
        return [
            {
                "driver": driver,
                "value": value,
                "uom": UOM_FAHRENHEIT if driver == TEMPERATURE_DRIVER else UOM_BOOLEAN,
            }
            for driver, value in self.database.get(address, {}).items()
        ]

    def addNode(self, node: object) -> None:
        self.added_nodes.append(deepcopy(node.drivers))


class FakeNode:
    drivers: list[dict[str, object]] = []

    def __init__(self, poly, primary: str, address: str, name: str) -> None:
        self.poly = poly
        self.primary = primary
        self.address = address
        self.name = name
        self.drivers = deepcopy(type(self).drivers)
        self.reports: list[tuple[str, object, int]] = []

    def getDriver(self, driver: str):
        return next((item["value"] for item in self.drivers if item["driver"] == driver), None)

    def setDriver(self, driver, value, report=True, force=False, uom=None) -> None:
        del force
        item = next(item for item in self.drivers if item["driver"] == driver)
        item["value"] = value
        if uom is not None:
            item["uom"] = uom
        if report:
            self.reportDriver(driver, True)

    def reportDriver(self, driver: str, force: bool) -> None:
        del force
        item = next(item for item in self.drivers if item["driver"] == driver)
        self.reports.append((driver, item["value"], int(item["uom"])))

    def runCmd(self, command: dict[str, str]):
        return self.commands[command["cmd"]](self, command)


class FakeLogger:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    def info(self, message: str, *args: object) -> None:
        self.entries.append(("info", message % args))

    def warning(self, message: str, *args: object) -> None:
        self.entries.append(("warning", message % args))

    def error(self, message: str, *args: object) -> None:
        self.entries.append(("error", message % args))


def measurement(rom: bytes = ROM, fahrenheit: float = 50.1125) -> FreshTemperature:
    return FreshTemperature(
        rom=rom,
        power_mode=PowerMode.EXTERNAL,
        resolution_bits=12,
        scratchpad=bytes.fromhex("A1004B467FFF0C10E8"),
        temperature_celsius=(fahrenheit - 32.0) * 5.0 / 9.0,
        temperature_fahrenheit=fahrenheit,
        conversion_maximum_seconds=0.750,
        conversion_polls=4,
    )


class FakeService:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[bytes] = []

    def read(self, rom: bytes) -> FreshTemperature:
        self.calls.append(rom)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeBranchService:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = []

    def read(self, target, *, allow_event_clear=False):
        self.calls.append((target, allow_event_clear))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class Ds18b20NodeTests(unittest.TestCase):
    def make_node(
        self,
        service,
        *,
        name="Test Sensor",
        database=None,
        lock=None,
        rom=ROM,
        branch_path=(),
        branch_target=None,
        branch_service=None,
        branch_event_clear_allowed=False,
        refresh_interval_seconds=0,
        set_refresh_interval=lambda seconds: False,
        temperature_presentation=TemperaturePresentation(),
        record_temperature=lambda rom, celsius: True,
    ):
        logger = FakeLogger()
        node_class = create_ds18b20_node_class(
            FakeNode,
            logger=logger,
            service_factory=lambda: service,
            branch_service_factory=lambda: branch_service,
        )
        poly = FakePolyglot(database)
        node = node_class(
            poly,
            primary=PRIMARY,
            address=rom[:7].hex(),
            name=name,
            rom=rom,
            adapter_lock=lock or threading.Lock(),
            branch_path=branch_path,
            branch_target=branch_target,
            branch_event_clear_allowed=branch_event_clear_allowed,
            refresh_interval_seconds=refresh_interval_seconds,
            set_refresh_interval=set_refresh_interval,
            temperature_presentation=temperature_presentation,
            record_temperature=record_temperature,
        )
        return node, poly, logger

    def test_identity_is_instance_configuration_and_initial_temperature_is_absent(self) -> None:
        node, _, _ = self.make_node(FakeService(measurement()), name="Garden")

        self.assertEqual((node.address, node.primary, node.name), (ADDRESS, PRIMARY, "Garden"))
        self.assertEqual(node.rom, ROM)
        self.assertEqual(node.id, "ds18b20")
        self.assertEqual(
            node.drivers,
            [
                {"driver": "GV0", "value": 0, "uom": 2},
                {"driver": "GV1", "value": 0, "uom": 25},
            ],
        )
        self.assertIsNone(node.getDriver("ST"))
        self.assertEqual(node.getDriver("GV0"), 0)
        self.assertIs(node.commands[REFRESH_COMMAND], type(node).refresh_temperature)

    def test_manual_refresh_uses_instance_rom_and_publishes_fahrenheit(self) -> None:
        service = FakeService(measurement(fahrenheit=68.225))
        node, poly, _ = self.make_node(service)

        self.assertTrue(node.runCmd({"cmd": REFRESH_COMMAND}))

        self.assertEqual(service.calls, [ROM])
        self.assertEqual(node.getDriver(TEMPERATURE_DRIVER), 68.2)
        self.assertEqual(node.getDriver(AVAILABILITY_DRIVER), 1)
        self.assertEqual(len(poly.added_nodes), 1)
        self.assertEqual(
            node.reports,
            [(TEMPERATURE_DRIVER, 68.2, UOM_FAHRENHEIT), (AVAILABILITY_DRIVER, 1, UOM_BOOLEAN)],
        )

    def test_crc_and_transport_failures_never_publish_temperature(self) -> None:
        for failure in (
            ScratchpadError("bad CRC"),
            UsbTransportError("bulk IN", -4),
            measurement(fahrenheit=300.0),
        ):
            with self.subTest(failure=type(failure).__name__):
                node, poly, _ = self.make_node(FakeService(failure))
                self.assertFalse(node.refresh_temperature())
                self.assertIsNone(node.getDriver(TEMPERATURE_DRIVER))
                self.assertEqual(node.getDriver(AVAILABILITY_DRIVER), 0)
                self.assertIn('"last_valid_temperature_stale": false', next(iter(poly.Notices.values())))

    def test_fixed_interval_commands_only_persist_configuration(self) -> None:
        service = FakeService(AssertionError("temperature service must remain idle"))
        calls = []
        node, _, _ = self.make_node(
            service,
            set_refresh_interval=lambda seconds: calls.append(seconds) or True,
        )

        for command, expected in (
            (REFRESH_30_COMMAND, 30),
            (REFRESH_60_COMMAND, 60),
            (REFRESH_120_COMMAND, 120),
            (REFRESH_300_COMMAND, 300),
            (REFRESH_OFF_COMMAND, 0),
        ):
            self.assertTrue(node.runCmd({"cmd": command}))
            self.assertEqual(node.getDriver(REFRESH_INTERVAL_DRIVER), expected)

        self.assertEqual(calls, [30, 60, 120, 300, 0])
        self.assertEqual(service.calls, [])

    def test_rejected_interval_command_does_not_change_visible_setting(self) -> None:
        node, _, _ = self.make_node(
            FakeService(measurement()),
            refresh_interval_seconds=60,
            set_refresh_interval=lambda seconds: False,
        )

        self.assertFalse(node.runCmd({"cmd": REFRESH_300_COMMAND}))
        self.assertEqual(node.getDriver(REFRESH_INTERVAL_DRIVER), 60)

    def test_restart_restores_numeric_temperature_but_marks_it_stale(self) -> None:
        node, _, _ = self.make_node(
            FakeService(ScratchpadError("bad CRC")),
            database={ADDRESS: {TEMPERATURE_DRIVER: "45.5", AVAILABILITY_DRIVER: 1}},
        )
        self.assertEqual(node.getDriver(TEMPERATURE_DRIVER), 45.5)
        self.assertEqual(node.getDriver(AVAILABILITY_DRIVER), 0)

    def test_shared_adapter_lock_rejects_overlap_across_two_sensor_nodes(self) -> None:
        shared_lock = threading.Lock()
        entered = threading.Event()
        release = threading.Event()

        class BlockingService:
            calls = 0

            def read(self, rom: bytes) -> FreshTemperature:
                self.calls += 1
                entered.set()
                release.wait(timeout=2)
                return measurement(rom)

        service = BlockingService()
        first, _, _ = self.make_node(service, lock=shared_lock)
        second_rom = bytes.fromhex("2822334455667727")
        second, _, logger = self.make_node(
            service, name="Second", lock=shared_lock, rom=second_rom
        )
        worker = threading.Thread(target=first.refresh_temperature)
        worker.start()
        self.assertTrue(entered.wait(timeout=1))
        self.assertFalse(second.refresh_temperature())
        self.assertEqual(service.calls, 1)
        self.assertEqual(second.getDriver(AVAILABILITY_DRIVER), 0)
        self.assertEqual(second.last_refresh_error(), "adapter is busy")
        release.set()
        worker.join(timeout=2)
        self.assertTrue(any(level == "warning" for level, _ in logger.entries))

    def test_branch_sensor_refresh_is_blocked_without_touching_transport(self) -> None:
        service = FakeService(measurement())
        path = (
            BranchHop(bytes.fromhex("1F5566778899AA38"), Ds2409Branch.MAIN),
        )
        node, poly, logger = self.make_node(
            service,
            database={ADDRESS: {TEMPERATURE_DRIVER: "45.5", AVAILABILITY_DRIVER: 1}},
            branch_path=path,
        )

        self.assertFalse(node.runCmd({"cmd": REFRESH_COMMAND}))

        self.assertEqual(service.calls, [])
        self.assertEqual(node.getDriver(TEMPERATURE_DRIVER), 45.5)
        self.assertEqual(node.getDriver(AVAILABILITY_DRIVER), 0)
        notice = next(iter(poly.Notices.values()))
        self.assertIn('"status": "branch_refresh_unsupported"', notice)
        self.assertIn("1F5566778899AA38/main", notice)
        self.assertTrue(any(level == "warning" for level, _ in logger.entries))

    def test_validated_branch_refresh_publishes_only_after_verified_restoration(self) -> None:
        target = BranchTemperatureTarget(ROM, ADAPTER, PATH)
        result = BranchTemperatureResult(
            measurement(), target, Ds2409State.OFF, Ds2409State.OFF, (), True, False
        )
        branch = FakeBranchService(result)
        root = FakeService(AssertionError("root path must not run"))
        node, _, _ = self.make_node(
            root,
            branch_path=PATH,
            branch_target=target,
            branch_service=branch,
            branch_event_clear_allowed=True,
        )

        self.assertTrue(node.runCmd({"cmd": REFRESH_COMMAND}))

        self.assertEqual(branch.calls, [(target, True)])
        self.assertEqual(root.calls, [])
        self.assertEqual(node.getDriver(TEMPERATURE_DRIVER), 50.1)
        self.assertEqual(node.getDriver(AVAILABILITY_DRIVER), 1)
        notice = next(iter(node.poly.Notices.values()))
        self.assertIn('"event_flags_cleared": false', notice)
        self.assertIn('"restoration_verified": true', notice)

        node.set_branch_target(
            PATH, target, event_clear_allowed=True, error=None
        )
        self.assertEqual(
            node.getDriver(AVAILABILITY_DRIVER), 1,
            "idempotent configuration reconciliation must preserve validity",
        )

    def test_presentation_change_reuses_raw_cache_without_usb_and_preserves_validity(self) -> None:
        service = FakeService(measurement(fahrenheit=75.425))
        saved = []
        node, _, _ = self.make_node(
            service,
            record_temperature=lambda rom, celsius: saved.append((rom, celsius)) or True,
        )
        self.assertTrue(node.refresh_temperature())
        calls_before = list(service.calls)
        reports_before = len(node.reports)

        node.set_temperature_presentation(
            TemperaturePresentation(TemperatureUnit.CELSIUS, 3)
        )

        self.assertEqual(service.calls, calls_before)
        self.assertEqual(node.getDriver(TEMPERATURE_DRIVER), 24.125)
        temperature = next(item for item in node.drivers if item["driver"] == "ST")
        self.assertEqual(temperature["uom"], UOM_CELSIUS)
        self.assertEqual(node.getDriver(AVAILABILITY_DRIVER), 1)
        self.assertEqual(saved, [(ROM, 24.125)])
        self.assertEqual(len(node.reports), reports_before + 1)

        node.set_temperature_presentation(
            TemperaturePresentation(TemperatureUnit.CELSIUS, 3)
        )
        self.assertEqual(len(node.reports), reports_before + 1)

    def test_stale_root_and_branch_values_convert_without_becoming_current(self) -> None:
        for branch_path in ((), PATH):
            with self.subTest(branch=bool(branch_path)):
                node, _, _ = self.make_node(
                    FakeService(AssertionError("USB must remain idle")),
                    database={ADDRESS: {TEMPERATURE_DRIVER: "50.1125", AVAILABILITY_DRIVER: 1}},
                    branch_path=branch_path,
                )
                node.set_temperature_presentation(
                    TemperaturePresentation(TemperatureUnit.CELSIUS, 2)
                )
                self.assertEqual(node.getDriver(TEMPERATURE_DRIVER), 10.06)
                self.assertEqual(node.getDriver(AVAILABILITY_DRIVER), 0)

    def test_scheduled_branch_refresh_uses_distinct_event_authorization(self) -> None:
        target = BranchTemperatureTarget(ROM, ADAPTER, PATH)
        result = BranchTemperatureResult(
            measurement(), target, Ds2409State.OFF, Ds2409State.OFF, (), True, False
        )
        branch = FakeBranchService(result)
        node, _, _ = self.make_node(
            FakeService(AssertionError("root path must not run")),
            branch_path=PATH,
            branch_target=target,
            branch_service=branch,
            branch_event_clear_allowed=False,
        )

        self.assertTrue(
            node.scheduled_refresh_temperature(branch_event_clear_allowed=True)
        )
        self.assertEqual(branch.calls, [(target, True)])

    def test_branch_failure_preserves_stale_temperature_and_latch_evidence(self) -> None:
        target = BranchTemperatureTarget(ROM, ADAPTER, PATH)
        failure = BranchTemperatureError(
            "scratchpad CRC failed",
            restoration_verified=True,
            event_evidence=(EventLatchEvidence("original", 0x3F, True, True),),
        )
        branch = FakeBranchService(failure)
        node, poly, _ = self.make_node(
            FakeService(measurement()),
            database={ADDRESS: {TEMPERATURE_DRIVER: "45.5", AVAILABILITY_DRIVER: 1}},
            branch_path=PATH,
            branch_target=target,
            branch_service=branch,
        )

        self.assertFalse(node.refresh_temperature())

        self.assertEqual(node.getDriver(TEMPERATURE_DRIVER), 45.5)
        self.assertEqual(node.getDriver(AVAILABILITY_DRIVER), 0)
        notice = next(iter(poly.Notices.values()))
        self.assertIn('"restoration_verified": true', notice)
        self.assertIn('"raw_status": "0x3F"', notice)


if __name__ == "__main__":
    unittest.main()
