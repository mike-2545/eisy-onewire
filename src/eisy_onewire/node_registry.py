"""Profile-gated controller, discovery inventory, and sensor registration."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from .adapter_exclusion import AdapterAccessLock
from .adapter_binding import AdapterIdentityBinding
from .adapter_recovery import (
    ADAPTER_RECOVERY_CONFIGURATION_KEY,
    ADAPTER_RECOVERY_NOTICE_KEY,
    AdapterRecoveryConfigurationError,
    AdapterRecoveryPolicy,
    AdapterRecoveryWorker,
)
from .automatic_refresh import AutomaticRefreshScheduler, RefreshTarget
from .discovery_inventory import (
    DS18B20_FAMILY,
    DS2438_FAMILY,
    DS2409_FAMILY,
    DS9490_ID_FAMILY,
    INVENTORY_KEY,
    DiscoveryInventory,
    DiscoveryInventoryError,
    KnownDevice,
    TOPOLOGY_SCOPE,
    default_sensor_name,
    update_inventory,
    update_topology_inventory,
)
from .branch_temperature import BranchTemperatureTarget
from .ds2438_diagnostic import Ds2438BranchTarget
from .ds2438_humidity import (
    DS2438_MEASUREMENT_CACHE_KEY,
    Ds2438MeasurementCache,
    Hih4000Reading,
)
from .ds2438_analog import (
    DS2438_ANALOG_CACHE_KEY,
    Ds2438AnalogMeasurementCache,
    CachedAnalogReading,
    SOLAR_MULTIPLIER_UNASSIGNED,
)
from .ds2409_transport import branch_path_label
from .manual_discovery import AdapterBusyError, ManualDiscoveryService
from .inventory_evidence import (
    invalidate_inventory_evidence,
    persist_inventory_evidence,
)
from .ds2409_status import ManualDs2409StatusService
from .topology_discovery import EventFlagAcknowledgement, TopologyDiscoveryReport
from .node_identity import DEFAULT_CONTROLLER_ADDRESS
from .refresh_configuration import (
    REFRESH_CONFIGURATION_KEY,
    RefreshConfiguration,
    RefreshConfigurationError,
    resolved_sensor_configuration,
)
from .sensor_config import (
    CONFIG_VERSION,
    CONFIG_VERSION_KEY,
    DS2438_APPLICATION_PREFIX,
    DS2438_SOLAR_MULTIPLIER_PREFIX,
    DS2438_SOLAR_OFFSET_PREFIX,
    ConfiguredDs18b20,
    ConfiguredDs2438,
    Ds2438Application,
    Ds2409EventFlagPolicy,
    Ds2409UnattendedBranchPolicy,
    FriendlyNameOverride,
    SensorConfigurationError,
    parse_friendly_name_overrides,
    parse_ds2409_branch_read_event_flag_policy,
    parse_ds2409_event_flag_policy,
    parse_ds2409_unattended_branch_policy,
    parse_ds2438_applications,
)
from .temperature_presentation import (
    TEMPERATURE_PRESENTATION_KEY,
    TemperaturePresentation,
    TemperaturePresentationError,
    TemperatureUnit,
)
from .presentation_worker import (
    PresentationCommandWorker,
    PresentationIntent,
    PresentationIntentKind,
)
from .usb_detector import UsbDeviceDescriptor, enumerate_ds9490


CONFIG_NOTICE_KEY = "onewire_sensor_configuration"
FRIENDLY_NAME_NOTICE_KEY = "optional_sensor_names"
DISCOVERY_NOTICE_KEY = "onewire_discovery"
DS2409_STATUS_NOTICE_KEY = "ds2409_status"
REFRESH_CONFIGURATION_NOTICE_KEY = "automatic_refresh_configuration"
TEMPERATURE_PRESENTATION_NOTICE_KEY = "temperature_presentation"
DS2438_CACHE_NOTICE_KEY = "ds2438_measurement_cache"
DS2438_APPLICATION_NOTICE_KEY = "ds2438_application_parameters"
DS2438_ANALOG_CACHE_NOTICE_KEY = "ds2438_analog_measurement_cache"
DS2438_TRANSITION_NOTICE_KEY = "ds2438_application_transition"
ADAPTER_BINDING_NOTICE_KEY = "ds9490_adapter_identity"


class Logger(Protocol):
    def info(self, message: str, *args: object) -> None: ...

    def error(self, message: str, *args: object) -> None: ...

    def warning(self, message: str, *args: object) -> None: ...


class CustomParameters(Protocol):
    def load(self, data: object, save: bool = False) -> None: ...

    def update(self, data: object, save: bool = False) -> None: ...

    def __setitem__(self, key: str, value: object) -> None: ...

    def items(self) -> object: ...


class InventoryMirrorWriteError(RuntimeError):
    """PG3 customdata committed, but diagnostic evidence stayed invalid."""


class OneWireNodeRegistry:
    """Reconcile a validated configuration only after profile synchronization."""

    def __init__(
        self,
        interface: Any,
        logger: Logger,
        custom_parameters: CustomParameters,
        custom_data: CustomParameters,
        default_parameters: Mapping[str, str],
        controller_class: type,
        sensor_class: type,
        ds2438_sensor_class: type | None = None,
        ds2438_sensor_classes: Mapping[Ds2438Application, type] | None = None,
        discovery_factory: Callable[
            [threading.Lock], ManualDiscoveryService
        ] = ManualDiscoveryService,
        status_factory: Callable[
            [threading.Lock], ManualDs2409StatusService
        ] = ManualDs2409StatusService,
        scheduler_factory: Callable[
            [Logger, object], AutomaticRefreshScheduler
        ] = AutomaticRefreshScheduler,
        adapter_lock_factory: Callable[[], AdapterAccessLock] = AdapterAccessLock,
        inventory_evidence_path: Path | None = None,
        presentation_profile_updater: Callable[[TemperaturePresentation], None] = (
            lambda presentation: None
        ),
        presentation_worker_factory: Callable[
            [Logger, Callable[[PresentationIntent], None]], PresentationCommandWorker
        ] = PresentationCommandWorker,
        adapter_binding: AdapterIdentityBinding | None = None,
        adapter_recovery_worker_factory: Callable[..., AdapterRecoveryWorker] = (
            AdapterRecoveryWorker
        ),
        adapter_enumerator: Callable[[], tuple[UsbDeviceDescriptor, ...]] | None = None,
    ) -> None:
        self._interface = interface
        self._logger = logger
        self._custom = custom_parameters
        self._custom_data = custom_data
        self._defaults = dict(default_parameters)
        self._controller_class = controller_class
        self._sensor_class = sensor_class
        self._ds2438_sensor_class = ds2438_sensor_class
        self._ds2438_sensor_classes = dict(ds2438_sensor_classes or {})
        if ds2438_sensor_class is not None:
            self._ds2438_sensor_classes.setdefault(
                Ds2438Application.HIH4000, ds2438_sensor_class
            )
        self._adapter_lock = adapter_lock_factory()
        self._adapter_binding = adapter_binding
        if adapter_binding is not None:
            set_callback = getattr(adapter_binding, "set_invalidation_callback", None)
            if callable(set_callback):
                set_callback(self._handle_adapter_binding_invalidation)
        self._adapter_enumerator = adapter_enumerator or _enumerate_adapter_descriptors
        self._inventory_evidence_path = inventory_evidence_path
        self._presentation_profile_updater = presentation_profile_updater
        if inventory_evidence_path is not None:
            invalidate_inventory_evidence(
                inventory_evidence_path,
                "plugin has not loaded and validated discovery customdata",
            )
        self._discovery = discovery_factory(self._adapter_lock)
        self._ds2409_status = status_factory(self._adapter_lock)
        self._scheduler = scheduler_factory(logger, interface.Notices)
        self._scheduler_started = False
        self._state_lock = threading.RLock()
        self._profile_ready = False
        self._adapter_accessible = False
        self._descriptor_accessible = False
        self._configured: tuple[ConfiguredDs18b20, ...] | None = None
        self._configured_ds2438: tuple[ConfiguredDs2438, ...] | None = None
        self._friendly_name_overrides: dict[bytes, FriendlyNameOverride] = {}
        self._custom_parameters_loaded = False
        self._event_flag_policy = Ds2409EventFlagPolicy.PRESERVE
        self._branch_read_event_policy = Ds2409EventFlagPolicy.PRESERVE
        self._unattended_branch_policy = Ds2409UnattendedBranchPolicy.DISABLED
        self._inventory = DiscoveryInventory()
        self._inventory_loaded = False
        self._refresh_configuration = RefreshConfiguration()
        self._refresh_configuration_valid = False
        self._temperature_presentation = TemperaturePresentation()
        self._temperature_presentation_valid = True
        self._temperature_presentation_consistent = True
        self._ds2438_cache = Ds2438MeasurementCache()
        self._ds2438_cache_valid = True
        self._ds2438_analog_cache = Ds2438AnalogMeasurementCache()
        self._ds2438_analog_cache_valid = True
        self._registered: dict[str, object] = {}
        self._event_flags_cleared: tuple[EventFlagAcknowledgement, ...] = ()
        self._presentation_worker = presentation_worker_factory(
            logger, self._process_temperature_presentation_intent
        )
        self._adapter_recovery_policy = AdapterRecoveryPolicy()
        self._adapter_recovery_policy_valid = True
        self._adapter_recovery_policy_loaded = False
        self._startup_recovery_triggered = False
        self._adapter_recovery_worker = adapter_recovery_worker_factory(
            logger,
            authorized=self._adapter_recovery_authorized,
            has_accepted_identity=self._has_accepted_adapter_identity,
            already_verified=self._adapter_binding_verified,
            enumerate_candidates=self._adapter_enumerator,
            verify=self._automatic_verify_adapter_identity,
            publish=self._publish_adapter_recovery_status,
        )

    @property
    def adapter_lock(self) -> AdapterAccessLock:
        """Expose the one shared lock only for dependency wiring and contract tests."""

        return self._adapter_lock

    def handle_custom_parameters(self, raw: object) -> None:
        """Receive the complete eisy-ui custom-parameter dictionary."""

        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            with self._state_lock:
                self._configured = None
                self._configured_ds2438 = None
                self._fail_closed_registered_hardware_locked(
                    "custom parameters must be a dictionary"
                )
                self._configuration_failed("custom parameters must be a dictionary")
                if self._profile_ready:
                    self._configure_scheduler_locked()
            return
        with self._state_lock:
            self._custom.load(raw)
            self._custom_parameters_loaded = True
            values = dict(self._custom.items())
            if CONFIG_VERSION_KEY not in values:
                # First generic-controller startup only. The marker prevents a later user
                # deletion from silently recreating a sensor mapping.
                self._custom.update(self._defaults, save=True)
                values = dict(self._custom.items())
            try:
                if values.get(CONFIG_VERSION_KEY) != CONFIG_VERSION:
                    raise SensorConfigurationError(
                        f"{CONFIG_VERSION_KEY} must be {CONFIG_VERSION!r}"
                    )
                self._configured_ds2438 = parse_ds2438_applications(values)
                self._event_flag_policy = parse_ds2409_event_flag_policy(values)
                self._branch_read_event_policy = (
                    parse_ds2409_branch_read_event_flag_policy(values)
                )
                self._unattended_branch_policy = (
                    parse_ds2409_unattended_branch_policy(values)
                )
            except SensorConfigurationError as exc:
                self._configured = None
                self._configured_ds2438 = None
                self._fail_closed_registered_hardware_locked(str(exc))
                self._configuration_failed(str(exc))
                if self._profile_ready:
                    self._configure_scheduler_locked()
                return
            overrides, name_errors = parse_friendly_name_overrides(
                values, previous=self._friendly_name_overrides
            )
            self._friendly_name_overrides = {item.rom: item for item in overrides}
            self._configured = tuple(
                ConfiguredDs18b20(item.rom, item.address, item.name)
                for item in overrides
                if item.rom[0] == DS18B20_FAMILY
            )
            if name_errors:
                self._friendly_names_failed(name_errors)
            else:
                self._interface.Notices.delete(FRIENDLY_NAME_NOTICE_KEY)
            self._interface.Notices.delete(CONFIG_NOTICE_KEY)
            self._ensure_ds2438_application_parameters_locked()
            self._reconcile_locked()

    def handle_custom_data(self, raw: object) -> None:
        """Restore validated last-known inventory without touching the bus."""

        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            with self._state_lock:
                self._invalidate_inventory_evidence_locked(
                    "custom data must be a dictionary"
                )
                self._inventory_loaded = False
                self._inventory_failed("custom data must be a dictionary")
                if self._profile_ready:
                    self._configure_scheduler_locked()
            return
        with self._state_lock:
            self._custom_data.load(raw)
            values = dict(self._custom_data.items())
            try:
                self._ds2438_cache = Ds2438MeasurementCache.from_data(
                    values.get(DS2438_MEASUREMENT_CACHE_KEY)
                )
            except ValueError as exc:
                self._ds2438_cache = Ds2438MeasurementCache()
                self._ds2438_cache_valid = False
                self._interface.Notices[DS2438_CACHE_NOTICE_KEY] = json.dumps(
                    {"status": "invalid_ds2438_cache", "detail": str(exc)},
                    sort_keys=True,
                )
            else:
                self._ds2438_cache_valid = True
                self._interface.Notices.delete(DS2438_CACHE_NOTICE_KEY)
            try:
                self._ds2438_analog_cache = Ds2438AnalogMeasurementCache.from_data(
                    values.get(DS2438_ANALOG_CACHE_KEY)
                )
            except ValueError as exc:
                self._ds2438_analog_cache = Ds2438AnalogMeasurementCache()
                self._ds2438_analog_cache_valid = False
                self._interface.Notices[DS2438_ANALOG_CACHE_NOTICE_KEY] = json.dumps(
                    {"status": "invalid_ds2438_analog_cache", "detail": str(exc)},
                    sort_keys=True,
                )
            else:
                self._ds2438_analog_cache_valid = True
                self._interface.Notices.delete(DS2438_ANALOG_CACHE_NOTICE_KEY)
            try:
                self._inventory = DiscoveryInventory.from_data(values.get(INVENTORY_KEY))
            except DiscoveryInventoryError as exc:
                self._invalidate_inventory_evidence_locked(str(exc))
                self._inventory_loaded = False
                self._inventory_failed(str(exc))
                if self._profile_ready:
                    self._configure_scheduler_locked()
                return
            try:
                self._persist_inventory_evidence_locked(self._inventory)
            except OSError as exc:
                self._inventory_loaded = False
                self._inventory_failed(f"inventory evidence persistence failed: {exc}")
                if self._profile_ready:
                    self._configure_scheduler_locked()
                return
            self._inventory_loaded = True
            raw_recovery = values.get(ADAPTER_RECOVERY_CONFIGURATION_KEY)
            try:
                self._adapter_recovery_policy = AdapterRecoveryPolicy.from_data(
                    raw_recovery
                )
            except AdapterRecoveryConfigurationError as exc:
                self._adapter_recovery_policy = AdapterRecoveryPolicy()
                self._adapter_recovery_policy_valid = False
                self._adapter_recovery_policy_loaded = True
                self._publish_adapter_recovery_status(
                    {
                        "status": "invalid_configuration",
                        "detail": str(exc),
                        "authorized": False,
                        "identity_transactions": 0,
                        "automatic_retries": 0,
                    }
                )
            else:
                self._adapter_recovery_policy_valid = True
                self._adapter_recovery_policy_loaded = True
                if raw_recovery is None:
                    self._custom_data[ADAPTER_RECOVERY_CONFIGURATION_KEY] = (
                        self._adapter_recovery_policy.to_data()
                    )
                self._publish_recovery_authorization_locked()
            self._ensure_ds2438_application_parameters_locked()
            raw_presentation = values.get(TEMPERATURE_PRESENTATION_KEY)
            try:
                loaded_presentation = TemperaturePresentation.from_data(
                    raw_presentation
                )
            except TemperaturePresentationError as exc:
                self._temperature_presentation_valid = False
                self._temperature_presentation_failed(str(exc))
            else:
                try:
                    if (
                        self._profile_ready
                        and loaded_presentation != self._temperature_presentation
                    ):
                        self._presentation_profile_updater(loaded_presentation)
                except Exception as exc:
                    self._temperature_presentation_valid = False
                    self._temperature_presentation_failed(
                        "IoX profile synchronization failed while loading "
                        f"customdata: {type(exc).__name__}: {exc}"
                    )
                else:
                    self._temperature_presentation = loaded_presentation
                    self._temperature_presentation_valid = True
                    self._temperature_presentation_consistent = True
                    self._interface.Notices.delete(TEMPERATURE_PRESENTATION_NOTICE_KEY)
                    if raw_presentation is None:
                        # Atomic migration only; it performs no USB operation and
                        # leaves every existing customdata key untouched.
                        self._custom_data[TEMPERATURE_PRESENTATION_KEY] = (
                            self._temperature_presentation.to_data()
                        )
            raw_refresh = values.get(REFRESH_CONFIGURATION_KEY)
            try:
                self._refresh_configuration = RefreshConfiguration.from_data(
                    raw_refresh, self._inventory
                )
            except RefreshConfigurationError as exc:
                self._refresh_configuration = RefreshConfiguration()
                self._refresh_configuration_valid = False
                self._refresh_configuration_failed(str(exc))
            else:
                self._refresh_configuration_valid = True
                self._interface.Notices.delete(REFRESH_CONFIGURATION_NOTICE_KEY)
                if RefreshConfiguration.requires_migration(raw_refresh):
                    # Schema 1's master flag never authorized automatic USB
                    # activity. Persist schema 2 atomically and disarmed.
                    self._custom_data[REFRESH_CONFIGURATION_KEY] = (
                        self._refresh_configuration.to_data()
                    )
            self._report_restored_inventory_locked()
            self._reconcile_locked()
            self._maybe_trigger_startup_recovery_locked()

    def activate(self, *, adapter_accessible: bool) -> None:
        """Register nodes after the correlated runtime profile acknowledgment."""

        with self._state_lock:
            if self._temperature_presentation != TemperaturePresentation():
                # The startup profile was the backward-compatible Fahrenheit/
                # one-decimal default. Install the persisted global selection
                # before any node is registered or re-reported.
                self._presentation_profile_updater(self._temperature_presentation)
            self._temperature_presentation_consistent = True
            self._profile_ready = True
            self._descriptor_accessible = adapter_accessible
            # A descriptor/open probe is not durable identity evidence.  Production
            # passes a binding and remains disarmed until explicit verification.
            self._adapter_accessible = (
                adapter_accessible if self._adapter_binding is None else False
            )
            self._presentation_worker.start()
            self._adapter_recovery_worker.start()
            if not self._scheduler_started:
                self._scheduler.start()
                self._scheduler_started = True
            controller = self._controller_class(
                self._interface,
                adapter_accessible=self._adapter_accessible,
                discover=self.discover_devices,
                read_ds2409_status=self.read_ds2409_status,
                verify_adapter_identity=self.verify_adapter_identity,
                automatic_refresh_enabled=(
                    self._refresh_configuration.master_enabled
                    if self._refresh_configuration_valid
                    else False
                ),
                set_automatic_refresh=self.set_automatic_refresh,
                adapter_recovery_enabled=self._adapter_recovery_authorized(),
                set_adapter_recovery=self.set_adapter_recovery,
                temperature_presentation=self._temperature_presentation,
                set_temperature_unit=self.set_temperature_unit,
                set_temperature_precision=self.set_temperature_precision,
            )
            self._interface.addNode(controller, rename=False)
            self._registered[DEFAULT_CONTROLLER_ADDRESS] = controller
            if self._adapter_binding is not None:
                self._adapter_binding_failed(
                    "plugin restart invalidated the live adapter binding; descriptor "
                    "location is not identity proof"
                )
            self._reconcile_locked()
            self._maybe_trigger_startup_recovery_locked()

    def _reconcile_locked(self) -> None:
        if not self._profile_ready:
            return
        if self._configured is None:
            self._configure_scheduler_locked()
            return
        try:
            desired = self._desired_sensors_locked(self._inventory)
        except SensorConfigurationError as exc:
            self._configured = None
            self._configuration_failed(str(exc))
            self._configure_scheduler_locked()
            return
        inventory_by_rom = {item.rom: item for item in self._inventory.devices}
        legacy_names = {
            rom: item.name for rom, item in self._friendly_name_overrides.items()
        }
        updated_refresh = self._refresh_configuration
        refresh_changed = False
        controller = self._registered.get(DEFAULT_CONTROLLER_ADDRESS)
        if controller is not None:
            update_master = getattr(controller, "set_automatic_refresh_enabled", None)
            if update_master is not None:
                update_master(
                    updated_refresh.master_enabled
                    if self._refresh_configuration_valid
                    else False
                )
            update_presentation = getattr(
                controller, "set_temperature_presentation", None
            )
            if update_presentation is not None:
                update_presentation(self._temperature_presentation)
        for configured in desired:
            known = inventory_by_rom.get(configured.rom)
            registered_current = self._registered.get(configured.address)
            branch_path = known.path if known is not None else ()
            branch_target, branch_error = self._branch_target_locked(known)
            event_clear_allowed = (
                self._branch_read_event_policy
                is Ds2409EventFlagPolicy.ACKNOWLEDGE
            )
            interval_seconds = 0
            explicit_legacy_rename = False
            if (
                known is not None
                and known.family_code == DS18B20_FAMILY
                and self._refresh_configuration_valid
            ):
                previous = updated_refresh.sensor(configured.rom)
                legacy_name = legacy_names.get(configured.rom)
                resolved = resolved_sensor_configuration(
                    configured.rom,
                    legacy_name_override=legacy_name,
                    default_name=default_sensor_name(configured.rom),
                    previous=previous,
                )
                explicit_legacy_rename = (
                    previous is not None
                    and legacy_name is not None
                    and previous.legacy_name_override != legacy_name
                    and registered_current is not None
                )
                if resolved != previous:
                    updated_refresh = updated_refresh.with_sensor(resolved)
                    refresh_changed = True
                interval_seconds = resolved.interval_seconds
                configured = ConfiguredDs18b20(
                    configured.rom, configured.address, resolved.effective_name
                )
            current = registered_current
            if current is not None:
                if explicit_legacy_rename:
                    rename = getattr(current, "rename", None)
                    if callable(rename):
                        rename(configured.name)
                update_target = getattr(current, "set_branch_target", None)
                if update_target is not None:
                    update_target(
                        branch_path,
                        branch_target,
                        event_clear_allowed=event_clear_allowed,
                        error=branch_error,
                    )
                else:
                    # Compatibility for older/fake node implementations; the
                    # production 0.3.9 node uses the validated target above.
                    update_path = getattr(current, "set_branch_path", None)
                    if update_path is not None:
                        update_path(branch_path)
                update_interval = getattr(
                    current, "set_desired_refresh_interval", None
                )
                if update_interval is not None:
                    update_interval(interval_seconds)
                update_presentation = getattr(
                    current, "set_temperature_presentation", None
                )
                if update_presentation is not None:
                    update_presentation(self._temperature_presentation)
                continue
            node = self._sensor_class(
                self._interface,
                primary=DEFAULT_CONTROLLER_ADDRESS,
                address=configured.address,
                name=configured.name,
                rom=configured.rom,
                adapter_lock=self._adapter_lock,
                branch_path=branch_path,
                branch_target=branch_target,
                branch_event_clear_allowed=event_clear_allowed,
                branch_refresh_error=branch_error,
                refresh_interval_seconds=interval_seconds,
                set_refresh_interval=(
                    lambda seconds, rom=configured.rom: self.set_refresh_interval(
                        rom, seconds
                    )
                ),
                temperature_presentation=self._temperature_presentation,
                record_temperature=self.record_temperature,
            )
            # The address is stable, while rename=False makes an existing IoX
            # node's externally assigned name authoritative across restart.
            self._interface.addNode(node, rename=False)
            self._registered[configured.address] = node
            self._logger.info(
                "Configured DS18B20 %s as %s under %s",
                configured.rom.hex().upper(),
                configured.name,
                DEFAULT_CONTROLLER_ADDRESS,
            )
        updated_refresh, ds2438_changed = self._reconcile_ds2438_locked(
            inventory_by_rom, updated_refresh
        )
        refresh_changed = refresh_changed or ds2438_changed
        if refresh_changed:
            self._save_refresh_configuration_locked(updated_refresh)
        self._configure_scheduler_locked()

    def _reconcile_ds2438_locked(
        self,
        inventory_by_rom: Mapping[bytes, KnownDevice],
        refresh: RefreshConfiguration,
    ) -> tuple[RefreshConfiguration, bool]:
        """Register explicitly identified DS2438 applications without guessing."""

        if not self._ds2438_sensor_classes or self._configured_ds2438 is None:
            return refresh, False
        changed = False
        for configured in self._configured_ds2438:
            known = inventory_by_rom.get(configured.rom)
            if known is None or known.family_code != DS2438_FAMILY:
                continue
            current = self._registered.get(configured.address)
            if configured.application is Ds2438Application.UNASSIGNED:
                if current is not None:
                    authorize = getattr(current, "set_application_authorized", None)
                    if callable(authorize):
                        authorize(False)
                continue
            node_class = self._ds2438_sensor_classes.get(configured.application)
            if node_class is None:
                continue
            application_notice_key = f"ds2438_application_{configured.address}"
            if configured.application_error is not None:
                self._interface.Notices[application_notice_key] = json.dumps(
                    {
                        "status": "invalid_application_configuration",
                        "application": configured.application.value,
                        "rom": configured.rom.hex().upper(),
                        "detail": configured.application_error,
                        "hardware_authorized": False,
                        "unrelated_sensors_affected": False,
                    },
                    sort_keys=True,
                )
            else:
                self._interface.Notices.delete(application_notice_key)
            previous = refresh.sensor(configured.rom)
            application_label = {
                Ds2438Application.HIH4000: "HIH-4000",
                Ds2438Application.ANALOG_VOLTAGE: "Analog",
                Ds2438Application.SOLAR_IRRADIANCE: "Solar",
            }[configured.application]
            default_name = f"{application_label} {configured.rom[1:7].hex().upper()}"
            legacy = self._friendly_name_overrides.get(configured.rom)
            legacy_name = legacy.name if legacy is not None else None
            resolved = resolved_sensor_configuration(
                configured.rom,
                legacy_name_override=legacy_name,
                default_name=default_name,
                previous=previous,
            )
            explicit_legacy_rename = (
                previous is not None
                and legacy_name is not None
                and previous.legacy_name_override != legacy_name
            )
            if resolved != previous:
                refresh = refresh.with_sensor(resolved)
                changed = True
            target, error = self._ds2438_branch_target_locked(known)
            allow_clear = (
                self._branch_read_event_policy is Ds2409EventFlagPolicy.ACKNOWLEDGE
            )
            if current is not None:
                current_application = getattr(current, "application", "hih4000")
                if current_application != configured.application.value:
                    authorize = getattr(current, "set_application_authorized", None)
                    if callable(authorize):
                        authorize(False)
                    self._interface.Notices[DS2438_TRANSITION_NOTICE_KEY] = json.dumps(
                        {
                            "status": "application_transition_requires_review",
                            "rom": configured.rom.hex().upper(),
                            "from": current_application,
                            "to": configured.application.value,
                            "detail": "IoX node-definition replacement is not performed automatically",
                        },
                        sort_keys=True,
                    )
                    continue
                authorize = getattr(current, "set_application_authorized", None)
                if callable(authorize):
                    authorize(configured.application_error is None)
                if explicit_legacy_rename:
                    rename = getattr(current, "rename", None)
                    if callable(rename):
                        rename(resolved.effective_name)
                set_target = getattr(current, "set_branch_target", None)
                if callable(set_target):
                    set_target(
                        known.path,
                        target,
                        event_clear_allowed=allow_clear,
                        error=error,
                    )
                set_interval = getattr(current, "set_desired_refresh_interval", None)
                if callable(set_interval):
                    set_interval(resolved.interval_seconds)
                set_presentation = getattr(current, "set_temperature_presentation", None)
                if callable(set_presentation):
                    set_presentation(self._temperature_presentation)
                set_calibration = getattr(current, "set_calibration", None)
                if callable(set_calibration):
                    set_calibration(
                        configured.solar_calibration, configured.application_error
                    )
                continue
            self._interface.Notices.delete(DS2438_TRANSITION_NOTICE_KEY)
            cache = None
            if configured.application is Ds2438Application.HIH4000:
                cache = (
                    self._ds2438_cache.reading(configured.rom)
                    if self._ds2438_cache_valid else None
                )
            else:
                cache = (
                    self._ds2438_analog_cache.reading(configured.rom)
                    if self._ds2438_analog_cache_valid else None
                )
            kwargs = dict(
                primary=DEFAULT_CONTROLLER_ADDRESS,
                address=configured.address,
                name=resolved.effective_name,
                rom=configured.rom,
                adapter_lock=self._adapter_lock,
                cached_reading=cache,
                branch_path=known.path,
                branch_target=target,
                branch_event_clear_allowed=allow_clear,
                branch_refresh_error=error,
                refresh_interval_seconds=resolved.interval_seconds,
                set_refresh_interval=(
                    lambda seconds, rom=configured.rom: self.set_refresh_interval(
                        rom, seconds
                    )
                ),
            )
            if configured.application is Ds2438Application.HIH4000:
                kwargs.update(
                    temperature_presentation=self._temperature_presentation,
                    record_measurement=self.record_ds2438_measurement,
                )
            else:
                kwargs.update(
                    calibration=configured.solar_calibration,
                    configuration_error=configured.application_error,
                    record_measurement=self.record_ds2438_analog_measurement,
                )
            node = node_class(
                self._interface,
                **kwargs,
            )
            self._interface.addNode(node, rename=False)
            self._registered[configured.address] = node
            if configured.application_error is not None:
                authorize = getattr(node, "set_application_authorized", None)
                if callable(authorize):
                    authorize(False)
            self._logger.info(
                "Configured DS2438/%s %s as %s under %s",
                configured.application.value,
                configured.rom.hex().upper(),
                resolved.effective_name,
                DEFAULT_CONTROLLER_ADDRESS,
            )
        return refresh, changed

    def set_automatic_refresh(self, enabled: bool) -> bool:
        """Persist explicit master intent and safely arm or disarm scheduling."""

        with self._state_lock:
            if not self._refresh_configuration_valid:
                return False
            try:
                proposed = self._refresh_configuration.with_master(enabled)
            except RefreshConfigurationError as exc:
                self._refresh_configuration_failed(str(exc))
                return False
            if proposed != self._refresh_configuration:
                self._save_refresh_configuration_locked(proposed)
            self._configure_scheduler_locked()
            return True

    def set_adapter_recovery(self, enabled: bool) -> bool:
        """Persist explicit recovery authorization without accepting identity."""

        with self._state_lock:
            if not isinstance(enabled, bool):
                return False
            proposed = AdapterRecoveryPolicy(enabled)
            try:
                if proposed != self._adapter_recovery_policy:
                    self._custom_data[ADAPTER_RECOVERY_CONFIGURATION_KEY] = (
                        proposed.to_data()
                    )
            except Exception as exc:
                self._publish_adapter_recovery_status(
                    {
                        "status": "persistence_failed",
                        "detail": f"{type(exc).__name__}: {exc}",
                        "authorized": self._adapter_recovery_authorized(),
                        "identity_transactions": 0,
                        "automatic_retries": 0,
                    }
                )
                return False
            self._adapter_recovery_policy = proposed
            self._adapter_recovery_policy_valid = True
            controller = self._registered.get(DEFAULT_CONTROLLER_ADDRESS)
            update = getattr(controller, "set_adapter_recovery_enabled", None)
            if callable(update):
                update(enabled)
            self._publish_recovery_authorization_locked()
            if enabled and self._profile_ready and not self._adapter_accessible:
                self._adapter_recovery_worker.trigger("policy_enabled")
            return True

    def set_refresh_interval(self, rom: bytes, seconds: int) -> bool:
        """Persist one inventory-backed desired interval without USB access."""

        with self._state_lock:
            if not self._refresh_configuration_valid or not self._inventory_loaded:
                return False
            known = next(
                (
                    item
                    for item in self._inventory.devices
                    if item.rom == rom
                    and item.family_code in (DS18B20_FAMILY, DS2438_FAMILY)
                ),
                None,
            )
            if known is None:
                return False
            try:
                proposed = self._refresh_configuration.with_interval(rom, seconds)
            except RefreshConfigurationError as exc:
                self._refresh_configuration_failed(str(exc))
                return False
            if proposed != self._refresh_configuration:
                self._save_refresh_configuration_locked(proposed)
            self._configure_scheduler_locked()
            return True

    def record_ds2438_measurement(self, reading: Hih4000Reading) -> bool:
        """Atomically persist one validated canonical DS2438 measurement."""

        with self._state_lock:
            if not self._ds2438_cache_valid:
                return False
            configured = any(
                item.rom == reading.rom for item in (self._configured_ds2438 or ())
            )
            known = any(
                item.rom == reading.rom and item.family_code == DS2438_FAMILY
                for item in self._inventory.devices
            )
            if not (configured and known):
                return False
            try:
                proposed = self._ds2438_cache.with_reading(reading)
                self._custom_data[DS2438_MEASUREMENT_CACHE_KEY] = proposed.to_data()
            except Exception as exc:
                self._interface.Notices[DS2438_CACHE_NOTICE_KEY] = json.dumps(
                    {
                        "status": "ds2438_cache_write_failed",
                        "detail": f"{type(exc).__name__}: {exc}",
                    },
                    sort_keys=True,
                )
                return False
            self._ds2438_cache = proposed
            self._interface.Notices.delete(DS2438_CACHE_NOTICE_KEY)
            return True

    def record_ds2438_analog_measurement(self, reading: CachedAnalogReading) -> bool:
        """Atomically persist one application-neutral or calibrated reading."""

        with self._state_lock:
            if not self._ds2438_analog_cache_valid:
                return False
            configured = next(
                (item for item in (self._configured_ds2438 or ()) if item.rom == reading.rom),
                None,
            )
            known = any(
                item.rom == reading.rom and item.family_code == DS2438_FAMILY
                for item in self._inventory.devices
            )
            if configured is None or not known:
                return False
            try:
                proposed = self._ds2438_analog_cache.with_reading(reading)
                self._custom_data[DS2438_ANALOG_CACHE_KEY] = proposed.to_data()
            except Exception as exc:
                self._interface.Notices[DS2438_ANALOG_CACHE_NOTICE_KEY] = json.dumps(
                    {
                        "status": "ds2438_analog_cache_write_failed",
                        "detail": f"{type(exc).__name__}: {exc}",
                    },
                    sort_keys=True,
                )
                return False
            self._ds2438_analog_cache = proposed
            self._interface.Notices.delete(DS2438_ANALOG_CACHE_NOTICE_KEY)
            return True

    def set_temperature_unit(self, unit: TemperatureUnit) -> bool:
        """Validate and enqueue one global unit selection without blocking."""

        with self._state_lock:
            try:
                parsed = (
                    unit if isinstance(unit, TemperatureUnit) else TemperatureUnit(unit)
                )
            except TemperaturePresentationError as exc:
                self._temperature_presentation_failed(str(exc))
                return False
            except (TypeError, ValueError):
                self._temperature_presentation_failed(
                    "temperature unit must be fahrenheit or celsius"
                )
                return False
            return self._enqueue_temperature_presentation_locked(
                PresentationIntent.unit(parsed)
            )

    def set_temperature_precision(self, decimal_places: int) -> bool:
        """Validate and enqueue one global decimal selection without blocking."""

        with self._state_lock:
            try:
                if isinstance(decimal_places, bool) or not isinstance(
                    decimal_places, int
                ):
                    raise TemperaturePresentationError(
                        "temperature decimal places must be an integer"
                    )
                TemperaturePresentation(decimal_places=decimal_places)
            except TemperaturePresentationError as exc:
                self._temperature_presentation_failed(str(exc))
                return False
            return self._enqueue_temperature_presentation_locked(
                PresentationIntent.precision(decimal_places)
            )

    def _enqueue_temperature_presentation_locked(
        self, intent: PresentationIntent
    ) -> bool:
        if not self._temperature_presentation_valid:
            self._temperature_presentation_failed(
                "persisted temperature presentation is invalid; correct it and restart "
                "the plugin before changing presentation settings"
            )
            return False
        if not self._temperature_presentation_consistent:
            self._temperature_presentation_consistency_unknown(
                "runtime profile consistency is unconfirmed; restart the plugin through "
                "the supported eisy-ui lifecycle before changing presentation settings"
            )
            return False
        if self._presentation_worker.enqueue(intent):
            return True
        self._temperature_presentation_change_failed(
            "presentation command queue is full or shutting down; wait for the current "
            "command to finish and try again",
            status="temperature_presentation_queue_unavailable",
            consistency_confirmed=True,
        )
        return False

    def _process_temperature_presentation_intent(
        self, intent: PresentationIntent
    ) -> None:
        """Evaluate and commit one intent on the dedicated serialized worker."""

        with self._state_lock:
            if (
                not self._temperature_presentation_valid
                or not self._temperature_presentation_consistent
            ):
                return
            try:
                if intent.kind is PresentationIntentKind.UNIT:
                    proposed = self._temperature_presentation.with_unit(intent.value)
                elif intent.kind is PresentationIntentKind.PRECISION:
                    proposed = self._temperature_presentation.with_decimal_places(
                        intent.value
                    )
                else:  # pragma: no cover - enum exhaustiveness guard
                    raise TemperaturePresentationError(
                        "temperature presentation command is unsupported"
                    )
            except TemperaturePresentationError as exc:
                self._temperature_presentation_failed(str(exc))
                return
            self._set_temperature_presentation_locked(proposed)

    def _set_temperature_presentation_locked(
        self, proposed: TemperaturePresentation
    ) -> bool:
        if (
            proposed == self._temperature_presentation
            and self._temperature_presentation_valid
        ):
            return True
        previous = self._temperature_presentation
        try:
            self._presentation_profile_updater(proposed)
        except TimeoutError as exc:
            self._restore_temperature_presentation_locked(
                previous,
                "profile acknowledgment timed out after the platform outcome became "
                f"unknown: {exc}",
                restore_persistence=False,
            )
            return False
        except Exception as exc:
            self._temperature_presentation_change_failed(
                f"IoX profile synchronization failed: {type(exc).__name__}: {exc}"
            )
            return False
        try:
            self._custom_data[TEMPERATURE_PRESENTATION_KEY] = proposed.to_data()
        except Exception as exc:
            self._restore_temperature_presentation_locked(
                previous,
                f"preference persistence failed after profile installation: "
                f"{type(exc).__name__}: {exc}",
                restore_persistence=True,
            )
            return False
        self._temperature_presentation = proposed
        self._temperature_presentation_valid = True
        self._interface.Notices.delete(TEMPERATURE_PRESENTATION_NOTICE_KEY)
        controller = self._registered.get(DEFAULT_CONTROLLER_ADDRESS)
        if controller is not None:
            update = getattr(controller, "set_temperature_presentation", None)
            if update is not None:
                update(proposed)
        for address, node in self._registered.items():
            if address == DEFAULT_CONTROLLER_ADDRESS:
                continue
            update = getattr(node, "set_temperature_presentation", None)
            if update is not None:
                update(proposed)
        return True

    def _restore_temperature_presentation_locked(
        self,
        previous: TemperaturePresentation,
        reason: str,
        *,
        restore_persistence: bool,
    ) -> None:
        """Attempt one compensating profile update and no automatic retry."""

        restoration_errors: list[str] = []
        try:
            self._presentation_profile_updater(previous)
        except Exception as exc:
            restoration_errors.append(
                f"profile restoration failed: {type(exc).__name__}: {exc}"
            )
        if restore_persistence:
            try:
                self._custom_data[TEMPERATURE_PRESENTATION_KEY] = previous.to_data()
            except Exception as exc:
                restoration_errors.append(
                    f"preference restoration failed: {type(exc).__name__}: {exc}"
                )
        if restoration_errors:
            self._temperature_presentation_consistent = False
            self._temperature_presentation_consistency_unknown(
                f"{reason}; {'; '.join(restoration_errors)}"
            )
            return
        self._temperature_presentation_consistent = True
        self._temperature_presentation_change_failed(
            f"{reason}; the previous authoritative profile was restored",
            status="temperature_presentation_change_rejected_restored",
            consistency_confirmed=True,
        )

    def record_temperature(self, rom: bytes, celsius: float) -> bool:
        """Atomically preserve canonical precision after one accepted read."""

        with self._state_lock:
            if not self._temperature_presentation_valid:
                return False
            known = any(
                item.rom == rom and item.family_code == DS18B20_FAMILY
                for item in self._inventory.devices
            )
            configured = any(
                item.rom == rom for item in (self._configured or ())
            )
            if not (known or configured):
                return False
            try:
                proposed = self._temperature_presentation.with_raw_celsius(
                    rom, celsius
                )
                if proposed != self._temperature_presentation:
                    self._custom_data[TEMPERATURE_PRESENTATION_KEY] = proposed.to_data()
                    self._temperature_presentation = proposed
            except Exception as exc:
                self._temperature_presentation_failed(
                    f"raw temperature persistence failed: {type(exc).__name__}: {exc}"
                )
                return False
            return True

    def _save_refresh_configuration_locked(
        self, configuration: RefreshConfiguration
    ) -> None:
        # Custom.__setitem__ atomically saves the complete nested value while
        # preserving inventory and unrelated customdata keys.
        self._custom_data[REFRESH_CONFIGURATION_KEY] = configuration.to_data()
        self._refresh_configuration = configuration

    def shutdown(self) -> None:
        """Quiesce every worker before the platform interface is stopped."""

        presentation_error: Exception | None = None
        recovery_error: Exception | None = None
        try:
            self._adapter_recovery_worker.stop()
        except Exception as exc:
            recovery_error = exc
        try:
            self._presentation_worker.stop()
        except Exception as exc:
            presentation_error = exc
        self._scheduler.stop()
        if self._adapter_binding is not None:
            if not self._adapter_lock.acquire(blocking=True):
                raise RuntimeError("could not quiesce adapter binding during shutdown")
            try:
                self._adapter_binding.close()
            finally:
                self._adapter_lock.release()
        if presentation_error is not None:
            raise presentation_error
        if recovery_error is not None:
            raise recovery_error

    def verify_adapter_identity(self) -> bool:
        """Explicitly bind every ordinary operation to the accepted adapter ROM."""

        return self._verify_adapter_identity("manual")

    def _automatic_verify_adapter_identity(self, event: str) -> bool:
        """Worker entry point; authorization is rechecked immediately."""

        if not self._adapter_recovery_authorized():
            return False
        return self._verify_adapter_identity(f"automatic:{event}")

    def _verify_adapter_identity(self, source: str) -> bool:
        """Verify once under the shared lock and retain that exact handle."""

        binding = self._adapter_binding
        if binding is None:
            return False
        with self._state_lock:
            if not self._profile_ready:
                return self._adapter_binding_failed("runtime profile is not ready")
            if not self._inventory_loaded:
                return self._adapter_binding_failed(
                    "persistent discovery inventory is not ready"
                )
            identities = tuple(
                item.rom
                for item in self._inventory.devices
                if item.family_code == DS9490_ID_FAMILY and not item.path
            )
            if len(identities) != 1:
                return self._adapter_binding_failed(
                    "accepted inventory must contain exactly one root family-0x81 "
                    "adapter identity"
                )
            expected = identities[0]
        if not self._adapter_lock.acquire(blocking=False):
            return self._adapter_binding_failed(
                "1-Wire adapter is busy; identity was not verified"
            )
        try:
            evidence = binding.verify(expected)
        except Exception as exc:
            self._logger.error("DS9490 identity verification failed: %s", exc)
            with self._state_lock:
                self._adapter_accessible = False
                self._update_adapter_status_locked(False)
                self._configure_scheduler_locked()
            return self._adapter_binding_failed(
                str(exc), error_type=type(exc).__name__
            )
        finally:
            self._adapter_lock.release()
        with self._state_lock:
            self._adapter_accessible = True
            self._update_adapter_status_locked(True)
            self._configure_scheduler_locked()
            descriptor = evidence.descriptor
            self._interface.Notices[ADAPTER_BINDING_NOTICE_KEY] = json.dumps(
                {
                    "status": "verified",
                    "adapter_identity_rom": evidence.identity_rom.hex().upper(),
                    "matching_root_devices": evidence.root_device_count,
                    "location_evidence": {
                        "bus_number": descriptor.bus_number,
                        "device_address": descriptor.device_address,
                        "port_path": list(descriptor.port_path)
                        if descriptor.port_path
                        else None,
                    },
                    "operator_action_after_restart_or_disconnect": (
                        "automatic recovery is authorized"
                        if self._adapter_recovery_authorized()
                        else "run Verify Adapter Identity again"
                    ),
                    "verification_source": source,
                    "automatic_retries": 0,
                },
                sort_keys=True,
            )
        return True

    def _update_adapter_status_locked(self, accessible: bool) -> None:
        controller = self._registered.get(DEFAULT_CONTROLLER_ADDRESS)
        update = getattr(controller, "set_adapter_accessible", None)
        if callable(update):
            update(accessible)

    def _adapter_binding_failed(
        self, detail: str, *, error_type: str | None = None
    ) -> bool:
        self._interface.Notices[ADAPTER_BINDING_NOTICE_KEY] = json.dumps(
            {
                "status": "unverified",
                "detail": detail,
                "error_type": error_type,
                "ordinary_hardware_operations_blocked": True,
                "operator_action": "run Verify Adapter Identity",
                "automatic_retries": 0,
            },
            sort_keys=True,
        )
        return False

    def _handle_adapter_binding_invalidation(self, detail: str) -> None:
        """Disarm promptly when the retained native handle reports disconnect."""

        with self._state_lock:
            self._adapter_accessible = False
            self._update_adapter_status_locked(False)
            if self._profile_ready:
                self._configure_scheduler_locked()
            self._adapter_binding_failed(detail, error_type="AdapterNotFoundError")
            if self._adapter_recovery_authorized():
                self._adapter_recovery_worker.trigger("retained_handle_invalidated")

    def _adapter_recovery_authorized(self) -> bool:
        with self._state_lock:
            return (
                self._adapter_recovery_policy_valid
                and self._adapter_recovery_policy.enabled
            )

    def _has_accepted_adapter_identity(self) -> bool:
        with self._state_lock:
            if not self._inventory_loaded:
                return False
            identities = tuple(
                item.rom
                for item in self._inventory.devices
                if item.family_code == DS9490_ID_FAMILY and not item.path
            )
            return len(identities) == 1

    def _adapter_binding_verified(self) -> bool:
        binding = self._adapter_binding
        return bool(binding is not None and binding.verified)

    def _publish_adapter_recovery_status(self, payload: dict[str, object]) -> None:
        rendered = json.dumps(payload, sort_keys=True)
        self._interface.Notices[ADAPTER_RECOVERY_NOTICE_KEY] = rendered
        self._logger.info("Automatic adapter recovery status: %s", rendered)

    def _publish_recovery_authorization_locked(self) -> None:
        enabled = self._adapter_recovery_authorized()
        self._publish_adapter_recovery_status(
            {
                "status": "authorized" if enabled else "disabled",
                "authorized": enabled,
                "identity_transactions": 0,
                "automatic_retries": 0,
                "identity_replacement_allowed": False,
                "automatic_topology_discovery": False,
            }
        )

    def _maybe_trigger_startup_recovery_locked(self) -> None:
        if (
            self._startup_recovery_triggered
            or not self._profile_ready
            or not self._adapter_recovery_policy_loaded
        ):
            return
        self._startup_recovery_triggered = True
        if not self._adapter_recovery_policy_valid:
            return
        if self._adapter_recovery_authorized():
            self._adapter_recovery_worker.trigger("plugin_startup")
        else:
            self._publish_recovery_authorization_locked()

    def wait_for_adapter_recovery(self, timeout_seconds: float = 2.0) -> bool:
        """Wait for recovery work; intended for deterministic verification."""

        return self._adapter_recovery_worker.wait_idle(timeout_seconds)

    def wait_for_presentation_commands(self, timeout_seconds: float = 2.0) -> bool:
        """Wait for queued presentation work; intended for deterministic verification."""

        return self._presentation_worker.wait_idle(timeout_seconds)

    def _configure_scheduler_locked(self) -> None:
        targets: list[RefreshTarget] = []
        configuration_valid = (
            self._refresh_configuration_valid
            and self._inventory_loaded
            and self._configured is not None
            and self._configured_ds2438 is not None
        )
        if configuration_valid:
            inventory_by_rom = {item.rom: item for item in self._inventory.devices}
            ds2438_applications = {
                item.rom: item.application for item in self._configured_ds2438 or ()
            }
            for setting in self._refresh_configuration.sensors:
                known = inventory_by_rom.get(setting.rom)
                node = self._registered.get(setting.address)
                if known is None or node is None:
                    continue
                if (
                    known.family_code == DS2438_FAMILY
                    and ds2438_applications.get(setting.rom)
                    not in (
                        Ds2438Application.HIH4000,
                        Ds2438Application.ANALOG_VOLTAGE,
                        Ds2438Application.SOLAR_IRRADIANCE,
                    )
                ):
                    continue
                if known.family_code == DS2438_FAMILY:
                    configured_ds2438 = next(
                        (
                            item for item in (self._configured_ds2438 or ())
                            if item.rom == setting.rom
                        ),
                        None,
                    )
                    if (
                        configured_ds2438 is None
                        or getattr(node, "application", "hih4000")
                        != configured_ds2438.application.value
                        or configured_ds2438.application_error is not None
                    ):
                        continue
                automatic_eligible = True
                if known.path:
                    automatic_eligible = (
                        self._unattended_branch_policy
                        is not Ds2409UnattendedBranchPolicy.DISABLED
                    )
                    if automatic_eligible:
                        scheduled = getattr(
                            node, "scheduled_refresh_temperature", None
                        )
                        if not callable(scheduled):
                            continue
                        allow_clear = (
                            self._unattended_branch_policy
                            is Ds2409UnattendedBranchPolicy.ACKNOWLEDGE
                        )
                        refresh = (
                            lambda scheduled=scheduled, allow_clear=allow_clear: scheduled(
                                branch_event_clear_allowed=allow_clear
                            )
                        )
                    else:
                        # Retain a visible, ineligible target without granting
                        # the scheduler a new branch authorization.
                        refresh = getattr(node, "refresh_temperature", None)
                else:
                    refresh = getattr(node, "refresh_temperature", None)
                if not callable(refresh):
                    continue
                error_getter = getattr(node, "last_refresh_error", None)
                if not callable(error_getter):
                    error_getter = lambda: None
                targets.append(
                    RefreshTarget(
                        setting.address,
                        setting.rom,
                        setting.effective_name,
                        setting.interval_seconds,
                        known.path,
                        refresh,
                        error_getter,
                        automatic_eligible,
                    )
                )
        self._scheduler.configure(
            master_enabled=(
                self._refresh_configuration.master_enabled
                if self._refresh_configuration_valid
                else False
            ),
            armed=(
                self._refresh_configuration.scheduler_armed
                if self._refresh_configuration_valid and self._adapter_accessible
                else False
            ),
            adapter_accessible=self._adapter_accessible,
            configuration_valid=configuration_valid,
            targets=tuple(targets),
        )

    def _ensure_ds2438_application_parameters_locked(self) -> None:
        """Publish safe inventory-derived selectors without authorizing hardware."""

        if not self._custom_parameters_loaded or not self._inventory_loaded:
            return
        values = dict(self._custom.items())
        changed = False
        errors: list[str] = []
        for device in self._inventory.devices:
            if device.family_code != DS2438_FAMILY:
                continue
            key = f"{DS2438_APPLICATION_PREFIX}{device.rom.hex().upper()}"
            if key in values:
                continue
            try:
                self._custom[key] = Ds2438Application.UNASSIGNED.value
            except Exception as exc:
                errors.append(f"{key}: {type(exc).__name__}: {exc}")
                continue
            values[key] = Ds2438Application.UNASSIGNED.value
            changed = True
        # Solar calibration fields are application-specific and never appear
        # for unassigned, HIH-4000, or generic analog devices.
        for configured in parse_ds2438_applications(values):
            if configured.application is not Ds2438Application.SOLAR_IRRADIANCE:
                continue
            suffix = configured.rom.hex().upper()
            defaults = {
                f"{DS2438_SOLAR_MULTIPLIER_PREFIX}{suffix}": SOLAR_MULTIPLIER_UNASSIGNED,
                f"{DS2438_SOLAR_OFFSET_PREFIX}{suffix}": "0",
            }
            for key, value in defaults.items():
                if key in values:
                    continue
                try:
                    self._custom[key] = value
                except Exception as exc:
                    errors.append(f"{key}: {type(exc).__name__}: {exc}")
                    continue
                values[key] = value
                changed = True
        if errors:
            self._interface.Notices[DS2438_APPLICATION_NOTICE_KEY] = json.dumps(
                {
                    "status": "application_parameter_population_failed",
                    "details": errors,
                    "hardware_authorized": False,
                    "action": "Save plugin configuration again or restart the plugin.",
                },
                sort_keys=True,
            )
        else:
            self._interface.Notices.delete(DS2438_APPLICATION_NOTICE_KEY)
        if changed:
            # Reflect the just-published safe declarations immediately; the
            # later PG3 customparams callback is intentionally idempotent.
            self._configured_ds2438 = parse_ds2438_applications(values)

    def _fail_closed_registered_hardware_locked(self, detail: str) -> None:
        """Revoke affected hardware paths after safety configuration fails."""

        for node in self._registered.values():
            rom = getattr(node, "rom", b"")
            if isinstance(rom, bytes) and rom[:1] == bytes((DS2438_FAMILY,)):
                authorize = getattr(node, "set_application_authorized", None)
                if callable(authorize):
                    authorize(False)
            path = getattr(node, "_branch_path", getattr(node, "branch_path", ()))
            if not path:
                continue
            set_target = getattr(node, "set_branch_target", None)
            if callable(set_target):
                set_target(
                    path,
                    None,
                    event_clear_allowed=False,
                    error=f"safety-critical configuration invalid: {detail}",
                )

    def _branch_target_locked(
        self, known: KnownDevice | None
    ) -> tuple[BranchTemperatureTarget | None, str | None]:
        if known is None or not known.path:
            return None, None
        if len(known.path) != 1:
            return None, "nested DS2409 branch temperature reads are unsupported"
        scan = self._inventory.last_scan
        if (
            scan is None
            or scan.segment_scope != TOPOLOGY_SCOPE
            or not scan.complete
        ):
            return None, "branch topology has no validated complete scan"
        if (
            known.adapter_rom is None
            or scan.adapter_rom != known.adapter_rom.hex().upper()
        ):
            return None, "branch sensor adapter identity is missing or ambiguous"
        if known.rom.hex().upper() not in scan.reachable_roms:
            return None, "branch sensor was not reachable in the last manual scan"
        if known.last_seen != scan.timestamp:
            return None, "branch sensor topology is stale"
        path_label = branch_path_label(known.path)
        if path_label not in scan.scanned_branches:
            return None, "branch path was not scanned in the last topology result"
        try:
            return (
                BranchTemperatureTarget(known.rom, known.adapter_rom, known.path),
                None,
            )
        except ValueError as exc:
            return None, str(exc)

    def _ds2438_branch_target_locked(
        self, known: KnownDevice
    ) -> tuple[Ds2438BranchTarget | None, str | None]:
        if not known.path:
            return None, None
        if len(known.path) != 1:
            return None, "nested DS2409 branch DS2438 reads are unsupported"
        scan = self._inventory.last_scan
        if scan is None or scan.segment_scope != TOPOLOGY_SCOPE or not scan.complete:
            return None, "branch topology has no validated complete scan"
        if known.adapter_rom is None or scan.adapter_rom != known.adapter_rom.hex().upper():
            return None, "branch sensor adapter identity is missing or ambiguous"
        if known.rom.hex().upper() not in scan.reachable_roms:
            return None, "branch sensor was not reachable in the last manual scan"
        if known.last_seen != scan.timestamp:
            return None, "branch sensor topology is stale"
        if branch_path_label(known.path) not in scan.scanned_branches:
            return None, "branch path was not scanned in the last topology result"
        try:
            return (
                Ds2438BranchTarget(
                    known.rom,
                    known.adapter_rom,
                    known.path,
                    scan.timestamp,
                ),
                None,
            )
        except ValueError as exc:
            return None, str(exc)

    def discover_devices(self) -> bool:
        """Run one explicit scan, persist it, and add validated family-0x28 nodes."""

        with self._state_lock:
            self._event_flags_cleared = ()
            if not self._profile_ready:
                return self._discovery_failed("runtime profile is not ready")
            if not self._adapter_accessible:
                return self._discovery_failed("DS9490 adapter is not accessible")
            if self._configured is None:
                return self._discovery_failed("sensor configuration is not ready")
            if not self._inventory_loaded:
                return self._discovery_failed("persistent discovery inventory is not ready")
            previous_inventory = self._inventory

        def commit(report, timestamp: str) -> None:
            if isinstance(report, TopologyDiscoveryReport):
                proposed = update_topology_inventory(
                    previous_inventory, report, timestamp=timestamp
                )
            else:
                proposed = update_inventory(
                    previous_inventory, report, timestamp=timestamp
                )
            with self._state_lock:
                self._desired_sensors_locked(proposed)
                # Invalidate the old authorization while the same adapter lock
                # is still held.  A failed update can therefore never leave the
                # previous successful scan usable by a separate diagnostic.
                self._invalidate_inventory_evidence_locked(
                    "discovery inventory update is in progress"
                )
                self._custom_data[INVENTORY_KEY] = proposed.to_data()
                self._inventory = proposed
                try:
                    self._persist_inventory_evidence_locked(proposed)
                except OSError as exc:
                    self._inventory_loaded = False
                    try:
                        self._invalidate_inventory_evidence_locked(
                            "PG3 inventory committed but evidence mirror write failed"
                        )
                    except OSError:
                        # The earlier in-progress invalidation already replaced
                        # the prior valid mirror before the PG3 commit.
                        pass
                    if self._profile_ready:
                        self._configure_scheduler_locked()
                    raise InventoryMirrorWriteError(
                        "PG3 discovery inventory was saved, but the diagnostic "
                        "evidence mirror failed and remains invalid"
                    ) from exc
                self._inventory_loaded = True
                self._ensure_ds2438_application_parameters_locked()
                self._reconcile_locked()
                self._report_scan_locked()

        try:
            self._discovery.run_once(
                acknowledge_event_flags=(
                    self._event_flag_policy
                    is Ds2409EventFlagPolicy.ACKNOWLEDGE
                ),
                on_event_flags_cleared=self._record_event_flags_cleared,
                commit=commit,
            )
            return True
        except AdapterBusyError as exc:
            self._logger.warning("Manual 1-Wire discovery rejected: %s", exc)
            return self._discovery_failed(str(exc), status="busy")
        except InventoryMirrorWriteError as exc:
            self._logger.error("Manual 1-Wire discovery partially persisted: %s", exc)
            return self._discovery_failed(
                str(exc),
                status="partial_persistence",
                error_type=type(exc).__name__,
                inventory_changed=True,
                evidence_mirror="invalid",
            )
        except Exception as exc:
            self._logger.error("Manual 1-Wire discovery failed: %s", exc)
            return self._discovery_failed(
                str(exc), status="error", error_type=type(exc).__name__
            )

    def _persist_inventory_evidence_locked(
        self, inventory: DiscoveryInventory
    ) -> None:
        if self._inventory_evidence_path is not None:
            persist_inventory_evidence(self._inventory_evidence_path, inventory)

    def _invalidate_inventory_evidence_locked(self, detail: str) -> None:
        if self._inventory_evidence_path is not None:
            invalidate_inventory_evidence(self._inventory_evidence_path, detail)

    def read_ds2409_status(self) -> bool:
        """Run one explicit, non-mutating status read for one known root coupler."""

        with self._state_lock:
            if not self._profile_ready:
                return self._ds2409_status_failed("runtime profile is not ready")
            if not self._adapter_accessible:
                return self._ds2409_status_failed(
                    "DS9490 adapter is not accessible"
                )
            if not self._inventory_loaded:
                return self._ds2409_status_failed(
                    "persistent discovery inventory is not ready"
                )
            couplers = [
                item.rom
                for item in self._inventory.devices
                if item.family_code == DS2409_FAMILY and not item.path
            ]
            adapters = [
                item.rom
                for item in self._inventory.devices
                if item.family_code == DS9490_ID_FAMILY and not item.path
            ]
            if len(couplers) != 1 or len(adapters) != 1:
                return self._ds2409_status_failed(
                    "status diagnostic requires exactly one known root DS2409 "
                    "and one adapter identity"
                )
            coupler = couplers[0]
            adapter = adapters[0]

        try:
            report = self._ds2409_status.run_once(adapter, coupler)
            payload = report.to_notice()
            rendered = json.dumps(payload, sort_keys=True)
            self._interface.Notices[DS2409_STATUS_NOTICE_KEY] = rendered
            self._logger.info("Manual DS2409 status result: %s", rendered)
            return True
        except AdapterBusyError as exc:
            return self._ds2409_status_failed(str(exc), status="busy")
        except Exception as exc:
            self._logger.error("Manual DS2409 status failed: %s", exc)
            return self._ds2409_status_failed(
                str(exc), status="error", error_type=type(exc).__name__
            )

    def _desired_sensors_locked(
        self, inventory: DiscoveryInventory
    ) -> tuple[ConfiguredDs18b20, ...]:
        assert self._configured is not None
        desired = {item.rom: item for item in self._configured}
        for item in inventory.devices:
            if item.family_code != DS18B20_FAMILY or item.rom in desired:
                continue
            desired[item.rom] = ConfiguredDs18b20(
                item.rom,
                _sensor_address(item),
                default_sensor_name(item.rom),
            )

        by_name: dict[str, ConfiguredDs18b20] = {}
        resolved: list[ConfiguredDs18b20] = []
        for item in sorted(desired.values(), key=lambda value: value.address):
            folded = item.name.casefold()
            conflict = by_name.get(folded)
            if conflict is not None and conflict.address != item.address:
                fallback = default_sensor_name(item.rom)
                self._friendly_names_failed((
                    f"friendly name {item.name!r} conflicts between ROMs "
                    f"{conflict.rom.hex().upper()} and {item.rom.hex().upper()}; "
                    f"using deterministic name {fallback!r} for the latter",
                ))
                item = ConfiguredDs18b20(item.rom, item.address, fallback)
                folded = fallback.casefold()
            by_name[folded] = item
            resolved.append(item)
        return tuple(resolved)

    def _report_restored_inventory_locked(self) -> None:
        if self._inventory.last_scan is None and not self._inventory.devices:
            return
        payload = self._inventory_payload(
            status="last_known_inventory",
            current_reachability="unknown_until_manual_discovery",
        )
        self._interface.Notices[DISCOVERY_NOTICE_KEY] = json.dumps(
            payload, sort_keys=True
        )

    def _report_scan_locked(self) -> None:
        scan = self._inventory.last_scan
        assert scan is not None
        if not scan.reachable_roms:
            status = "empty_bus"
        elif scan.branches_not_scanned:
            status = "partial_topology"
        else:
            status = "ok"
        payload = self._inventory_payload(
            status=status,
            current_reachability="from_last_manual_scan",
        )
        self._interface.Notices[DISCOVERY_NOTICE_KEY] = json.dumps(
            payload, sort_keys=True
        )
        self._logger.info(
            "Manual 1-Wire discovery result: %s",
            json.dumps(payload, sort_keys=True),
        )

    def _inventory_payload(
        self, *, status: str, current_reachability: str
    ) -> dict[str, object]:
        scan = self._inventory.last_scan
        reachable = set(scan.reachable_roms) if scan is not None else None
        return {
            "status": status,
            "segment_scope": scan.segment_scope if scan is not None else None,
            "scan_timestamp": scan.timestamp if scan is not None else None,
            "adapter_rom": scan.adapter_rom if scan is not None else None,
            "current_reachability": current_reachability,
            "reachable_roms_at_last_scan": (
                sorted(reachable) if reachable is not None else None
            ),
            "known_devices": [
                {
                    "rom": item.rom.hex().upper(),
                    "family_code": f"0x{item.family_code:02X}",
                    "kind": _device_kind(item.family_code),
                    "reachable_at_last_scan": (
                        item.rom.hex().upper() in reachable
                        if reachable is not None
                        else None
                    ),
                    "adapter_rom": (
                        item.adapter_rom.hex().upper()
                        if item.adapter_rom is not None
                        else None
                    ),
                    "path": [
                        {
                            "coupler_rom": hop.coupler_rom.hex().upper(),
                            "branch": hop.branch.value,
                        }
                        for hop in item.path
                    ],
                }
                for item in self._inventory.devices
            ],
            "branches_not_scanned": (
                list(scan.branches_not_scanned) if scan is not None else None
            ),
            "scanned_branches": (
                list(scan.scanned_branches) if scan is not None else None
            ),
            "discovery_complete": scan.complete if scan is not None else None,
            "automatic_retries": 0,
            "event_flags_acknowledged": [
                {
                    "coupler_rom": item.coupler_rom.hex().upper(),
                    "raw_status": f"0x{item.raw_status:02X}",
                    "main_event": item.main_event,
                    "aux_event": item.aux_event,
                    "irreversible_loss": True,
                }
                for item in (
                    scan.event_flags_acknowledged if scan is not None else ()
                )
            ],
        }

    def _discovery_failed(
        self,
        detail: str,
        *,
        status: str = "unavailable",
        error_type: str | None = None,
        inventory_changed: bool = False,
        evidence_mirror: str | None = None,
    ) -> bool:
        payload: dict[str, object] = {
            "status": status,
            "detail": detail,
            "inventory_changed": inventory_changed,
            "nodes_added": False,
            "automatic_retries": 0,
        }
        if error_type is not None:
            payload["error_type"] = error_type
        if evidence_mirror is not None:
            payload["evidence_mirror"] = evidence_mirror
        if self._event_flags_cleared:
            payload["event_flags_acknowledged"] = _event_acknowledgement_payload(
                self._event_flags_cleared
            )
            payload["irreversible_event_loss"] = True
        self._interface.Notices[DISCOVERY_NOTICE_KEY] = json.dumps(
            payload, sort_keys=True
        )
        return False

    def _record_event_flags_cleared(
        self, acknowledgements: tuple[EventFlagAcknowledgement, ...]
    ) -> None:
        """Make event loss visible immediately, including later failure paths."""

        self._event_flags_cleared = acknowledgements
        evidence = _event_acknowledgement_payload(acknowledgements)
        self._logger.warning(
            "DS2409 event flags acknowledged and irreversibly cleared: %s",
            json.dumps(evidence, sort_keys=True),
        )
        self._interface.Notices[DISCOVERY_NOTICE_KEY] = json.dumps(
            {
                "status": "event_flags_acknowledged",
                "event_flags_acknowledged": evidence,
                "irreversible_event_loss": True,
                "inventory_changed": False,
                "automatic_retries": 0,
            },
            sort_keys=True,
        )

    def _ds2409_status_failed(
        self,
        detail: str,
        *,
        status: str = "unavailable",
        error_type: str | None = None,
    ) -> bool:
        payload: dict[str, object] = {
            "status": status,
            "detail": detail,
            "mutating_ds2409_commands": 0,
            "automatic_retries": 0,
        }
        if error_type is not None:
            payload["error_type"] = error_type
        self._interface.Notices[DS2409_STATUS_NOTICE_KEY] = json.dumps(
            payload, sort_keys=True
        )
        return False

    def _configuration_failed(self, detail: str) -> None:
        self._interface.Notices[CONFIG_NOTICE_KEY] = detail
        self._logger.error("1-Wire sensor configuration rejected: %s", detail)

    def _friendly_names_failed(self, details: tuple[str, ...]) -> None:
        payload = {
            "status": "invalid_optional_friendly_names",
            "details": list(details),
            "action": (
                "Correct or remove the listed optional ROM-keyed name. "
                "Use standard IoX node Rename for routine naming."
            ),
            "scheduler_affected": False,
            "usb_activity": False,
        }
        self._interface.Notices[FRIENDLY_NAME_NOTICE_KEY] = json.dumps(
            payload, sort_keys=True
        )
        self._logger.warning(
            "Optional 1-Wire friendly-name entries rejected without disabling "
            "valid sensors: %s",
            "; ".join(details),
        )

    def _inventory_failed(self, detail: str) -> None:
        self._interface.Notices[DISCOVERY_NOTICE_KEY] = json.dumps(
            {
                "status": "invalid_persisted_inventory",
                "detail": detail,
                "nodes_added": False,
            },
            sort_keys=True,
        )
        self._logger.error("1-Wire discovery inventory rejected: %s", detail)

    def _refresh_configuration_failed(self, detail: str) -> None:
        self._interface.Notices[REFRESH_CONFIGURATION_NOTICE_KEY] = json.dumps(
            {
                "status": "invalid_refresh_configuration",
                "detail": detail,
                "master_enabled": False,
                "automatic_usb_activity": False,
            },
            sort_keys=True,
        )
        self._logger.error("Automatic refresh configuration rejected: %s", detail)

    def _temperature_presentation_failed(self, detail: str) -> None:
        self._interface.Notices[TEMPERATURE_PRESENTATION_NOTICE_KEY] = json.dumps(
            {
                "status": "invalid_temperature_presentation",
                "detail": detail,
                "defaults": {"unit": "fahrenheit", "decimal_places": 1},
                "usb_activity": False,
            },
            sort_keys=True,
        )
        self._logger.error("Temperature presentation rejected: %s", detail)

    def _temperature_presentation_change_failed(
        self,
        detail: str,
        *,
        status: str = "temperature_presentation_change_failed",
        consistency_confirmed: bool = True,
    ) -> None:
        self._interface.Notices[TEMPERATURE_PRESENTATION_NOTICE_KEY] = json.dumps(
            {
                "status": status,
                "detail": detail,
                "authoritative_preference": {
                    "unit": self._temperature_presentation.unit.value,
                    "decimal_places": self._temperature_presentation.decimal_places,
                },
                "profile_consistency_confirmed": consistency_confirmed,
                "further_changes_allowed": consistency_confirmed,
                "usb_activity": False,
            },
            sort_keys=True,
        )
        self._logger.error("Temperature presentation change failed: %s", detail)

    def _temperature_presentation_consistency_unknown(self, detail: str) -> None:
        self._interface.Notices[TEMPERATURE_PRESENTATION_NOTICE_KEY] = json.dumps(
            {
                "status": "temperature_presentation_consistency_unknown",
                "detail": detail,
                "authoritative_preference": {
                    "unit": self._temperature_presentation.unit.value,
                    "decimal_places": self._temperature_presentation.decimal_places,
                },
                "profile_consistency_confirmed": False,
                "further_changes_allowed": False,
                "recovery": (
                    "Restart eisy-onewire through the supported eisy-ui lifecycle. "
                    "Normal startup reapplies the persisted authoritative profile "
                    "before registering nodes."
                ),
                "usb_activity": False,
            },
            sort_keys=True,
        )
        self._logger.error("Temperature presentation consistency unknown: %s", detail)


def _enumerate_adapter_descriptors() -> tuple[UsbDeviceDescriptor, ...]:
    result = enumerate_ds9490()
    if result.error_code is not None:
        raise RuntimeError(
            f"descriptor enumeration failed with libusb error {result.error_code}: "
            f"{result.detail or 'unknown error'}"
        )
    return result.descriptors


def _sensor_address(item: KnownDevice) -> str:
    from .node_identity import sensor_address

    return sensor_address(item.rom)


def _event_acknowledgement_payload(
    acknowledgements: tuple[EventFlagAcknowledgement, ...]
) -> list[dict[str, object]]:
    return [
        {
            "coupler_rom": item.coupler_rom.hex().upper(),
            "raw_status": f"0x{item.raw_status:02X}",
            "main_event": item.main_event,
            "aux_event": item.aux_event,
            "irreversible_loss": True,
        }
        for item in acknowledgements
    ]


def _device_kind(family_code: int) -> str:
    if family_code == DS18B20_FAMILY:
        return "ds18b20_temperature"
    if family_code == DS2438_FAMILY:
        return "ds2438_inventory_only"
    if family_code == DS2409_FAMILY:
        return "ds2409_coupler"
    if family_code == DS9490_ID_FAMILY:
        return "ds9490_identification"
    return "unsupported_family"
