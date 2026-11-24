from __future__ import annotations

from copy import deepcopy
import queue
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import udi_interface
except ImportError:
    udi_interface = None

from eisy_onewire.ds18b20_node import (
    AVAILABILITY_DRIVER,
    REFRESH_COMMAND,
    REFRESH_60_COMMAND,
    REFRESH_INTERVAL_DRIVER,
    TEMPERATURE_DRIVER,
    UOM_BOOLEAN,
    UOM_FAHRENHEIT,
    create_ds18b20_node_class,
)
from eisy_onewire.automatic_refresh import AutomaticRefreshScheduler, RefreshTarget
from eisy_onewire.ds18b20 import PowerMode
from eisy_onewire.branch_temperature import (
    BranchTemperatureResult,
    BranchTemperatureTarget,
)
from eisy_onewire.ds2409_transport import (
    BranchHop,
    Ds2409Branch,
    Ds2409State,
)
from eisy_onewire.discovery_inventory import (
    INVENTORY_KEY,
    DiscoveryInventory,
    update_inventory,
    update_topology_inventory,
)
from eisy_onewire.inventory_evidence import (
    load_inventory_evidence,
    persist_inventory_evidence,
    validate_ds2438_target,
)
from eisy_onewire.controller_node import (
    AUTOMATIC_REFRESH_ENABLE_COMMAND,
    DISCOVER_COMMAND,
    DS2409_STATUS_COMMAND,
    create_controller_node_class,
    USE_CELSIUS_COMMAND,
)
from eisy_onewire.refresh_configuration import (
    REFRESH_CONFIGURATION_KEY,
    RefreshConfiguration,
    SensorRefreshConfiguration,
)
from eisy_onewire.temperature_service import FreshTemperature
from eisy_onewire.plugin_wrapper import synchronize_json_profile
from eisy_onewire.onewire_discovery import DiscoveryReport, OneWireDevice
from eisy_onewire.topology_discovery import LocatedDevice, TopologyDiscoveryReport
from eisy_onewire.temperature_presentation import TemperaturePresentation, TemperatureUnit
from eisy_onewire.temperature_presentation import TEMPERATURE_PRESENTATION_KEY
from eisy_onewire.iox_profile import build_runtime_json_profile
from eisy_onewire.presentation_worker import (
    PresentationCommandWorker,
    PresentationIntent,
)
from eisy_onewire.ds2438_humidity import Hih4000Reading
from eisy_onewire.ds2438_node import create_ds2438_node_class
from eisy_onewire.ds2438_service import Ds2438Acquisition

ROM = bytes.fromhex("2811223344556656")
ADDRESS = "28112233445566"


class Notices(dict[str, str]):
    def delete(self, key: str) -> None:
        self.pop(key, None)


class RealNodePolyglotHarness:
    def __init__(self, persisted: list[dict[str, object]] | None = None) -> None:
        self.persisted = persisted or []
        self.Notices = Notices()
        self.sent: list[tuple[dict[str, object], str]] = []
        self.added: list[list[dict[str, object]]] = []

    def db_getNodeDrivers(self, address: str) -> list[dict[str, object]]:
        self.asserted_address = address
        return deepcopy(self.persisted)

    def getNodesFromDb(self, address: str) -> list[dict[str, object]]:
        self.asserted_address = address
        return deepcopy(self.persisted)

    def send(self, message: dict[str, object], channel: str) -> None:
        self.sent.append((message, channel))

    def addNode(self, node: object) -> None:
        self.added.append(deepcopy(node.drivers))


class Logger:
    def info(self, message: str, *args: object) -> None:
        pass

    def warning(self, message: str, *args: object) -> None:
        pass

    def error(self, message: str, *args: object) -> None:
        pass


class RealProfileHarness:
    """Minimum harness around the pinned Interface.updateJsonProfile method."""

    UPDATEPROFILEDONE = 24

    def __init__(self, response: str = "success") -> None:
        self.response = response
        self.callback = None
        self.sent: list[tuple[dict[str, object], str]] = []
        self.unsubscribed = False

    def pg3MinimumVersion(self, version: str) -> bool:
        self.minimum_version = version
        return True

    def _createRequestId(self) -> str:
        return "contract-request"

    def subscribe(self, topic: int, callback) -> None:
        self.subscribed_topic = topic
        self.callback = callback

    def unsubscribe(self, topic: int, callback) -> bool:
        self.unsubscribed = topic == self.UPDATEPROFILEDONE and callback is self.callback
        return self.unsubscribed

    def send(self, message: dict[str, object], channel: str) -> None:
        self.sent.append((deepcopy(message), channel))
        if self.response == "timeout":
            return
        request_id = message["updateprofile"]["requestId"]
        # A response for another request must not complete this operation.
        self.callback({"success": True, "requestId": "another-request"})
        self.callback(
            {
                "success": self.response == "success",
                "requestId": request_id,
                "message": "rejected by IoX",
            }
        )

    def updateJsonProfile(self, profile, options=None):
        return udi_interface.Interface.updateJsonProfile(self, profile, options)


class ImmediateTimeoutEvent:
    def set(self) -> None:
        pass

    def wait(self, timeout: float) -> bool:
        self.timeout = timeout
        return False


class ShortRealEvent:
    """Real event signaling with a short contract-test timeout."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def wait(self, timeout: float) -> bool:
        del timeout
        return self._event.wait(0.5)


class StoppableQueue(queue.Queue):
    STOP = object()

    def get(self, block=True, timeout=None):
        item = super().get(block, timeout)
        if item is self.STOP:
            super().task_done()
            raise SystemExit
        return item


class RealDispatcherHarness:
    """Run the pinned Interface input dispatcher without constructing MQTT."""

    UPDATEPROFILEDONE = 24

    def __init__(self, *, delayed_first_ack: bool = False) -> None:
        self.inQueue = StoppableQueue()
        self.nodes_internal = {}
        self.ns_config = {"requestId": False}
        self.sent = []
        self._request_number = 0
        self._send_number = 0
        self._delayed_first_ack = delayed_first_ack
        self._thread = threading.Thread(target=self._parseInput, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.inQueue.put(self.inQueue.STOP)
        self._thread.join(1.0)

    def _parseInput(self):
        return udi_interface.Interface._parseInput(self)

    def _handleInput(self, key, item, published):
        return udi_interface.Interface._handleInput(self, key, item, published)

    def pg3MinimumVersion(self, version: str) -> bool:
        del version
        return True

    def _createRequestId(self) -> str:
        self._request_number += 1
        return f"dispatcher-request-{self._request_number}"

    def subscribe(self, topic, callback, address=None):
        return udi_interface.Interface.subscribe(self, topic, callback, address)

    def unsubscribe(self, topic, callback, address=None):
        return udi_interface.Interface.unsubscribe(self, topic, callback, address)

    def updateJsonProfile(self, profile, options=None):
        return udi_interface.Interface.updateJsonProfile(self, profile, options)

    def send(self, message, channel):
        self.sent.append((deepcopy(message), channel))
        if "updateprofile" not in message:
            return
        self._send_number += 1
        response = {
            "success": True,
            "requestId": message["updateprofile"]["requestId"],
        }

        def acknowledge():
            self.inQueue.put({"updateprofile": response})

        if self._delayed_first_ack and self._send_number == 1:
            threading.Timer(0.7, acknowledge).start()
        else:
            acknowledge()


def fresh_temperature() -> FreshTemperature:
    return FreshTemperature(
        rom=ROM,
        power_mode=PowerMode.EXTERNAL,
        resolution_bits=12,
        scratchpad=bytes.fromhex("A1004B467FFF0C10E8"),
        temperature_celsius=10.0625,
        temperature_fahrenheit=50.1125,
        conversion_maximum_seconds=0.750,
        conversion_polls=3,
    )


@unittest.skipIf(udi_interface is None, "udi_interface 3.4.9 is not installed")
class UdiInterfaceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        if udi_interface is None:
            return
        self._pub = udi_interface.Interface.updateJsonProfile.__globals__["pub"]
        self._pub.topics.clear()
        self._pub.topic_list[udi_interface.Interface.UPDATEPROFILEDONE][2] = None

    def tearDown(self) -> None:
        if udi_interface is None:
            return
        self._pub.topics.clear()
        self._pub.topic_list[udi_interface.Interface.UPDATEPROFILEDONE][2] = None

    def make_node(
        self,
        polyglot,
        service_factory=lambda: None,
        branch_path=(),
        branch_target=None,
        branch_service_factory=lambda: None,
        branch_event_clear_allowed=False,
        refresh_interval_seconds=0,
        set_refresh_interval=lambda seconds: False,
        adapter_lock=None,
        temperature_presentation=TemperaturePresentation(),
        record_temperature=lambda rom, celsius: True,
    ):
        node_class = create_ds18b20_node_class(
            udi_interface.Node,
            logger=Logger(),
            service_factory=service_factory,
            branch_service_factory=branch_service_factory,
        )
        return node_class(
            polyglot,
            primary="onewire",
            address=ADDRESS,
            name="Configured Sensor",
            rom=ROM,
            adapter_lock=adapter_lock or threading.Lock(),
            branch_path=branch_path,
            branch_target=branch_target,
            branch_event_clear_allowed=branch_event_clear_allowed,
            refresh_interval_seconds=refresh_interval_seconds,
            set_refresh_interval=set_refresh_interval,
            temperature_presentation=temperature_presentation,
            record_temperature=record_temperature,
        )

    def test_actual_addnode_serialization_contains_no_null_temperature(self) -> None:
        polyglot = RealNodePolyglotHarness()
        node = self.make_node(polyglot)

        class InterfaceHarness:
            def __init__(self) -> None:
                self.nodes_internal: dict[str, object] = {}
                self.sent: list[tuple[dict[str, object], str]] = []

            def send(self, message, channel) -> None:
                self.sent.append((message, channel))

        interface = InterfaceHarness()
        udi_interface.Interface.addNode(interface, node, rename=False)

        payload = interface.sent[0][0]["addnode"][0]
        self.assertEqual(payload["address"], ADDRESS)
        self.assertEqual(payload["primaryNode"], "onewire")
        self.assertFalse(payload["rename"])
        self.assertEqual(
            payload["drivers"],
            [
                {"driver": AVAILABILITY_DRIVER, "value": 0, "uom": UOM_BOOLEAN},
                {"driver": REFRESH_INTERVAL_DRIVER, "value": 0, "uom": 25},
            ],
        )
        self.assertNotIn(TEMPERATURE_DRIVER, [d["driver"] for d in payload["drivers"]])
        self.assertFalse(any(d["value"] is None for d in payload["drivers"]))

    def test_actual_rename_and_database_name_survive_restart_contract(self) -> None:
        polyglot = RealNodePolyglotHarness()
        node = self.make_node(polyglot)

        node.rename("Garden Bin")

        self.assertEqual(node.name, "Garden Bin")
        self.assertEqual(polyglot.sent[-1][1], "command")
        self.assertEqual(
            polyglot.sent[-1][0]["renamenode"][0],
            {"address": ADDRESS, "name": "Garden Bin"},
        )

        class DatabaseHarness:
            db_getNodeDrivers = udi_interface.Interface.db_getNodeDrivers
            getNodesFromDb = udi_interface.Interface.getNodesFromDb

            def __init__(self) -> None:
                self._nodes = {
                    ADDRESS: {
                        "address": ADDRESS,
                        "name": "Garden Bin",
                        "drivers": [],
                    }
                }

        restarted = DatabaseHarness()
        self.assertEqual(
            udi_interface.Interface.getNodeNameFromDb(restarted, ADDRESS),
            "Garden Bin",
        )

    def test_actual_json_profile_api_correlates_successful_ack(self) -> None:
        interface = RealProfileHarness()
        profile = {
            "editors": [{"id": "temperature_f"}, {"id": "reading_current"}],
            "nodedefs": [{"id": "ds18b20"}],
        }

        response = synchronize_json_profile(interface, profile)

        self.assertEqual(response["requestId"], "contract-request")
        self.assertEqual(interface.minimum_version, "3.4.5")
        self.assertEqual(interface.subscribed_topic, interface.UPDATEPROFILEDONE)
        self.assertTrue(interface.unsubscribed)
        message, channel = interface.sent[0]
        self.assertEqual(channel, "system")
        self.assertEqual(message["updateprofile"]["requestId"], "contract-request")
        self.assertNotIn("requestId", profile)
        self.assertNotIn("delete", message["updateprofile"])

    def test_actual_json_profile_api_surfaces_failure(self) -> None:
        interface = RealProfileHarness(response="failure")

        with self.assertRaisesRegex(RuntimeError, "rejected by IoX"):
            synchronize_json_profile(
                interface,
                {"nodedefs": [{"id": "ds18b20"}]},
            )

    def test_actual_json_profile_api_timeout_fails_closed(self) -> None:
        interface = RealProfileHarness(response="timeout")
        globals_dict = udi_interface.Interface.updateJsonProfile.__globals__

        with patch.dict(globals_dict, {"Event": ImmediateTimeoutEvent}):
            with self.assertRaisesRegex(TimeoutError, "No update profile response"):
                synchronize_json_profile(
                    interface,
                    {"nodedefs": [{"id": "ds18b20"}]},
                )

    def test_actual_dispatcher_reproduces_inline_profile_ack_deadlock(self) -> None:
        interface = RealDispatcherHarness()
        errors = []
        completed = threading.Event()

        def inline_update(unit) -> bool:
            del unit
            try:
                synchronize_json_profile(
                    interface, {"nodedefs": [{"id": "onewire_controller"}]}
                )
            except Exception as exc:
                errors.append(exc)
            completed.set()
            return False

        node = create_controller_node_class(udi_interface.Node)(
            RealNodePolyglotHarness(),
            adapter_accessible=True,
            discover=lambda: False,
            read_ds2409_status=lambda: False,
            set_temperature_unit=inline_update,
        )
        interface.nodes_internal[node.address] = node
        globals_dict = udi_interface.Interface.updateJsonProfile.__globals__
        interface.start()
        try:
            with patch.dict(globals_dict, {"Event": ShortRealEvent}):
                interface.inQueue.put(
                    {
                        "command": {
                            "address": node.address,
                            "cmd": USE_CELSIUS_COMMAND,
                        }
                    }
                )
                self.assertTrue(completed.wait(1.0))
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], TimeoutError)
            self.assertEqual(len(interface.sent), 1)
        finally:
            interface.stop()

    def test_actual_dispatcher_worker_receives_correlated_ack(self) -> None:
        interface = RealDispatcherHarness()
        errors = []
        completed = threading.Event()

        def process(intent) -> None:
            try:
                synchronize_json_profile(
                    interface,
                    build_runtime_json_profile(
                        TemperaturePresentation(intent.value, 1)
                    ),
                )
            except Exception as exc:
                errors.append(exc)
            completed.set()

        worker = PresentationCommandWorker(Logger(), process)
        node = create_controller_node_class(udi_interface.Node)(
            RealNodePolyglotHarness(),
            adapter_accessible=True,
            discover=lambda: False,
            read_ds2409_status=lambda: False,
            set_temperature_unit=lambda unit: worker.enqueue(
                PresentationIntent.unit(unit)
            ),
        )
        interface.nodes_internal[node.address] = node
        globals_dict = udi_interface.Interface.updateJsonProfile.__globals__
        interface.start()
        worker.start()
        try:
            with patch.dict(globals_dict, {"Event": ShortRealEvent}):
                interface.inQueue.put(
                    {
                        "command": {
                            "address": node.address,
                            "cmd": USE_CELSIUS_COMMAND,
                        }
                    }
                )
                self.assertTrue(completed.wait(1.0))
            self.assertEqual(errors, [])
            self.assertEqual(len(interface.sent), 1)
            request = interface.sent[0][0]["updateprofile"]
            self.assertEqual(request["requestId"], "dispatcher-request-1")
        finally:
            worker.stop()
            interface.stop()

    def test_worker_ignores_late_ack_and_confirms_compensating_profile(self) -> None:
        interface = RealDispatcherHarness(delayed_first_ack=True)
        completed = threading.Event()
        errors = []
        candidate = TemperaturePresentation(TemperatureUnit.CELSIUS, 3)
        previous = TemperaturePresentation()

        def process(intent) -> None:
            del intent
            try:
                synchronize_json_profile(
                    interface, build_runtime_json_profile(candidate)
                )
            except TimeoutError:
                try:
                    synchronize_json_profile(
                        interface, build_runtime_json_profile(previous)
                    )
                except Exception as exc:
                    errors.append(exc)
            completed.set()

        worker = PresentationCommandWorker(Logger(), process)
        globals_dict = udi_interface.Interface.updateJsonProfile.__globals__
        interface.start()
        worker.start()
        try:
            with patch.dict(globals_dict, {"Event": ShortRealEvent}):
                self.assertTrue(worker.enqueue(PresentationIntent.precision(3)))
                self.assertTrue(completed.wait(1.0))
            self.assertEqual(errors, [])
            self.assertEqual(len(interface.sent), 2)
            self.assertNotEqual(
                interface.sent[0][0]["updateprofile"]["requestId"],
                interface.sent[1][0]["updateprofile"]["requestId"],
            )
        finally:
            worker.stop()
            interface.stop()

    def test_actual_runCmd_routes_refresh_and_registers_numeric_st(self) -> None:
        class Service:
            calls: list[bytes] = []

            def read(self, rom: bytes) -> FreshTemperature:
                self.calls.append(rom)
                return fresh_temperature()

        service = Service()
        polyglot = RealNodePolyglotHarness()
        node = self.make_node(polyglot, service_factory=lambda: service)

        node.runCmd({"cmd": REFRESH_COMMAND})

        self.assertEqual(service.calls, [ROM])
        self.assertEqual(len(polyglot.added), 1)
        by_driver = {item["driver"]: item for item in polyglot.added[0]}
        self.assertEqual(by_driver[TEMPERATURE_DRIVER]["value"], 50.1)
        self.assertEqual(by_driver[TEMPERATURE_DRIVER]["uom"], UOM_FAHRENHEIT)
        self.assertEqual(by_driver[AVAILABILITY_DRIVER]["value"], 1)
        self.assertEqual(
            [message["set"][0]["driver"] for message, _ in polyglot.sent],
            [TEMPERATURE_DRIVER, AVAILABILITY_DRIVER],
        )

    def test_actual_runCmd_routes_fixed_interval_without_temperature_access(self) -> None:
        calls = []
        polyglot = RealNodePolyglotHarness()
        node = self.make_node(
            polyglot,
            service_factory=lambda: self.fail("temperature service was constructed"),
            set_refresh_interval=lambda seconds: calls.append(seconds) or True,
        )

        node.runCmd({"cmd": REFRESH_60_COMMAND})

        self.assertEqual(calls, [60])
        self.assertEqual(node.getDriver(REFRESH_INTERVAL_DRIVER), 60)
        self.assertEqual(polyglot.sent[-1][0]["set"][0]["driver"], REFRESH_INTERVAL_DRIVER)

    def test_actual_runCmd_blocks_branch_refresh_before_service_access(self) -> None:
        calls = []

        class Service:
            def read(self, rom: bytes) -> FreshTemperature:
                calls.append(rom)
                return fresh_temperature()

        polyglot = RealNodePolyglotHarness()
        node = self.make_node(
            polyglot,
            service_factory=Service,
            branch_path=(
                BranchHop(
                    bytes.fromhex("1F5566778899AA38"), Ds2409Branch.MAIN
                ),
            ),
        )

        node.runCmd({"cmd": REFRESH_COMMAND})

        self.assertEqual(calls, [])
        self.assertEqual(node.getDriver(AVAILABILITY_DRIVER), 0)
        self.assertIn(
            "branch_refresh_unsupported", next(iter(polyglot.Notices.values()))
        )

    def test_actual_runCmd_routes_validated_branch_refresh(self) -> None:
        adapter = bytes.fromhex("814455667788999C")
        path = (
            BranchHop(bytes.fromhex("1F5566778899AA38"), Ds2409Branch.MAIN),
        )
        target = BranchTemperatureTarget(ROM, adapter, path)
        calls = []

        class BranchService:
            def read(self, actual, *, allow_event_clear=False):
                calls.append((actual, allow_event_clear))
                return BranchTemperatureResult(
                    fresh_temperature(),
                    target,
                    Ds2409State.MAIN,
                    Ds2409State.MAIN,
                    (),
                    False,
                    False,
                )

        polyglot = RealNodePolyglotHarness()
        node = self.make_node(
            polyglot,
            branch_path=path,
            branch_target=target,
            branch_service_factory=BranchService,
        )

        node.runCmd({"cmd": REFRESH_COMMAND})

        self.assertEqual(calls, [(target, False)])
        self.assertEqual(node.getDriver(TEMPERATURE_DRIVER), 50.1)
        self.assertEqual(node.getDriver(AVAILABILITY_DRIVER), 1)

    def test_actual_scheduled_branch_refresh_uses_only_supplied_unattended_policy(self) -> None:
        adapter = bytes.fromhex("814455667788999C")
        path = (
            BranchHop(bytes.fromhex("1F5566778899AA38"), Ds2409Branch.MAIN),
        )
        target = BranchTemperatureTarget(ROM, adapter, path)
        calls = []

        class BranchService:
            def read(self, actual, *, allow_event_clear=False):
                calls.append((actual, allow_event_clear))
                return BranchTemperatureResult(
                    fresh_temperature(),
                    target,
                    Ds2409State.OFF,
                    Ds2409State.OFF,
                    (),
                    allow_event_clear,
                    False,
                )

        polyglot = RealNodePolyglotHarness()
        node = self.make_node(
            polyglot,
            branch_path=path,
            branch_target=target,
            branch_event_clear_allowed=False,
            branch_service_factory=BranchService,
        )

        self.assertTrue(
            node.scheduled_refresh_temperature(branch_event_clear_allowed=True)
        )
        self.assertEqual(calls, [(target, True)])
        self.assertEqual(node.getDriver(AVAILABILITY_DRIVER), 1)

    def test_actual_node_serializes_runtime_uom_but_precision_comes_from_profile(self) -> None:
        class Service:
            def read(self, rom):
                return fresh_temperature()

        polyglot = RealNodePolyglotHarness()
        node = self.make_node(polyglot, service_factory=Service)
        self.assertTrue(node.refresh_temperature())
        before = len(polyglot.sent)

        selected = TemperaturePresentation(TemperatureUnit.CELSIUS, 3)
        node.set_temperature_presentation(selected)

        message = polyglot.sent[-1][0]["set"][0]
        self.assertEqual(len(polyglot.sent), before + 1)
        self.assertEqual(message["driver"], TEMPERATURE_DRIVER)
        self.assertEqual(message["value"], "10.062")
        self.assertEqual(message["uom"], 4)
        editor = next(
            item
            for item in build_runtime_json_profile(selected)["editors"]
            if item["id"] == "temperature"
        )
        self.assertEqual(editor["ranges"][0]["prec"], 3)
        self.assertNotIn("prec", message)

    def test_actual_runCmd_routes_parameterless_controller_discovery(self) -> None:
        calls = []
        node_class = create_controller_node_class(udi_interface.Node)
        node = node_class(
            RealNodePolyglotHarness(),
            adapter_accessible=True,
            discover=lambda: calls.append("discover") or True,
            read_ds2409_status=lambda: calls.append("status") or True,
        )

        node.runCmd({"cmd": DISCOVER_COMMAND})

        self.assertEqual(calls, ["discover"])

    def test_actual_runCmd_routes_parameterless_ds2409_status(self) -> None:
        calls = []
        node_class = create_controller_node_class(udi_interface.Node)
        node = node_class(
            RealNodePolyglotHarness(),
            adapter_accessible=True,
            discover=lambda: False,
            read_ds2409_status=lambda: calls.append("status") or True,
        )

        node.runCmd({"cmd": DS2409_STATUS_COMMAND})

        self.assertEqual(calls, ["status"])

    def test_actual_runCmd_routes_master_enable_without_polling(self) -> None:
        calls = []
        node_class = create_controller_node_class(udi_interface.Node)
        node = node_class(
            RealNodePolyglotHarness(),
            adapter_accessible=True,
            discover=lambda: self.fail("discovery must not run"),
            read_ds2409_status=lambda: self.fail("status must not run"),
            set_automatic_refresh=lambda enabled: calls.append(enabled) or True,
        )

        node.runCmd({"cmd": AUTOMATIC_REFRESH_ENABLE_COMMAND})

        self.assertEqual(calls, [True])
        self.assertEqual(node.getDriver("GV0"), 1)

    def test_actual_runCmd_routes_unit_selection_without_usb_activity(self) -> None:
        calls = []
        node = create_controller_node_class(udi_interface.Node)(
            RealNodePolyglotHarness(),
            adapter_accessible=True,
            discover=lambda: self.fail("unit selection must not discover"),
            read_ds2409_status=lambda: self.fail("unit selection must not access USB"),
            set_temperature_unit=lambda unit: calls.append(unit) or True,
        )

        node.runCmd({"cmd": USE_CELSIUS_COMMAND})

        self.assertEqual(calls, [TemperatureUnit.CELSIUS])

    def test_actual_node_scheduler_contract_uses_shared_lock_and_stale_driver(self) -> None:
        class Service:
            calls = []

            def read(self, rom):
                self.calls.append(rom)
                return fresh_temperature()

        class Clock:
            value = 0.0

            def monotonic(self):
                return self.value

        service = Service()
        clock = Clock()
        shared = threading.Lock()
        polyglot = RealNodePolyglotHarness()
        node = self.make_node(
            polyglot,
            service_factory=lambda: service,
            adapter_lock=shared,
            refresh_interval_seconds=30,
        )
        scheduler = AutomaticRefreshScheduler(
            Logger(), polyglot.Notices, monotonic=clock.monotonic
        )
        scheduler.configure(
            master_enabled=True,
            armed=True,
            adapter_accessible=True,
            configuration_valid=True,
            targets=(
                RefreshTarget(
                    ADDRESS,
                    ROM,
                    node.name,
                    30,
                    (),
                    node.refresh_temperature,
                    node.last_refresh_error,
                ),
            ),
        )

        shared.acquire()
        try:
            clock.value = 2.0
            self.assertTrue(scheduler.run_due_once())
        finally:
            shared.release()
        self.assertEqual(service.calls, [])
        self.assertEqual(node.getDriver(AVAILABILITY_DRIVER), 0)
        self.assertEqual(
            scheduler.snapshot()["sensors"][0]["last_error"], "adapter is busy"
        )

        clock.value = 32.0
        self.assertTrue(scheduler.run_due_once())
        self.assertEqual(service.calls, [ROM])
        self.assertEqual(node.getDriver(AVAILABILITY_DRIVER), 1)

    def test_actual_customdata_assignment_is_atomic_across_saves_and_restart(self) -> None:
        class CustomHarness:
            def __init__(self) -> None:
                self.sent = []

            def send(self, message, channel) -> None:
                self.sent.append((deepcopy(message), channel))

        harness = CustomHarness()
        custom_data = udi_interface.Custom(harness, "customdata")
        custom_data.load({"unrelated": {"preserved": True}})
        sensor_two = bytes.fromhex("2822334455667727")
        first = update_inventory(
            DiscoveryInventory(),
            DiscoveryReport((OneWireDevice(ROM),), ()),
            timestamp="2026-11-22T12:00:00Z",
        )
        custom_data[INVENTORY_KEY] = first.to_data()

        message, channel = harness.sent[-1]
        self.assertEqual(channel, "custom")
        self.assertEqual(message["set"][0]["key"], "customdata")
        self.assertTrue(message["set"][0]["value"]["unrelated"]["preserved"])
        self.assertEqual(
            message["set"][0]["value"][INVENTORY_KEY]["last_scan"]["timestamp"],
            "2026-11-22T12:00:00Z",
        )

        second = update_inventory(
            first,
            DiscoveryReport((OneWireDevice(ROM), OneWireDevice(sensor_two)), ()),
            timestamp="2026-11-22T13:00:00Z",
        )
        custom_data[INVENTORY_KEY] = second.to_data()
        saved = deepcopy(harness.sent[-1][0]["set"][0]["value"])
        self.assertEqual(
            saved[INVENTORY_KEY]["last_scan"]["timestamp"],
            "2026-11-22T13:00:00Z",
        )
        self.assertEqual(len(saved[INVENTORY_KEY]["devices"]), 2)
        self.assertTrue(saved["unrelated"]["preserved"])

        restarted = udi_interface.Custom(CustomHarness(), "customdata")
        restarted.load(saved)
        restored = DiscoveryInventory.from_data(restarted[INVENTORY_KEY])
        self.assertEqual(restored, second)
        self.assertEqual(restored.devices[0].first_seen, "2026-11-22T12:00:00Z")

    def test_actual_customdata_presentation_assignment_preserves_other_state(self) -> None:
        class CustomHarness:
            def __init__(self) -> None:
                self.sent = []

            def send(self, message, channel) -> None:
                self.sent.append((deepcopy(message), channel))

        harness = CustomHarness()
        custom_data = udi_interface.Custom(harness, "customdata")
        custom_data.load(
            {
                INVENTORY_KEY: {"preserved": True},
                REFRESH_CONFIGURATION_KEY: {"preserved": True},
                "unrelated": 7,
            }
        )
        selected = TemperaturePresentation(
            TemperatureUnit.CELSIUS, 2
        ).with_raw_celsius(ROM, 10.0625)
        custom_data[TEMPERATURE_PRESENTATION_KEY] = selected.to_data()

        saved = harness.sent[-1][0]["set"][0]["value"]
        self.assertEqual(saved[INVENTORY_KEY], {"preserved": True})
        self.assertEqual(saved[REFRESH_CONFIGURATION_KEY], {"preserved": True})
        self.assertEqual(saved["unrelated"], 7)
        self.assertEqual(
            TemperaturePresentation.from_data(saved[TEMPERATURE_PRESENTATION_KEY]),
            selected,
        )

    def test_actual_customdata_assignment_round_trips_schema_two_topology(self) -> None:
        class CustomHarness:
            def __init__(self) -> None:
                self.sent = []

            def send(self, message, channel) -> None:
                self.sent.append((deepcopy(message), channel))

        adapter = bytes.fromhex("814455667788999C")
        coupler = bytes.fromhex("1F5566778899AA38")
        branch_sensor = bytes.fromhex("28778899AABBCC50")
        path = (BranchHop(coupler, Ds2409Branch.MAIN),)
        topology = TopologyDiscoveryReport(
            adapter_rom=adapter,
            devices=(
                LocatedDevice(OneWireDevice(adapter), adapter),
                LocatedDevice(OneWireDevice(coupler), adapter),
                LocatedDevice(OneWireDevice(branch_sensor), adapter, path),
            ),
            scanned_branches=(
                path,
                (BranchHop(coupler, Ds2409Branch.AUX),),
            ),
            branches_not_scanned=(),
        )
        inventory = update_topology_inventory(
            DiscoveryInventory(), topology, timestamp="2026-11-22T14:00:00Z"
        )
        harness = CustomHarness()
        custom_data = udi_interface.Custom(harness, "customdata")
        custom_data.load({"unrelated": ["keep"]})

        custom_data[INVENTORY_KEY] = inventory.to_data()

        saved = harness.sent[-1][0]["set"][0]["value"]
        self.assertEqual(saved[INVENTORY_KEY]["schema_version"], 2)
        self.assertEqual(
            saved[INVENTORY_KEY]["last_scan"]["scanned_branches"],
            ["1F5566778899AA38/aux", "1F5566778899AA38/main"],
        )
        self.assertEqual(saved["unrelated"], ["keep"])
        restored = DiscoveryInventory.from_data(saved[INVENTORY_KEY])
        self.assertEqual(restored, inventory)

    def test_actual_customdata_topology_drives_exact_ds2438_evidence_gate(self) -> None:
        class CustomHarness:
            def __init__(self) -> None:
                self.sent = []

            def send(self, message, channel) -> None:
                self.sent.append((deepcopy(message), channel))

        adapter = bytes.fromhex("814455667788999C")
        coupler = bytes.fromhex("1F5566778899AA38")
        sensor = bytes.fromhex("2666778899AABB54")
        path = (BranchHop(coupler, Ds2409Branch.MAIN),)
        topology = TopologyDiscoveryReport(
            adapter_rom=adapter,
            devices=(
                LocatedDevice(OneWireDevice(adapter), adapter),
                LocatedDevice(OneWireDevice(coupler), adapter),
                LocatedDevice(OneWireDevice(sensor), adapter, path),
            ),
            scanned_branches=(path,),
            branches_not_scanned=(),
        )
        inventory = update_topology_inventory(
            DiscoveryInventory(), topology, timestamp="2026-11-22T14:00:00Z"
        )
        custom_data = udi_interface.Custom(CustomHarness(), "customdata")
        custom_data.load({})
        custom_data[INVENTORY_KEY] = inventory.to_data()
        restored = DiscoveryInventory.from_data(dict(custom_data.items())[INVENTORY_KEY])

        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "inventory.json"
            persist_inventory_evidence(evidence_path, restored)
            loaded = load_inventory_evidence(evidence_path)
            hop, timestamp = validate_ds2438_target(
                loaded,
                sensor_rom=sensor,
                adapter_rom=adapter,
                coupler_rom=coupler,
                branch=Ds2409Branch.MAIN,
            )

        self.assertEqual(hop, path[0])
        self.assertEqual(timestamp, "2026-11-22T14:00:00Z")

    def test_actual_customdata_refresh_assignment_preserves_inventory_and_unrelated_keys(self) -> None:
        class CustomHarness:
            def __init__(self) -> None:
                self.sent = []

            def send(self, message, channel) -> None:
                self.sent.append((deepcopy(message), channel))

        inv = update_inventory(
            DiscoveryInventory(),
            DiscoveryReport((OneWireDevice(ROM),), ()),
            timestamp="2026-11-23T12:00:00Z",
        )
        config = RefreshConfiguration(
            False,
            (
                SensorRefreshConfiguration(
                    ROM, ADDRESS, 120, "Test Sensor", "Test Sensor"
                ),
            ),
        )
        harness = CustomHarness()
        custom_data = udi_interface.Custom(harness, "customdata")
        custom_data.load({INVENTORY_KEY: inv.to_data(), "unrelated": "keep"})

        custom_data[REFRESH_CONFIGURATION_KEY] = config.to_data()

        saved = harness.sent[-1][0]["set"][0]["value"]
        self.assertEqual(saved[REFRESH_CONFIGURATION_KEY]["sensors"][0]["interval_seconds"], 120)
        self.assertEqual(saved[INVENTORY_KEY], inv.to_data())
        self.assertEqual(saved["unrelated"], "keep")

    def test_actual_node_restores_numeric_st_but_marks_it_stale(self) -> None:
        polyglot = RealNodePolyglotHarness(
            [
                {"driver": TEMPERATURE_DRIVER, "value": "45.5", "uom": 17},
                {"driver": AVAILABILITY_DRIVER, "value": 1, "uom": 2},
            ]
        )

        node = self.make_node(polyglot)

        self.assertEqual(node.getDriver(TEMPERATURE_DRIVER), 45.5)
        self.assertEqual(node.getDriver(AVAILABILITY_DRIVER), 0)
        self.assertEqual(polyglot.sent[-1][0]["set"][0]["driver"], AVAILABILITY_DRIVER)
        self.assertEqual(polyglot.sent[-1][0]["set"][0]["value"], "0")

    def test_actual_ds2438_command_contract_publishes_numeric_humidity(self) -> None:
        sensor_rom = bytes.fromhex("2666778899AABB54")

        class Service:
            calls = []

            def read(self, rom):
                self.calls.append(rom)
                return Ds2438Acquisition(
                    Hih4000Reading(rom, 23.875, 35.38285249177748, 1.98, 5.21)
                )

        service = Service()
        cls = create_ds2438_node_class(
            udi_interface.Node,
            logger=Logger(),
            root_service_factory=lambda: service,
        )
        polyglot = RealNodePolyglotHarness()
        node = cls(
            polyglot,
            primary="onewire",
            address="2666778899aabb",
            name="Humidity",
            rom=sensor_rom,
            adapter_lock=threading.Lock(),
        )

        node.runCmd({"cmd": "REFRESH"})

        self.assertEqual(service.calls, [sensor_rom])
        by_driver = {item["driver"]: item for item in polyglot.added[0]}
        self.assertEqual(by_driver["ST"]["value"], 75.0)
        self.assertEqual(by_driver["GV2"]["value"], 35.4)
        self.assertEqual(by_driver["GV2"]["uom"], 51)


if __name__ == "__main__":
    unittest.main()
