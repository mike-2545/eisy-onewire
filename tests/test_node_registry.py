from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from eisy_onewire.discovery_inventory import INVENTORY_KEY
from eisy_onewire.ds2409_transport import (
    BranchHop,
    Ds2409Branch,
    Ds2409State,
    Ds2409StatusInfo,
)
from eisy_onewire.ds2409_status import Ds2409StatusReport
from eisy_onewire.manual_discovery import ManualDiscoveryService
from eisy_onewire.inventory_evidence import (
    load_inventory_evidence,
    persist_inventory_evidence,
)
from eisy_onewire.node_registry import (
    CONFIG_NOTICE_KEY,
    DISCOVERY_NOTICE_KEY,
    DS2409_STATUS_NOTICE_KEY,
    OneWireNodeRegistry,
    TEMPERATURE_PRESENTATION_NOTICE_KEY,
)
from eisy_onewire.onewire_discovery import (
    DiscoveryReport,
    InvalidRom,
    OneWireDevice,
    dallas_crc8,
)
from eisy_onewire.refresh_configuration import REFRESH_CONFIGURATION_KEY
from eisy_onewire.temperature_presentation import (
    TEMPERATURE_PRESENTATION_KEY,
    TemperaturePresentation,
    TemperatureUnit,
)
from eisy_onewire.presentation_worker import PresentationCommandWorker
from eisy_onewire.topology_discovery import (
    EventFlagAcknowledgement,
    LocatedDevice,
    TopologyDiscoveryReport,
)


SENSOR_A = bytes.fromhex("2811223344556656")
SENSOR_TWO = bytes.fromhex("2822334455667727")
SENSOR_THREE = bytes.fromhex("28334455667788E7")
COUPLER = bytes.fromhex("1F5566778899AA38")
ADAPTER_ID = bytes.fromhex("814455667788999C")
DEFAULTS = {"_config_version": "1", SENSOR_A.hex().upper(): "Test Sensor"}


class Notices(dict[str, str]):
    def delete(self, key: str) -> None:
        self.pop(key, None)


class FakeInterface:
    def __init__(self, database_names=None) -> None:
        self.Notices = Notices()
        self.added: list[tuple[object, bool]] = []
        self.database_names = dict(database_names or {})

    def addNode(self, node: object, rename: bool = False) -> None:
        self.added.append((node, rename))

    def getNodeNameFromDb(self, address: str):
        return self.database_names.get(address)


class FakeCustom:
    def __init__(self, values=None) -> None:
        self.values: dict[str, object] = dict(values or {})
        self.saved = 0
        self.fail_next_set = False
        self.commit_before_failure = False

    def load(self, data, save=False) -> None:
        self.values = dict(data)
        self.saved += int(save)

    def update(self, data, save=False) -> None:
        self.values.update(data)
        self.saved += int(save)

    def __setitem__(self, key, value) -> None:
        if self.fail_next_set:
            self.fail_next_set = False
            if self.commit_before_failure:
                self.values[key] = value
            raise RuntimeError("customdata write failed")
        self.values[key] = value
        self.saved += 1

    def items(self):
        return self.values.items()


class Logger:
    def __init__(self) -> None:
        self.entries = []

    def info(self, message, *args) -> None:
        self.entries.append(("info", message % args))

    def warning(self, message, *args) -> None:
        self.entries.append(("warning", message % args))

    def error(self, message, *args) -> None:
        self.entries.append(("error", message % args))


class Controller:
    def __init__(
        self,
        poly,
        *,
        adapter_accessible,
        discover,
        read_ds2409_status,
        automatic_refresh_enabled=False,
        set_automatic_refresh=lambda enabled: False,
        temperature_presentation=None,
        set_temperature_unit=lambda unit: False,
        set_temperature_precision=lambda places: False,
    ) -> None:
        self.poly = poly
        self.address = "onewire"
        self.primary = "onewire"
        self.name = "1-Wire Controller"
        self.adapter_accessible = adapter_accessible
        self.discover = discover
        self.read_ds2409_status = read_ds2409_status
        self.automatic_refresh_enabled = automatic_refresh_enabled
        self.set_automatic_refresh = set_automatic_refresh
        self.temperature_presentation = temperature_presentation
        self.set_temperature_unit = set_temperature_unit
        self.set_temperature_precision = set_temperature_precision

    def set_automatic_refresh_enabled(self, enabled) -> None:
        self.automatic_refresh_enabled = enabled

    def set_temperature_presentation(self, presentation) -> None:
        self.temperature_presentation = presentation


class Sensor:
    def __init__(self, poly, **kwargs) -> None:
        self.poly = poly
        self.__dict__.update(kwargs)
        self.drivers = [{"driver": "GV0", "value": 0, "uom": 2}]

    def set_branch_path(self, branch_path) -> None:
        self.branch_path = branch_path

    def set_branch_target(
        self, branch_path, target, *, event_clear_allowed, error
    ) -> None:
        self.branch_path = branch_path
        self.branch_target = target
        self.branch_event_clear_allowed = event_clear_allowed
        self.branch_refresh_error = error

    def set_desired_refresh_interval(self, seconds) -> None:
        self.refresh_interval_seconds = seconds

    def set_temperature_presentation(self, presentation) -> None:
        self.temperature_presentation = presentation

    def refresh_temperature(self) -> bool:
        self.scheduled_refresh_calls = getattr(self, "scheduled_refresh_calls", 0) + 1
        return True

    def scheduled_refresh_temperature(self, *, branch_event_clear_allowed) -> bool:
        self.scheduled_branch_clear_allowed = branch_event_clear_allowed
        self.scheduled_refresh_calls = getattr(self, "scheduled_refresh_calls", 0) + 1
        return True

    def last_refresh_error(self):
        return None


class SchedulerFactory:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.configurations = []

    def __call__(self, logger, notices):
        del logger, notices
        factory = self

        class Scheduler:
            def start(self):
                factory.started += 1

            def stop(self):
                factory.stopped += 1

            def configure(self, **kwargs):
                factory.configurations.append(kwargs)

        return Scheduler()


class ImmediatePresentationWorker:
    """Keep registry unit tests deterministic; worker behavior is tested separately."""

    def __init__(self, logger, processor) -> None:
        del logger
        self.processor = processor
        self.started = False

    def start(self) -> None:
        self.started = True

    def enqueue(self, intent) -> bool:
        if not self.started:
            return False
        self.processor(intent)
        return True

    def stop(self) -> None:
        self.started = False

    def wait_idle(self, timeout_seconds) -> bool:
        del timeout_seconds
        return True


class DiscoveryFactory:
    def __init__(
        self, result: DiscoveryReport | TopologyDiscoveryReport | Exception
    ) -> None:
        self.result = result
        self.calls = 0
        self.acknowledge_event_flags = None
        self.acknowledgements_before_error = ()

    def __call__(self, adapter_lock):
        def search(**kwargs):
            self.acknowledge_event_flags = kwargs.get("acknowledge_event_flags")
            self.calls += 1
            if isinstance(self.result, Exception):
                callback = kwargs.get("on_event_flags_cleared")
                if self.acknowledgements_before_error and callback is not None:
                    callback(self.acknowledgements_before_error)
                raise self.result
            return self.result

        return ManualDiscoveryService(
            adapter_lock,
            search=search,
            clock=lambda: datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc),
        )


class StatusFactory:
    def __init__(self, result: Ds2409StatusInfo | Exception) -> None:
        self.result = result
        self.calls: list[tuple[bytes, bytes]] = []

    def __call__(self, adapter_lock):
        del adapter_lock
        factory = self

        class Service:
            def run_once(self, adapter_rom, coupler_rom):
                factory.calls.append((adapter_rom, coupler_rom))
                if isinstance(factory.result, Exception):
                    raise factory.result
                return Ds2409StatusReport(
                    adapter_rom,
                    coupler_rom,
                    "2026-11-22T12:00:00Z",
                    factory.result,
                )

        return Service()


def report(*roms: bytes) -> DiscoveryReport:
    return DiscoveryReport(tuple(OneWireDevice(rom) for rom in roms), ())


class NodeRegistryTests(unittest.TestCase):
    def make_registry(
        self,
        discovery_result: DiscoveryReport | Exception | None = None,
        *,
        persisted=None,
        status_factory=None,
        database_names=None,
        scheduler_factory=None,
        inventory_evidence_path=None,
        presentation_profile_updater=None,
        presentation_worker_factory=ImmediatePresentationWorker,
        ds2438_sensor_class=None,
    ):
        interface = FakeInterface(database_names)
        params = FakeCustom()
        data = FakeCustom(persisted)
        logger = Logger()
        factory = DiscoveryFactory(discovery_result or report())
        scheduler = scheduler_factory or SchedulerFactory()
        registry = OneWireNodeRegistry(
            interface,
            logger,
            params,
            data,
            DEFAULTS,
            Controller,
            Sensor,
            ds2438_sensor_class=ds2438_sensor_class,
            discovery_factory=factory,
            status_factory=(status_factory or StatusFactory(RuntimeError("unused"))),
            scheduler_factory=scheduler,
            inventory_evidence_path=inventory_evidence_path,
            presentation_profile_updater=(
                presentation_profile_updater or (lambda presentation: None)
            ),
            presentation_worker_factory=presentation_worker_factory,
        )
        registry.test_scheduler_factory = scheduler
        return registry, interface, params, data, factory, logger

    @staticmethod
    def ready(registry: OneWireNodeRegistry) -> None:
        registry.handle_custom_parameters({})
        registry.handle_custom_data({})
        registry.activate(adapter_accessible=True)

    def test_profile_gate_registers_controller_before_legacy_child_without_scan(self) -> None:
        registry, interface, params, _, factory, _ = self.make_registry()
        registry.handle_custom_parameters({})
        registry.handle_custom_data({})
        self.assertEqual(interface.added, [])
        self.assertEqual(params.saved, 1)

        registry.activate(adapter_accessible=True)

        self.assertEqual(
            [node.address for node, _ in interface.added],
            ["onewire", "28112233445566"],
        )
        child = interface.added[1][0]
        self.assertEqual((child.primary, child.name), ("onewire", "Test Sensor"))
        self.assertFalse(any(rename for _, rename in interface.added))
        self.assertEqual(factory.calls, 0, "startup must not perform Search ROM")

    def test_temperature_preferences_persist_restart_and_never_scan(self) -> None:
        profile_updates = []
        registry, interface, _, data, factory, _ = self.make_registry(
            presentation_profile_updater=profile_updates.append
        )
        self.ready(registry)
        sensor = interface.added[1][0]

        self.assertTrue(registry.set_temperature_unit(TemperatureUnit.CELSIUS))
        self.assertTrue(registry.set_temperature_precision(3))
        self.assertEqual(factory.calls, 0)
        self.assertEqual(
            data.values[TEMPERATURE_PRESENTATION_KEY]["unit"], "celsius"
        )
        self.assertEqual(
            data.values[TEMPERATURE_PRESENTATION_KEY]["decimal_places"], 3
        )
        self.assertEqual(sensor.temperature_presentation.unit, TemperatureUnit.CELSIUS)
        self.assertEqual(len(profile_updates), 2)

        saves = data.saved
        self.assertTrue(registry.set_temperature_precision(3))
        self.assertEqual(data.saved, saves)
        self.assertEqual(len(profile_updates), 2)

        restarted_profiles = []
        restarted, restarted_interface, _, _, restarted_factory, _ = self.make_registry(
            persisted=data.values,
            presentation_profile_updater=restarted_profiles.append,
        )
        restarted.handle_custom_parameters({})
        restarted.handle_custom_data(data.values)
        restarted.activate(adapter_accessible=True)
        self.assertEqual(restarted_factory.calls, 0)
        self.assertEqual(len(restarted_profiles), 1)
        controller = restarted_interface.added[0][0]
        self.assertEqual(controller.temperature_presentation.decimal_places, 3)

    def test_serial_worker_evaluates_rapid_intents_against_latest_preference(self) -> None:
        updates = []
        registry, interface, _, data, factory, _ = self.make_registry(
            presentation_profile_updater=updates.append,
            presentation_worker_factory=PresentationCommandWorker,
        )
        self.ready(registry)
        sensor = interface.added[1][0]

        self.assertTrue(registry.set_temperature_unit(TemperatureUnit.CELSIUS))
        self.assertTrue(registry.set_temperature_precision(3))
        self.assertTrue(registry.set_temperature_unit(TemperatureUnit.FAHRENHEIT))
        self.assertTrue(registry.set_temperature_precision(2))
        self.assertTrue(registry.set_temperature_precision(2))
        self.assertTrue(registry.set_temperature_unit(TemperatureUnit.FAHRENHEIT))
        self.assertTrue(registry.wait_for_presentation_commands())

        self.assertEqual(
            [(item.unit.value, item.decimal_places) for item in updates],
            [
                ("celsius", 1),
                ("celsius", 3),
                ("fahrenheit", 3),
                ("fahrenheit", 2),
            ],
        )
        self.assertEqual(
            data.values[TEMPERATURE_PRESENTATION_KEY]["decimal_places"], 2
        )
        self.assertIs(sensor.temperature_presentation.unit, TemperatureUnit.FAHRENHEIT)
        self.assertEqual(sensor.temperature_presentation.decimal_places, 2)
        self.assertEqual(factory.calls, 0)
        registry.shutdown()

    def test_invalid_persisted_presentation_falls_back_without_usb_or_overwrite(self) -> None:
        malformed = {
            TEMPERATURE_PRESENTATION_KEY: {
                "schema_version": 1,
                "unit": "kelvin",
                "decimal_places": 9,
                "raw_celsius_by_rom": {},
            }
        }
        registry, interface, _, data, factory, _ = self.make_registry(
            persisted=malformed
        )
        registry.handle_custom_parameters({})
        registry.handle_custom_data(malformed)
        registry.activate(adapter_accessible=True)
        self.assertEqual(factory.calls, 0)
        self.assertEqual(data.values[TEMPERATURE_PRESENTATION_KEY], malformed[TEMPERATURE_PRESENTATION_KEY])
        notice = json.loads(interface.Notices["temperature_presentation"])
        self.assertEqual(notice["status"], "invalid_temperature_presentation")
        self.assertFalse(notice["usb_activity"])

    def test_profile_rejection_preserves_authoritative_preference_and_nodes(self) -> None:
        def reject(presentation):
            raise RuntimeError("profile rejected")

        registry, interface, _, data, factory, _ = self.make_registry(
            presentation_profile_updater=reject
        )
        self.ready(registry)
        sensor = interface.added[1][0]
        before = data.values[TEMPERATURE_PRESENTATION_KEY].copy()

        self.assertTrue(registry.set_temperature_unit(TemperatureUnit.CELSIUS))

        self.assertEqual(factory.calls, 0)
        self.assertEqual(data.values[TEMPERATURE_PRESENTATION_KEY], before)
        self.assertIs(sensor.temperature_presentation.unit, TemperatureUnit.FAHRENHEIT)
        notice = json.loads(interface.Notices["temperature_presentation"])
        self.assertIn("profile synchronization failed", notice["detail"])

    def test_profile_timeout_restores_previous_authoritative_profile_once(self) -> None:
        updates = []

        def timeout_then_restore(presentation):
            updates.append(presentation)
            if len(updates) == 1:
                raise TimeoutError("ack missing")

        registry, interface, _, data, factory, _ = self.make_registry(
            presentation_profile_updater=timeout_then_restore
        )
        self.ready(registry)

        self.assertTrue(registry.set_temperature_unit(TemperatureUnit.CELSIUS))

        self.assertEqual(factory.calls, 0)
        self.assertEqual(
            [item.unit for item in updates],
            [TemperatureUnit.CELSIUS, TemperatureUnit.FAHRENHEIT],
        )
        self.assertEqual(
            data.values[TEMPERATURE_PRESENTATION_KEY]["unit"], "fahrenheit"
        )
        notice = json.loads(interface.Notices[TEMPERATURE_PRESENTATION_NOTICE_KEY])
        self.assertEqual(
            notice["status"], "temperature_presentation_change_rejected_restored"
        )
        self.assertTrue(notice["profile_consistency_confirmed"])

    def test_persistence_failure_restores_profile_and_previous_customdata(self) -> None:
        updates = []
        registry, interface, _, data, _, _ = self.make_registry(
            presentation_profile_updater=updates.append
        )
        self.ready(registry)
        before = data.values[TEMPERATURE_PRESENTATION_KEY].copy()
        data.fail_next_set = True
        data.commit_before_failure = True

        self.assertTrue(registry.set_temperature_precision(3))

        self.assertEqual([item.decimal_places for item in updates], [3, 1])
        self.assertEqual(data.values[TEMPERATURE_PRESENTATION_KEY], before)
        notice = json.loads(interface.Notices[TEMPERATURE_PRESENTATION_NOTICE_KEY])
        self.assertTrue(notice["profile_consistency_confirmed"])

    def test_failed_restoration_latches_and_rejects_further_changes(self) -> None:
        updates = []

        def always_timeout(presentation):
            updates.append(presentation)
            raise TimeoutError("ack missing")

        registry, interface, _, data, _, _ = self.make_registry(
            presentation_profile_updater=always_timeout
        )
        self.ready(registry)
        before = data.values[TEMPERATURE_PRESENTATION_KEY].copy()

        self.assertTrue(registry.set_temperature_unit(TemperatureUnit.CELSIUS))
        self.assertFalse(registry.set_temperature_precision(3))

        self.assertEqual(len(updates), 2)
        self.assertEqual(data.values[TEMPERATURE_PRESENTATION_KEY], before)
        notice = json.loads(interface.Notices[TEMPERATURE_PRESENTATION_NOTICE_KEY])
        self.assertEqual(
            notice["status"], "temperature_presentation_consistency_unknown"
        )
        self.assertFalse(notice["further_changes_allowed"])
        self.assertIn("Restart eisy-onewire", notice["recovery"])

    def test_repeated_configuration_is_idempotent_and_name_edit_keeps_address(self) -> None:
        registry, interface, _, _, _, _ = self.make_registry()
        self.ready(registry)
        registry.handle_custom_parameters(DEFAULTS)
        registry.handle_custom_parameters(DEFAULTS)
        self.assertEqual(
            [node.address for node, _ in interface.added].count("28112233445566"), 1
        )

        registry.handle_custom_parameters(
            {"_config_version": "1", SENSOR_A.hex().upper(): "Garden"}
        )
        children = [
            node for node, _ in interface.added if node.address == "28112233445566"
        ]
        self.assertEqual([node.name for node in children], ["Test Sensor", "Garden"])
        self.assertEqual({node.address for node in children}, {"28112233445566"})

    def test_invalid_update_registers_no_partial_children(self) -> None:
        registry, interface, _, _, _, _ = self.make_registry()
        registry.handle_custom_data({})
        registry.activate(adapter_accessible=False)
        registry.handle_custom_parameters({"_config_version": "1", "bad": "Broken"})
        self.assertEqual([node.address for node, _ in interface.added], ["onewire"])
        self.assertIn(CONFIG_NOTICE_KEY, interface.Notices)

    def test_mixed_five_rom_scan_adds_three_sensors_and_inventories_other_families(self) -> None:
        registry, interface, _, data, factory, _ = self.make_registry(
            report(SENSOR_A, SENSOR_TWO, SENSOR_THREE, COUPLER, ADAPTER_ID)
        )
        self.ready(registry)

        controller = interface.added[0][0]
        self.assertTrue(controller.discover())

        children = [node for node, _ in interface.added if node.address != "onewire"]
        self.assertEqual(factory.calls, 1)
        self.assertFalse(factory.acknowledge_event_flags)
        self.assertEqual(
            {node.address for node in children},
            {"28112233445566", "28223344556677", "28334455667788"},
        )
        self.assertEqual(
            {node.name for node in children},
            {
                "Test Sensor",
                "DS18B20 28.223344556677",
                "DS18B20 28.334455667788",
            },
        )
        self.assertTrue(
            all(node.adapter_lock is registry.adapter_lock for node in children)
        )
        self.assertNotIn("1f5566778899aa", {node.address for node in children})
        self.assertNotIn("81445566778899", {node.address for node in children})
        self.assertEqual(data.saved, 3)
        self.assertEqual(len(data.values[INVENTORY_KEY]["devices"]), 5)

        notice = json.loads(interface.Notices[DISCOVERY_NOTICE_KEY])
        self.assertEqual(notice["status"], "partial_topology")
        self.assertEqual(notice["branches_not_scanned"], [COUPLER.hex().upper()])
        self.assertFalse(notice["discovery_complete"])
        self.assertEqual(
            {item["kind"] for item in notice["known_devices"]},
            {
                "ds18b20_temperature",
                "ds2409_coupler",
                "ds9490_identification",
            },
        )

    def test_duplicate_manual_scan_is_idempotent(self) -> None:
        registry, interface, _, data, factory, _ = self.make_registry(
            report(SENSOR_A, SENSOR_TWO)
        )
        self.ready(registry)

        self.assertTrue(registry.discover_devices())
        self.assertTrue(registry.discover_devices())

        addresses = [node.address for node, _ in interface.added]
        self.assertEqual(addresses.count("28112233445566"), 1)
        self.assertEqual(addresses.count("28223344556677"), 1)
        self.assertEqual(factory.calls, 2)
        self.assertEqual(data.saved, 4)

    def test_restart_restores_last_known_nodes_stale_without_search(self) -> None:
        first, _, _, data, _, _ = self.make_registry(report(SENSOR_A, SENSOR_TWO))
        self.ready(first)
        self.assertTrue(first.discover_devices())
        persisted = dict(data.values)

        registry, interface, _, _, factory, _ = self.make_registry(
            RuntimeError("must not run"), persisted=persisted
        )
        registry.handle_custom_parameters(DEFAULTS)
        registry.handle_custom_data(persisted)
        registry.activate(adapter_accessible=True)

        self.assertEqual(factory.calls, 0)
        self.assertEqual(
            [node.address for node, _ in interface.added],
            ["onewire", "28112233445566", "28223344556677"],
        )
        notice = json.loads(interface.Notices[DISCOVERY_NOTICE_KEY])
        self.assertEqual(notice["status"], "last_known_inventory")
        self.assertEqual(
            notice["current_reachability"], "unknown_until_manual_discovery"
        )

    def test_live_empty_last_scan_restores_nodes_stale_without_search(self) -> None:
        timestamp_first = "2026-11-22T23:58:54.576548Z"
        timestamp_last = "2026-11-23T00:00:11.688713Z"
        persisted = {
            INVENTORY_KEY: {
                "schema_version": 1,
                "devices": [
                    {
                        "rom": rom.hex().upper(),
                        "family_code": f"0x{rom[0]:02X}",
                        "first_seen": timestamp_first,
                        "last_seen": timestamp_last,
                    }
                    for rom in (
                        COUPLER,
                        SENSOR_A,
                        SENSOR_TWO,
                        SENSOR_THREE,
                        ADAPTER_ID,
                    )
                ],
                "last_scan": {},
            },
            "unrelated": {"preserved": True},
        }
        registry, interface, _, data, factory, _ = self.make_registry(
            RuntimeError("startup must not search"), persisted=persisted
        )
        registry.handle_custom_parameters(
            {
                "_config_version": "1",
                SENSOR_A.hex().upper(): "Test Sensor",
                SENSOR_THREE.hex().upper(): "Lab Probe",
            }
        )
        registry.handle_custom_data(persisted)

        registry.activate(adapter_accessible=True)

        self.assertEqual(factory.calls, 0)
        nodes = {node.address: node for node, _ in interface.added}
        self.assertEqual(
            set(nodes),
            {"onewire", "28112233445566", "28223344556677", "28334455667788"},
        )
        self.assertEqual(nodes["28112233445566"].name, "Test Sensor")
        self.assertEqual(nodes["28334455667788"].name, "Lab Probe")
        self.assertEqual(nodes["28223344556677"].name, "DS18B20 28.223344556677")
        for address, node in nodes.items():
            if address == "onewire":
                continue
            self.assertEqual(node.primary, "onewire")
            self.assertEqual(node.drivers, [{"driver": "GV0", "value": 0, "uom": 2}])
        notice = json.loads(interface.Notices[DISCOVERY_NOTICE_KEY])
        self.assertEqual(notice["status"], "last_known_inventory")
        self.assertIsNone(notice["scan_timestamp"])
        self.assertIsNone(notice["reachable_roms_at_last_scan"])
        self.assertTrue(
            all(item["reachable_at_last_scan"] is None for item in notice["known_devices"])
        )
        self.assertTrue(data.values["unrelated"]["preserved"])

    def test_nonempty_malformed_last_scan_still_blocks_inventory_restore(self) -> None:
        persisted = {
            INVENTORY_KEY: {
                "schema_version": 1,
                "devices": [
                    {
                        "rom": SENSOR_TWO.hex().upper(),
                        "family_code": "0x28",
                        "first_seen": "2026-11-22T12:00:00Z",
                        "last_seen": "2026-11-22T12:00:00Z",
                    }
                ],
                "last_scan": {"timestamp": "2026-11-22T12:00:00Z"},
            }
        }
        registry, interface, _, _, factory, _ = self.make_registry(
            RuntimeError("startup must not search"), persisted=persisted
        )
        registry.handle_custom_parameters(DEFAULTS)
        registry.handle_custom_data(persisted)
        registry.activate(adapter_accessible=True)

        self.assertEqual(factory.calls, 0)
        self.assertEqual(
            [node.address for node, _ in interface.added],
            ["onewire", "28112233445566"],
        )
        notice = json.loads(interface.Notices[DISCOVERY_NOTICE_KEY])
        self.assertEqual(notice["status"], "invalid_persisted_inventory")

    def test_shared_lock_rejects_discovery_overlap_without_retry(self) -> None:
        registry, interface, _, _, factory, _ = self.make_registry(report(SENSOR_TWO))
        self.ready(registry)
        registry.adapter_lock.acquire()
        try:
            self.assertFalse(interface.added[0][0].discover())
        finally:
            registry.adapter_lock.release()

        self.assertEqual(factory.calls, 0)
        notice = json.loads(interface.Notices[DISCOVERY_NOTICE_KEY])
        self.assertEqual(notice["status"], "busy")
        self.assertEqual(notice["automatic_retries"], 0)

    def test_invalid_rom_response_fails_closed_without_inventory_or_node(self) -> None:
        invalid = DiscoveryReport((), (InvalidRom(b"bad", "ROM response is not 8 bytes"),))
        registry, interface, _, data, factory, _ = self.make_registry(invalid)
        self.ready(registry)

        self.assertFalse(registry.discover_devices())

        self.assertEqual(factory.calls, 1)
        self.assertEqual(data.saved, 1)
        self.assertEqual(
            [node.address for node, _ in interface.added],
            ["onewire", "28112233445566"],
        )
        notice = json.loads(interface.Notices[DISCOVERY_NOTICE_KEY])
        self.assertEqual(notice["status"], "error")

    def test_transport_error_and_empty_bus_are_distinct_and_never_retried(self) -> None:
        failed, failed_interface, _, failed_data, failed_factory, _ = self.make_registry(
            TimeoutError("bounded timeout")
        )
        self.ready(failed)
        self.assertFalse(failed.discover_devices())
        self.assertEqual(failed_factory.calls, 1)
        self.assertEqual(failed_data.saved, 1)
        self.assertEqual(
            json.loads(failed_interface.Notices[DISCOVERY_NOTICE_KEY])["status"],
            "error",
        )

        empty, empty_interface, _, empty_data, empty_factory, _ = self.make_registry(
            report()
        )
        self.ready(empty)
        self.assertTrue(empty.discover_devices())
        self.assertEqual(empty_factory.calls, 1)
        self.assertEqual(empty_data.saved, 2)
        notice = json.loads(empty_interface.Notices[DISCOVERY_NOTICE_KEY])
        self.assertEqual(notice["status"], "empty_bus")
        self.assertEqual(notice["reachable_roms_at_last_scan"], [])

    def test_name_collision_with_discovered_default_fails_before_persisting(self) -> None:
        registry, interface, _, data, _, _ = self.make_registry(report(SENSOR_TWO))
        registry.handle_custom_parameters(
            {
                "_config_version": "1",
                SENSOR_A.hex().upper(): "DS18B20 28.223344556677",
            }
        )
        registry.handle_custom_data({})
        registry.activate(adapter_accessible=True)

        self.assertFalse(registry.discover_devices())

        self.assertEqual(data.saved, 1)
        self.assertNotIn(
            "28223344556677", [node.address for node, _ in interface.added]
        )
        notice = json.loads(interface.Notices[DISCOVERY_NOTICE_KEY])
        self.assertEqual(notice["status"], "error")
        self.assertEqual(notice["error_type"], "SensorConfigurationError")

    def test_topology_adds_branch_ds18b20_and_inventories_ds2438_only(self) -> None:
        branch_sensor = bytes.fromhex("28778899AABBCC50")
        ds2438 = bytes.fromhex("2666778899AABB54")
        unsupported_prefix = bytes.fromhex("10010203040506")
        unsupported = unsupported_prefix + bytes([dallas_crc8(unsupported_prefix)])
        path = (BranchHop(COUPLER, Ds2409Branch.MAIN),)
        topology = TopologyDiscoveryReport(
            adapter_rom=ADAPTER_ID,
            devices=(
                LocatedDevice(OneWireDevice(ADAPTER_ID), ADAPTER_ID),
                LocatedDevice(OneWireDevice(COUPLER), ADAPTER_ID),
                LocatedDevice(OneWireDevice(SENSOR_A), ADAPTER_ID),
                LocatedDevice(OneWireDevice(branch_sensor), ADAPTER_ID, path),
                LocatedDevice(OneWireDevice(ds2438), ADAPTER_ID, path),
                LocatedDevice(OneWireDevice(unsupported), ADAPTER_ID, path),
            ),
            scanned_branches=(
                path,
                (BranchHop(COUPLER, Ds2409Branch.AUX),),
            ),
            branches_not_scanned=(),
        )
        registry, interface, _, data, _, _ = self.make_registry(topology)
        self.ready(registry)

        self.assertTrue(registry.discover_devices())

        addresses = {node.address for node, _ in interface.added}
        self.assertIn("28778899aabbcc", addresses)
        self.assertNotIn("2666778899aabb", addresses)
        self.assertNotIn(unsupported.hex()[:14].lower(), addresses)
        persisted = data.values[INVENTORY_KEY]
        stored_ds2438 = next(
            item for item in persisted["devices"] if item["rom"] == ds2438.hex().upper()
        )
        self.assertEqual(stored_ds2438["path"][0]["branch"], "main")
        notice = json.loads(interface.Notices[DISCOVERY_NOTICE_KEY])
        self.assertEqual(notice["status"], "ok")
        self.assertIn("1F5566778899AA38/main", notice["scanned_branches"])
        self.assertIn(
            "ds2438_inventory_only",
            {item["kind"] for item in notice["known_devices"]},
        )
        self.assertIn(
            "unsupported_family",
            {item["kind"] for item in notice["known_devices"]},
        )
        branch_node = next(
            node for node, _ in interface.added if node.address == "28778899aabbcc"
        )
        self.assertEqual(branch_node.branch_target.rom, branch_sensor)
        self.assertEqual(branch_node.branch_target.adapter_rom, ADAPTER_ID)
        self.assertEqual(branch_node.branch_target.path, path)
        self.assertFalse(branch_node.branch_event_clear_allowed)
        self.assertIsNone(branch_node.branch_refresh_error)

        restarted, restarted_interface, _, _, restarted_factory, _ = (
            self.make_registry(persisted=data.values)
        )
        restarted.handle_custom_parameters({})
        restarted.handle_custom_data(data.values)
        restarted.activate(adapter_accessible=True)
        restored = next(
            node
            for node, _ in restarted_interface.added
            if node.address == "28778899aabbcc"
        )
        self.assertEqual(restored.branch_target, branch_node.branch_target)
        self.assertEqual(restored.drivers, [{"driver": "GV0", "value": 0, "uom": 2}])
        self.assertEqual(restarted_factory.calls, 0)

    def test_explicit_hih4000_application_registers_inventory_backed_ds2438(self) -> None:
        ds2438 = bytes.fromhex("2666778899AABB54")
        path = (BranchHop(COUPLER, Ds2409Branch.MAIN),)
        topology = TopologyDiscoveryReport(
            adapter_rom=ADAPTER_ID,
            devices=(
                LocatedDevice(OneWireDevice(ADAPTER_ID), ADAPTER_ID),
                LocatedDevice(OneWireDevice(COUPLER), ADAPTER_ID),
                LocatedDevice(OneWireDevice(ds2438), ADAPTER_ID, path),
            ),
            scanned_branches=(path, (BranchHop(COUPLER, Ds2409Branch.AUX),)),
            branches_not_scanned=(),
        )
        registry, interface, _, _, _, _ = self.make_registry(
            topology, ds2438_sensor_class=Sensor
        )
        registry.handle_custom_parameters(
            {
                "_config_version": "1",
                "_ds2438_application_2666778899AABB54": "hih4000",
            }
        )
        registry.handle_custom_data({})
        registry.activate(adapter_accessible=True)
        self.assertTrue(registry.discover_devices())

        node = next(
            node for node, _ in interface.added if node.address == "2666778899aabb"
        )
        self.assertEqual(node.primary, "onewire")
        self.assertEqual(node.rom, ds2438)
        self.assertEqual(node.branch_target.adapter_rom, ADAPTER_ID)
        self.assertEqual(node.branch_target.path, path)
        self.assertEqual(node.refresh_interval_seconds, 0)
        scheduled = registry.test_scheduler_factory.configurations[-1]["targets"]
        target = next(item for item in scheduled if item.rom == ds2438)
        self.assertFalse(target.automatic_eligible)

    def test_valid_customdata_atomically_replaces_startup_invalid_evidence(self) -> None:
        ds2438 = bytes.fromhex("2666778899AABB54")
        path = (BranchHop(COUPLER, Ds2409Branch.MAIN),)
        topology = TopologyDiscoveryReport(
            adapter_rom=ADAPTER_ID,
            devices=(
                LocatedDevice(OneWireDevice(ADAPTER_ID), ADAPTER_ID),
                LocatedDevice(OneWireDevice(COUPLER), ADAPTER_ID),
                LocatedDevice(OneWireDevice(ds2438), ADAPTER_ID, path),
            ),
            scanned_branches=(path,),
            branches_not_scanned=(),
        )
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "inventory.json"
            registry, _, _, data, _, _ = self.make_registry(
                topology, inventory_evidence_path=evidence_path
            )
            with self.assertRaises(ValueError):
                load_inventory_evidence(evidence_path)

            self.ready(registry)
            self.assertTrue(registry.discover_devices())
            restored = load_inventory_evidence(evidence_path)

        self.assertEqual(restored.to_data(), data.values[INVENTORY_KEY])

    def test_mirror_update_remains_inside_adapter_lock_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "inventory.json"
            registry, _, _, _, _, _ = self.make_registry(
                report(SENSOR_TWO), inventory_evidence_path=evidence_path
            )
            self.ready(registry)

            def assert_locked(path, inventory):
                self.assertFalse(registry.adapter_lock.acquire(blocking=False))
                persist_inventory_evidence(path, inventory)

            with patch(
                "eisy_onewire.node_registry.persist_inventory_evidence",
                side_effect=assert_locked,
            ):
                self.assertTrue(registry.discover_devices())

    def test_mirror_write_failure_invalidates_old_evidence_and_reports_partial_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "inventory.json"
            registry, interface, _, data, _, _ = self.make_registry(
                report(SENSOR_TWO), inventory_evidence_path=evidence_path
            )
            self.ready(registry)
            old = load_inventory_evidence(evidence_path)
            self.assertEqual(old.devices, ())

            with patch(
                "eisy_onewire.node_registry.persist_inventory_evidence",
                side_effect=OSError("disk full"),
            ):
                self.assertFalse(registry.discover_devices())

            with self.assertRaises(ValueError):
                load_inventory_evidence(evidence_path)
            self.assertEqual(
                data.values[INVENTORY_KEY]["devices"][0]["rom"],
                SENSOR_TWO.hex().upper(),
            )
            notice = json.loads(interface.Notices[DISCOVERY_NOTICE_KEY])
            self.assertEqual(notice["status"], "partial_persistence")
            self.assertTrue(notice["inventory_changed"])
            self.assertEqual(notice["evidence_mirror"], "invalid")
            self.assertFalse(notice["nodes_added"])

    def test_existing_sensor_is_gated_when_topology_later_places_it_on_branch(self) -> None:
        branch_sensor = bytes.fromhex("28778899AABBCC50")
        registry, interface, _, _, factory, _ = self.make_registry(
            report(branch_sensor)
        )
        self.ready(registry)
        self.assertTrue(registry.discover_devices())
        address = branch_sensor[:7].hex()
        sensor = next(node for node, _ in interface.added if node.address == address)
        self.assertEqual(sensor.branch_path, ())

        path = (BranchHop(COUPLER, Ds2409Branch.MAIN),)
        factory.result = TopologyDiscoveryReport(
            adapter_rom=ADAPTER_ID,
            devices=(
                LocatedDevice(OneWireDevice(ADAPTER_ID), ADAPTER_ID),
                LocatedDevice(OneWireDevice(COUPLER), ADAPTER_ID),
                LocatedDevice(OneWireDevice(branch_sensor), ADAPTER_ID, path),
            ),
            scanned_branches=(
                path,
                (BranchHop(COUPLER, Ds2409Branch.AUX),),
            ),
            branches_not_scanned=(),
        )

        self.assertTrue(registry.discover_devices())

        self.assertEqual(sensor.branch_path, path)
        self.assertEqual(
            [node.address for node, _ in interface.added].count(address), 1
        )

    def test_explicit_event_policy_is_forwarded_and_evidence_is_persisted(self) -> None:
        acknowledgement = EventFlagAcknowledgement(COUPLER, 0x3F, True, True)
        topology = TopologyDiscoveryReport(
            adapter_rom=ADAPTER_ID,
            devices=(
                LocatedDevice(OneWireDevice(ADAPTER_ID), ADAPTER_ID),
                LocatedDevice(OneWireDevice(COUPLER), ADAPTER_ID),
                LocatedDevice(OneWireDevice(SENSOR_A), ADAPTER_ID),
            ),
            scanned_branches=(
                (BranchHop(COUPLER, Ds2409Branch.MAIN),),
                (BranchHop(COUPLER, Ds2409Branch.AUX),),
            ),
            branches_not_scanned=(),
            event_flags_acknowledged=(acknowledgement,),
        )
        registry, interface, _, data, factory, _ = self.make_registry(topology)
        registry.handle_custom_parameters(
            {
                "_config_version": "1",
                "_ds2409_event_flag_policy": "acknowledge",
                SENSOR_A.hex().upper(): "Test Sensor",
            }
        )
        registry.handle_custom_data({})
        registry.activate(adapter_accessible=True)

        self.assertTrue(registry.discover_devices())

        self.assertTrue(factory.acknowledge_event_flags)
        saved = data.values[INVENTORY_KEY]["last_scan"]
        self.assertEqual(
            saved["event_flags_acknowledged"],
            [
                {
                    "coupler_rom": COUPLER.hex().upper(),
                    "raw_status": "0x3F",
                    "main_event": True,
                    "aux_event": True,
                    "irreversible_loss": True,
                }
            ],
        )
        notice = json.loads(interface.Notices[DISCOVERY_NOTICE_KEY])
        self.assertTrue(notice["event_flags_acknowledged"][0]["irreversible_loss"])

    def test_manual_ds2409_status_reports_all_bits_without_discovery(self) -> None:
        status_factory = StatusFactory(
            Ds2409StatusInfo(
                Ds2409State.OFF,
                True,
                True,
                0x7F,
                True,
                True,
                control_association=Ds2409Branch.AUX,
            )
        )
        registry, interface, _, _, discovery, _ = self.make_registry(
            report(ADAPTER_ID, COUPLER), status_factory=status_factory
        )
        self.ready(registry)
        self.assertTrue(registry.discover_devices())
        discovery_calls = discovery.calls

        controller = interface.added[0][0]
        self.assertTrue(controller.read_ds2409_status())

        self.assertEqual(discovery.calls, discovery_calls)
        self.assertEqual(status_factory.calls, [(ADAPTER_ID, COUPLER)])
        notice = json.loads(interface.Notices[DS2409_STATUS_NOTICE_KEY])
        self.assertEqual(notice["raw_status"], "0x7F")
        self.assertEqual(notice["branch_state"], "off")
        self.assertTrue(notice["main"]["event_flag"])
        self.assertEqual(notice["main"]["sensed_level"], "high")
        self.assertTrue(notice["main"]["sensed_level_valid"])
        self.assertEqual(notice["control"]["mode"], "auto")
        self.assertEqual(notice["control"]["association"], "aux")
        self.assertTrue(notice["event_flags_preserved"])
        self.assertEqual(notice["mutating_ds2409_commands"], 0)

    def test_post_clear_failure_reports_irreversible_flags_without_inventory_commit(self) -> None:
        registry, interface, _, data, factory, logger = self.make_registry(
            TimeoutError("branch scan timed out")
        )
        factory.acknowledgements_before_error = (
            EventFlagAcknowledgement(COUPLER, 0x1F, True, False),
        )
        registry.handle_custom_parameters(
            {
                "_config_version": "1",
                "_ds2409_event_flag_policy": "acknowledge",
                SENSOR_A.hex().upper(): "Test Sensor",
            }
        )
        registry.handle_custom_data({})
        registry.activate(adapter_accessible=True)

        self.assertFalse(registry.discover_devices())

        self.assertEqual(data.saved, 1)
        notice = json.loads(interface.Notices[DISCOVERY_NOTICE_KEY])
        self.assertEqual(notice["status"], "error")
        self.assertTrue(notice["irreversible_event_loss"])
        self.assertEqual(
            notice["event_flags_acknowledged"][0]["raw_status"], "0x1F"
        )
        self.assertTrue(
            any("irreversibly cleared" in message for _, message in logger.entries)
        )


    def test_refresh_settings_are_atomic_restart_safe_and_do_not_scan(self) -> None:
        registry, interface, _, data, factory, _ = self.make_registry(
            report(SENSOR_A, SENSOR_TWO)
        )
        registry.handle_custom_parameters({})
        registry.handle_custom_data({"unrelated": {"preserved": True}})
        registry.activate(adapter_accessible=True)
        self.assertTrue(registry.discover_devices())
        sensor = next(
            node for node, _ in interface.added if node.address == "28223344556677"
        )
        controller = interface.added[0][0]
        saves_before = data.saved

        self.assertTrue(sensor.set_refresh_interval(60))
        self.assertTrue(controller.set_automatic_refresh(True))
        self.assertEqual(factory.calls, 1)
        saved = data.values[REFRESH_CONFIGURATION_KEY]
        self.assertTrue(saved["master_enabled"])
        row = next(
            item
            for item in saved["sensors"]
            if item["rom"] == SENSOR_TWO.hex().upper()
        )
        self.assertEqual(row["interval_seconds"], 60)
        self.assertTrue(data.values["unrelated"]["preserved"])

        self.assertTrue(sensor.set_refresh_interval(60))
        self.assertEqual(data.saved, saves_before + 2, "duplicate setting must not save")

        restarted, restarted_interface, _, restarted_data, restarted_factory, _ = (
            self.make_registry(
                persisted=data.values,
                database_names={
                    "28112233445566": "Test Sensor",
                    "28223344556677": "Lab Bench",
                },
            )
        )
        restarted.handle_custom_parameters({})
        restarted.handle_custom_data(data.values)
        restarted.activate(adapter_accessible=True)

        restored_sensor = next(
            node
            for node, _ in restarted_interface.added
            if node.address == "28223344556677"
        )
        restored_controller = restarted_interface.added[0][0]
        self.assertEqual(restored_sensor.name, "Lab Bench")
        self.assertEqual(restored_sensor.refresh_interval_seconds, 60)
        self.assertTrue(restored_controller.automatic_refresh_enabled)
        self.assertEqual(restarted_factory.calls, 0)
        self.assertTrue(restarted_data.values["unrelated"]["preserved"])
        self.assertFalse(any(rename for _, rename in restarted_interface.added))

        restarted.handle_custom_parameters(
            {
                "_config_version": "1",
                SENSOR_A.hex().upper(): "Test Sensor West",
            }
        )
        sensor_a_nodes = [
            (node, rename)
            for node, rename in restarted_interface.added
            if node.address == "28112233445566"
        ]
        self.assertEqual(sensor_a_nodes[-1][0].name, "Test Sensor West")
        self.assertTrue(sensor_a_nodes[-1][1])

    def test_malformed_refresh_settings_do_not_clear_inventory_or_nodes(self) -> None:
        first, _, _, data, _, _ = self.make_registry(report(SENSOR_A, SENSOR_TWO))
        self.ready(first)
        self.assertTrue(first.discover_devices())
        persisted = dict(data.values)
        persisted[REFRESH_CONFIGURATION_KEY] = {
            "schema_version": 1,
            "master_enabled": True,
            "sensors": [{"rom": "typo"}],
        }

        registry, interface, _, restarted_data, factory, _ = self.make_registry(
            persisted=persisted
        )
        registry.handle_custom_parameters({})
        registry.handle_custom_data(persisted)
        registry.activate(adapter_accessible=True)

        addresses = {node.address for node, _ in interface.added}
        self.assertIn("28112233445566", addresses)
        self.assertIn("28223344556677", addresses)
        self.assertIn("automatic_refresh_configuration", interface.Notices)
        self.assertEqual(restarted_data.saved, 0)
        self.assertEqual(factory.calls, 0)

    def test_each_sensor_interval_is_independent_and_policies_are_preserved(self) -> None:
        registry, interface, params, data, factory, _ = self.make_registry(
            report(SENSOR_A, SENSOR_TWO)
        )
        registry.handle_custom_parameters(
            {
                "_config_version": "1",
                "_ds2409_event_flag_policy": "acknowledge",
                "_ds2409_branch_read_event_flag_policy": "acknowledge",
                SENSOR_A.hex().upper(): "Test Sensor",
            }
        )
        registry.handle_custom_data({})
        registry.activate(adapter_accessible=True)
        self.assertTrue(registry.discover_devices())
        sensors = {
            node.address: node
            for node, _ in interface.added
            if node.address.startswith("28")
        }

        self.assertTrue(sensors["28112233445566"].set_refresh_interval(30))
        self.assertTrue(sensors["28223344556677"].set_refresh_interval(300))

        rows = {
            item["address"]: item["interval_seconds"]
            for item in data.values[REFRESH_CONFIGURATION_KEY]["sensors"]
        }
        self.assertEqual(rows["28112233445566"], 30)
        self.assertEqual(rows["28223344556677"], 300)
        self.assertEqual(
            params.values["_ds2409_event_flag_policy"], "acknowledge"
        )
        self.assertEqual(
            params.values["_ds2409_branch_read_event_flag_policy"], "acknowledge"
        )
        self.assertEqual(factory.calls, 1, "configuration must not perform USB work")

    def test_schema_one_master_true_is_atomically_migrated_disarmed(self) -> None:
        first, _, _, data, _, _ = self.make_registry(report(SENSOR_A))
        self.ready(first)
        self.assertTrue(first.discover_devices())
        persisted = dict(data.values)
        persisted[REFRESH_CONFIGURATION_KEY] = {
            "schema_version": 1,
            "master_enabled": True,
            "sensors": [
                {
                    "rom": SENSOR_A.hex().upper(),
                    "address": "28112233445566",
                    "interval_seconds": 30,
                    "legacy_name_override": "Test Sensor",
                    "effective_name": "Test Sensor",
                }
            ],
        }

        registry, interface, _, migrated_data, factory, _ = self.make_registry(
            RuntimeError("upgrade must not touch USB"), persisted=persisted
        )
        registry.handle_custom_parameters(DEFAULTS)
        registry.handle_custom_data(persisted)
        registry.activate(adapter_accessible=True)

        saved = migrated_data.values[REFRESH_CONFIGURATION_KEY]
        self.assertEqual(saved["schema_version"], 2)
        self.assertFalse(saved["master_enabled"])
        self.assertFalse(saved["scheduler_armed"])
        self.assertFalse(interface.added[0][0].automatic_refresh_enabled)
        final = registry.test_scheduler_factory.configurations[-1]
        self.assertFalse(final["master_enabled"])
        self.assertFalse(final["armed"])
        self.assertEqual(len(final["targets"]), 1)
        self.assertEqual(factory.calls, 0)

    def test_explicit_enable_arms_and_schema_two_restart_resumes_intent(self) -> None:
        first, first_interface, _, data, _, _ = self.make_registry(report(SENSOR_A))
        self.ready(first)
        self.assertTrue(first.discover_devices())
        sensor = next(
            node for node, _ in first_interface.added if node.address != "onewire"
        )
        controller = first_interface.added[0][0]
        self.assertTrue(sensor.set_refresh_interval(60))
        self.assertTrue(controller.set_automatic_refresh(True))
        saved = data.values[REFRESH_CONFIGURATION_KEY]
        self.assertTrue(saved["master_enabled"])
        self.assertTrue(saved["scheduler_armed"])
        final = first.test_scheduler_factory.configurations[-1]
        self.assertTrue(final["master_enabled"])
        self.assertTrue(final["armed"])

        restarted, _, _, _, factory, _ = self.make_registry(
            RuntimeError("restart must not discover"), persisted=data.values
        )
        restarted.handle_custom_parameters(DEFAULTS)
        restarted.handle_custom_data(data.values)
        restarted.activate(adapter_accessible=True)
        restored = restarted.test_scheduler_factory.configurations[-1]
        self.assertTrue(restored["master_enabled"])
        self.assertTrue(restored["armed"])
        self.assertEqual(restored["targets"][0].interval_seconds, 60)
        self.assertEqual(factory.calls, 0)

        restarted.shutdown()
        self.assertEqual(restarted.test_scheduler_factory.stopped, 1)

    def test_branch_interval_is_visible_to_scheduler_but_never_grants_event_policy(self) -> None:
        branch_sensor = bytes.fromhex("28778899AABBCC50")
        path = (BranchHop(COUPLER, Ds2409Branch.MAIN),)
        topology = TopologyDiscoveryReport(
            adapter_rom=ADAPTER_ID,
            devices=(
                LocatedDevice(OneWireDevice(ADAPTER_ID), ADAPTER_ID),
                LocatedDevice(OneWireDevice(COUPLER), ADAPTER_ID),
                LocatedDevice(OneWireDevice(branch_sensor), ADAPTER_ID, path),
            ),
            scanned_branches=(path, (BranchHop(COUPLER, Ds2409Branch.AUX),)),
            branches_not_scanned=(),
        )
        registry, interface, params, _, _, _ = self.make_registry(topology)
        registry.handle_custom_parameters(
            {
                "_config_version": "1",
                "_ds2409_event_flag_policy": "acknowledge",
                "_ds2409_branch_read_event_flag_policy": "acknowledge",
            }
        )
        registry.handle_custom_data({})
        registry.activate(adapter_accessible=True)
        self.assertTrue(registry.discover_devices())
        node = next(
            node
            for node, _ in interface.added
            if node.address == branch_sensor[:7].hex()
        )
        self.assertTrue(node.set_refresh_interval(30))
        self.assertTrue(interface.added[0][0].set_automatic_refresh(True))

        target = registry.test_scheduler_factory.configurations[-1]["targets"][0]
        self.assertEqual(target.branch_path, path)
        self.assertFalse(target.automatic_eligible)
        self.assertEqual(params.values["_ds2409_event_flag_policy"], "acknowledge")
        self.assertEqual(
            params.values["_ds2409_branch_read_event_flag_policy"], "acknowledge"
        )

    def test_unattended_branch_policy_is_independent_and_controls_scheduler_callback(self) -> None:
        branch_sensor = bytes.fromhex("28778899AABBCC50")
        path = (BranchHop(COUPLER, Ds2409Branch.MAIN),)
        topology = TopologyDiscoveryReport(
            adapter_rom=ADAPTER_ID,
            devices=(
                LocatedDevice(OneWireDevice(ADAPTER_ID), ADAPTER_ID),
                LocatedDevice(OneWireDevice(COUPLER), ADAPTER_ID),
                LocatedDevice(OneWireDevice(branch_sensor), ADAPTER_ID, path),
            ),
            scanned_branches=(path, (BranchHop(COUPLER, Ds2409Branch.AUX),)),
            branches_not_scanned=(),
        )
        registry, interface, params, _, _, _ = self.make_registry(topology)
        registry.handle_custom_parameters(
            {
                "_config_version": "1",
                "_ds2409_event_flag_policy": "preserve",
                "_ds2409_branch_read_event_flag_policy": "preserve",
                "_ds2409_unattended_branch_access_policy": "acknowledge",
            }
        )
        registry.handle_custom_data({})
        registry.activate(adapter_accessible=True)
        self.assertTrue(registry.discover_devices())
        node = next(
            node for node, _ in interface.added
            if node.address == branch_sensor[:7].hex()
        )
        self.assertTrue(node.set_refresh_interval(30))
        self.assertTrue(interface.added[0][0].set_automatic_refresh(True))

        target = registry.test_scheduler_factory.configurations[-1]["targets"][0]
        self.assertTrue(target.automatic_eligible)
        self.assertTrue(target.refresh())
        self.assertTrue(node.scheduled_branch_clear_allowed)
        self.assertEqual(params.values["_ds2409_event_flag_policy"], "preserve")
        self.assertEqual(
            params.values["_ds2409_branch_read_event_flag_policy"], "preserve"
        )

    def test_malformed_runtime_callbacks_immediately_disable_active_scheduler(self) -> None:
        registry, interface, _, _, _, _ = self.make_registry(report(SENSOR_A))
        self.ready(registry)
        self.assertTrue(registry.discover_devices())
        sensor = next(
            node for node, _ in interface.added if node.address != "onewire"
        )
        self.assertTrue(sensor.set_refresh_interval(30))
        self.assertTrue(interface.added[0][0].set_automatic_refresh(True))
        self.assertTrue(
            registry.test_scheduler_factory.configurations[-1]["configuration_valid"]
        )

        registry.handle_custom_parameters(["malformed"])
        self.assertFalse(
            registry.test_scheduler_factory.configurations[-1]["configuration_valid"]
        )

        registry.handle_custom_parameters(DEFAULTS)
        registry.handle_custom_data(["malformed"])
        final = registry.test_scheduler_factory.configurations[-1]
        self.assertFalse(final["configuration_valid"])
        self.assertEqual(final["targets"], ())


if __name__ == "__main__":
    unittest.main()
