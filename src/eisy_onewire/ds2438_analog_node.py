"""IoX nodes for explicitly selected DS2438 analog applications."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_EVEN
import json
import threading
from typing import Any, TypeVar

from .ds2409_transport import BranchHop, branch_path_label
from .ds2438 import parse_ds2438_rom
from .ds2438_analog import (
    AnalogVoltageReading,
    CachedAnalogReading,
    SolarCalibration,
    SolarIrradianceReading,
    analog_reading_from_measurement,
    solar_reading_from_analog,
)
from .ds2438_diagnostic import Ds2438BranchTarget
from .ds2438_service import (
    BranchDs2438MeasurementAdapter,
    Ds2438RawAcquisition,
    RootDs2438MeasurementAdapter,
)


ANALOG_NODE_DEFINITION_ID = "ds2438_analog_voltage"
SOLAR_NODE_DEFINITION_ID = "ds2438_solar_irradiance"
ANALOG_APPLICATION = "analog_voltage"
SOLAR_APPLICATION = "solar_irradiance"
PRIMARY_DRIVER = "ST"
AVAILABILITY_DRIVER = "GV0"
REFRESH_INTERVAL_DRIVER = "GV1"
SECONDARY_DRIVER = "GV2"
UOM_BOOLEAN = 2
UOM_INDEX = 25
UOM_VOLTS = 72
UOM_WATTS_PER_SQUARE_METER = 74
REFRESH_COMMAND = "REFRESH"
REFRESH_INTERVALS = {"REFRESH_OFF": 0, "REFRESH_30": 30, "REFRESH_60": 60,
                     "REFRESH_120": 120, "REFRESH_300": 300}

NodeType = TypeVar("NodeType")


def create_ds2438_analog_node_class(
    node_base: type[NodeType],
    *,
    logger: Any,
    root_service_factory: Callable[[], Any] = RootDs2438MeasurementAdapter,
    branch_service_factory: Callable[[], Any] = BranchDs2438MeasurementAdapter,
) -> type[NodeType]:
    return _create_node_class(
        node_base,
        logger=logger,
        application=ANALOG_APPLICATION,
        node_definition=ANALOG_NODE_DEFINITION_ID,
        root_service_factory=root_service_factory,
        branch_service_factory=branch_service_factory,
    )


def create_ds2438_solar_node_class(
    node_base: type[NodeType],
    *,
    logger: Any,
    root_service_factory: Callable[[], Any] = RootDs2438MeasurementAdapter,
    branch_service_factory: Callable[[], Any] = BranchDs2438MeasurementAdapter,
) -> type[NodeType]:
    return _create_node_class(
        node_base,
        logger=logger,
        application=SOLAR_APPLICATION,
        node_definition=SOLAR_NODE_DEFINITION_ID,
        root_service_factory=root_service_factory,
        branch_service_factory=branch_service_factory,
    )


def _create_node_class(
    node_base: type[NodeType],
    *,
    logger: Any,
    application: str,
    node_definition: str,
    root_service_factory: Callable[[], Any],
    branch_service_factory: Callable[[], Any],
) -> type[NodeType]:
    application_value = application

    class Ds2438AnalogNode(node_base):  # type: ignore[misc, valid-type]
        id = node_definition
        drivers = [
            {"driver": AVAILABILITY_DRIVER, "value": 0, "uom": UOM_BOOLEAN},
            {"driver": REFRESH_INTERVAL_DRIVER, "value": 0, "uom": UOM_INDEX},
        ]
        application = application_value

        def __init__(
            self,
            polyglot: Any,
            *,
            primary: str,
            address: str,
            name: str,
            rom: bytes,
            adapter_lock: threading.Lock,
            cached_reading: CachedAnalogReading | None = None,
            calibration: SolarCalibration | None = None,
            configuration_error: str | None = None,
            branch_path: tuple[BranchHop, ...] = (),
            branch_target: Ds2438BranchTarget | None = None,
            branch_event_clear_allowed: bool = False,
            branch_refresh_error: str | None = None,
            refresh_interval_seconds: int = 0,
            set_refresh_interval: Callable[[int], bool] = lambda interval: False,
            record_measurement: Callable[[CachedAnalogReading], bool] = lambda reading: True,
        ) -> None:
            validated_rom = parse_ds2438_rom(rom.hex())
            if not address or not name.strip():
                raise ValueError("DS2438 node address and name are required")
            super().__init__(polyglot, primary, address, name.strip())
            self.drivers = deepcopy(type(self).drivers)
            self.rom = validated_rom
            self._adapter_lock = adapter_lock
            self._branch_path = _branch_path(branch_path)
            self._branch_target = branch_target
            self._branch_event_clear_allowed = branch_event_clear_allowed
            self._branch_refresh_error = branch_refresh_error
            self._refresh_interval_seconds = refresh_interval_seconds
            self._set_refresh_interval = set_refresh_interval
            self._record_measurement = record_measurement
            self._application_authorized = True
            self._calibration = calibration
            self._configuration_error = configuration_error
            self._cached_reading = _matching_cache(cached_reading, application, calibration)
            self._last_refresh_error: str | None = None
            self._notice_key = f"ds2438_{address}"
            if self._cached_reading is not None:
                self._set_cached_drivers(self._cached_reading, report=False)
            self.setDriver(AVAILABILITY_DRIVER, 0,
                           report=self._cached_reading is not None, force=True,
                           uom=UOM_BOOLEAN)
            self.setDriver(REFRESH_INTERVAL_DRIVER, refresh_interval_seconds,
                           report=False, force=True, uom=UOM_INDEX)

        def refresh_measurements(self, command: object | None = None) -> bool:
            del command
            return self._refresh(branch_event_clear_allowed=self._branch_event_clear_allowed)

        refresh_temperature = refresh_measurements

        def scheduled_refresh_temperature(self, *, branch_event_clear_allowed: bool) -> bool:
            return self._refresh(branch_event_clear_allowed=branch_event_clear_allowed)

        def _refresh(self, *, branch_event_clear_allowed: bool) -> bool:
            if not self._application_authorized:
                return self._fail("ApplicationUnavailable", "DS2438 application is not authorized")
            if self._configuration_error is not None:
                return self._fail("ApplicationConfigurationError", self._configuration_error)
            if application == SOLAR_APPLICATION and self._calibration is None:
                return self._fail("CalibrationRequired", "valid solar calibration is required before acquisition")
            if not self._adapter_lock.acquire(blocking=False):
                return self._fail("AdapterBusyError", "adapter is busy")
            try:
                if self._branch_path:
                    if self._branch_target is None:
                        return self._fail("BranchTargetUnavailable", self._branch_refresh_error or "validated persisted branch target is unavailable")
                    acquisition = branch_service_factory().read(
                        self._branch_target, allow_event_clear=branch_event_clear_allowed
                    )
                else:
                    acquisition = root_service_factory().read(self.rom)
                reading: CachedAnalogReading = analog_reading_from_measurement(acquisition.measurement)
                if application == SOLAR_APPLICATION:
                    assert self._calibration is not None
                    reading = solar_reading_from_analog(reading, self._calibration)
                if not self._record_measurement(reading):
                    raise RuntimeError("DS2438 analog cache persistence failed")
                self._cached_reading = reading
                first_measurement = self._driver(PRIMARY_DRIVER) is None
                self._set_cached_drivers(reading, report=False)
                if first_measurement:
                    self.poly.addNode(self)
                self.setDriver(AVAILABILITY_DRIVER, 1, report=False, force=True, uom=UOM_BOOLEAN)
                self.reportDriver(PRIMARY_DRIVER, True)
                self.reportDriver(SECONDARY_DRIVER, True)
                self.reportDriver(AVAILABILITY_DRIVER, True)
                self._last_refresh_error = None
                self.poly.Notices.delete(self._notice_key)
                self._report_branch_evidence(acquisition)
                logger.info("%s (%s) refreshed for %s: VAD %.2f V, VDD %.2f V",
                            self.name, self.rom.hex().upper(), application,
                            reading.vad_volts, reading.vdd_volts)
                return True
            except Exception as exc:
                return self._fail(type(exc).__name__, str(exc), exc=exc)
            finally:
                self._adapter_lock.release()

        def _fail(self, error_type: str, detail: str, *, exc: Exception | None = None) -> bool:
            self._last_refresh_error = f"{error_type}: {detail}"
            self.setDriver(AVAILABILITY_DRIVER, 0, report=True, force=True, uom=UOM_BOOLEAN)
            self.poly.Notices[self._notice_key] = json.dumps({
                "status": "unavailable", "application": application,
                "rom": self.rom.hex().upper(), "error_type": error_type,
                "detail": detail, "last_valid_measurement_stale": self._cached_reading is not None,
                "restoration_verified": getattr(exc, "restoration_verified", None),
                "automatic_retries": 0, "eeprom_writes": 0,
            }, sort_keys=True)
            return False

        def _report_branch_evidence(self, acquisition: Ds2438RawAcquisition) -> None:
            result = acquisition.branch_result
            if result is not None:
                self.poly.Notices[self._notice_key] = json.dumps({
                    "status": "ok", "application": application,
                    "rom": self.rom.hex().upper(),
                    "branch_path": branch_path_label(self._branch_path),
                    "restoration_verified": True,
                    "event_flags_cleared": result.event_flags_cleared,
                    "event_evidence": list(result.event_evidence),
                    "automatic_retries": 0, "eeprom_writes": 0,
                }, sort_keys=True)

        def set_branch_target(self, branch_path: tuple[BranchHop, ...], target: Ds2438BranchTarget | None, *, event_clear_allowed: bool, error: str | None) -> None:
            self._branch_path = _branch_path(branch_path)
            self._branch_target = target
            self._branch_event_clear_allowed = event_clear_allowed
            self._branch_refresh_error = error

        def set_application_authorized(self, authorized: bool) -> None:
            self._application_authorized = bool(authorized)
            if not authorized:
                self.setDriver(AVAILABILITY_DRIVER, 0, report=self._cached_reading is not None, force=True, uom=UOM_BOOLEAN)

        def set_calibration(self, calibration: SolarCalibration | None, error: str | None) -> None:
            if calibration == self._calibration and error == self._configuration_error:
                return
            self._calibration = calibration
            self._configuration_error = error
            # A calibration change never transforms an old derived value into a
            # fresh reading. Raw cache remains available for audit only.
            self.setDriver(AVAILABILITY_DRIVER, 0, report=self._cached_reading is not None, force=True, uom=UOM_BOOLEAN)

        def set_desired_refresh_interval(self, seconds: int) -> None:
            self._refresh_interval_seconds = seconds
            self.setDriver(REFRESH_INTERVAL_DRIVER, seconds, report=False, force=True, uom=UOM_INDEX)

        def _set_refresh_interval_value(self, seconds: int) -> bool:
            if not self._set_refresh_interval(seconds):
                return False
            self.set_desired_refresh_interval(seconds)
            self.reportDriver(REFRESH_INTERVAL_DRIVER, True)
            return True

        def query(self, command: object | None = None) -> None:
            del command
            if self._cached_reading is not None:
                self.reportDriver(PRIMARY_DRIVER, True)
                self.reportDriver(SECONDARY_DRIVER, True)
            self.reportDriver(AVAILABILITY_DRIVER, True)
            self.reportDriver(REFRESH_INTERVAL_DRIVER, True)

        def last_refresh_error(self) -> str | None:
            return self._last_refresh_error

        def _set_cached_drivers(self, reading: CachedAnalogReading, *, report: bool) -> None:
            if application == ANALOG_APPLICATION:
                assert isinstance(reading, AnalogVoltageReading) and not isinstance(reading, SolarIrradianceReading)
                primary, primary_uom = _round(reading.vad_volts, 2), UOM_VOLTS
                secondary = _round(reading.vdd_volts, 2)
            else:
                assert isinstance(reading, SolarIrradianceReading)
                primary, primary_uom = _round(reading.irradiance_w_m2, 1), UOM_WATTS_PER_SQUARE_METER
                secondary = _round(reading.vad_volts, 2)
            self._set_or_append(PRIMARY_DRIVER, primary, primary_uom, report)
            self._set_or_append(SECONDARY_DRIVER, secondary, UOM_VOLTS, report)

        def _set_or_append(self, driver: str, value: float, uom: int, report: bool) -> None:
            if self._driver(driver) is None:
                self.drivers.append({"driver": driver, "value": value, "uom": uom})
            else:
                self.setDriver(driver, value, report=report, force=True, uom=uom)

        def _driver(self, driver: str) -> dict[str, object] | None:
            return next((item for item in self.drivers if item.get("driver") == driver), None)

        def set_refresh_off(self, command=None): return self._set_refresh_interval_value(0)
        def set_refresh_30(self, command=None): return self._set_refresh_interval_value(30)
        def set_refresh_60(self, command=None): return self._set_refresh_interval_value(60)
        def set_refresh_120(self, command=None): return self._set_refresh_interval_value(120)
        def set_refresh_300(self, command=None): return self._set_refresh_interval_value(300)

        commands = {
            REFRESH_COMMAND: refresh_measurements,
            "REFRESH_OFF": set_refresh_off, "REFRESH_30": set_refresh_30,
            "REFRESH_60": set_refresh_60, "REFRESH_120": set_refresh_120,
            "REFRESH_300": set_refresh_300,
        }

    Ds2438AnalogNode.__name__ = (
        "Ds2438SolarIrradianceNode" if application == SOLAR_APPLICATION
        else "Ds2438AnalogVoltageNode"
    )
    Ds2438AnalogNode.__qualname__ = Ds2438AnalogNode.__name__
    return Ds2438AnalogNode


def _matching_cache(reading: CachedAnalogReading | None, application: str, calibration: SolarCalibration | None) -> CachedAnalogReading | None:
    if reading is None:
        return None
    if application == ANALOG_APPLICATION:
        return reading if type(reading) is AnalogVoltageReading else None
    if isinstance(reading, SolarIrradianceReading) and reading.calibration == calibration:
        return reading
    return None


def _round(value: float, places: int) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_EVEN))


def _branch_path(value: object) -> tuple[BranchHop, ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, BranchHop) for item in value):
        raise ValueError("branch path must contain validated BranchHop values")
    return value
