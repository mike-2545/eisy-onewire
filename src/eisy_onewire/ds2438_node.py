"""IoX temperature/humidity node for an explicitly identified HIH-4000."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_EVEN
import json
import threading
from typing import Any, TypeVar

from .ds2409_transport import BranchHop, branch_path_label
from .ds2438 import parse_ds2438_rom
from .ds2438_diagnostic import Ds2438BranchTarget
from .ds2438_humidity import Hih4000Reading
from .ds2438_service import (
    BranchDs2438Adapter,
    Ds2438Acquisition,
    RootDs2438Adapter,
)
from .temperature_presentation import TemperaturePresentation


NODE_DEFINITION_ID = "ds2438_hih4000"
TEMPERATURE_DRIVER = "ST"
AVAILABILITY_DRIVER = "GV0"
REFRESH_INTERVAL_DRIVER = "GV1"
HUMIDITY_DRIVER = "GV2"
UOM_BOOLEAN = 2
UOM_INDEX = 25
UOM_PERCENT = 51
REFRESH_COMMAND = "REFRESH"
REFRESH_INTERVALS = {
    "REFRESH_OFF": 0,
    "REFRESH_30": 30,
    "REFRESH_60": 60,
    "REFRESH_120": 120,
    "REFRESH_300": 300,
}


NodeType = TypeVar("NodeType")


def create_ds2438_node_class(
    node_base: type[NodeType],
    *,
    logger: Any,
    root_service_factory: Callable[[], Any] = RootDs2438Adapter,
    branch_service_factory: Callable[[], Any] = BranchDs2438Adapter,
) -> type[NodeType]:
    """Create the reusable family-0x26 HIH-4000 node class."""

    class Ds2438Hih4000Node(node_base):  # type: ignore[misc, valid-type]
        id = NODE_DEFINITION_ID
        drivers = [
            {"driver": AVAILABILITY_DRIVER, "value": 0, "uom": UOM_BOOLEAN},
            {"driver": REFRESH_INTERVAL_DRIVER, "value": 0, "uom": UOM_INDEX},
        ]

        def __init__(
            self,
            polyglot: Any,
            *,
            primary: str,
            address: str,
            name: str,
            rom: bytes,
            adapter_lock: threading.Lock,
            cached_reading: Hih4000Reading | None = None,
            branch_path: tuple[BranchHop, ...] = (),
            branch_target: Ds2438BranchTarget | None = None,
            branch_event_clear_allowed: bool = False,
            branch_refresh_error: str | None = None,
            refresh_interval_seconds: int = 0,
            set_refresh_interval: Callable[[int], bool] = lambda interval: False,
            temperature_presentation: TemperaturePresentation = TemperaturePresentation(),
            record_measurement: Callable[[Hih4000Reading], bool] = lambda reading: True,
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
            self._temperature_presentation = temperature_presentation
            self._record_measurement = record_measurement
            self._application_authorized = True
            self._cached_reading = cached_reading
            self._last_refresh_error: str | None = None
            self._notice_key = f"ds2438_{address}"
            if cached_reading is not None:
                if cached_reading.rom != validated_rom:
                    raise ValueError("cached DS2438 reading ROM does not match node")
                self._set_cached_drivers(cached_reading, report=False)
            self.setDriver(
                AVAILABILITY_DRIVER, 0, report=cached_reading is not None,
                force=True, uom=UOM_BOOLEAN,
            )
            self.setDriver(
                REFRESH_INTERVAL_DRIVER, refresh_interval_seconds,
                report=False, force=True, uom=UOM_INDEX,
            )

        def refresh_measurements(self, command: object | None = None) -> bool:
            del command
            return self._refresh(
                branch_event_clear_allowed=self._branch_event_clear_allowed
            )

        def scheduled_refresh_temperature(
            self, *, branch_event_clear_allowed: bool
        ) -> bool:
            return self._refresh(
                branch_event_clear_allowed=branch_event_clear_allowed
            )

        def _refresh(self, *, branch_event_clear_allowed: bool) -> bool:
            if not self._application_authorized:
                return self._fail(
                    "ApplicationUnassigned",
                    "DS2438 application is unassigned; hardware acquisition is disabled",
                )
            if not self._adapter_lock.acquire(blocking=False):
                return self._fail("AdapterBusyError", "adapter is busy")
            try:
                if self._branch_path:
                    if self._branch_target is None:
                        return self._fail(
                            "BranchTargetUnavailable",
                            self._branch_refresh_error
                            or "validated persisted branch target is unavailable",
                        )
                    acquisition = branch_service_factory().read(
                        self._branch_target,
                        allow_event_clear=branch_event_clear_allowed,
                    )
                else:
                    acquisition = root_service_factory().read(self.rom)
                reading = acquisition.reading
                if not self._record_measurement(reading):
                    raise RuntimeError("DS2438 measurement cache persistence failed")
                self._cached_reading = reading
                first_measurement = self._driver(TEMPERATURE_DRIVER) is None
                self._set_cached_drivers(reading, report=False)
                if first_measurement:
                    self.poly.addNode(self)
                self.setDriver(
                    AVAILABILITY_DRIVER, 1, report=False, force=True, uom=UOM_BOOLEAN
                )
                self.reportDriver(TEMPERATURE_DRIVER, True)
                self.reportDriver(HUMIDITY_DRIVER, True)
                self.reportDriver(AVAILABILITY_DRIVER, True)
                self._last_refresh_error = None
                self.poly.Notices.delete(self._notice_key)
                self._report_branch_evidence(acquisition)
                logger.info(
                    "%s (%s) refreshed: %.4f C, %.4f %%RH, VAD %.3f V, VDD %.3f V",
                    self.name,
                    self.rom.hex().upper(),
                    reading.temperature_celsius,
                    reading.humidity_percent,
                    reading.vad_volts,
                    reading.vdd_volts,
                )
                return True
            except Exception as exc:
                return self._fail(type(exc).__name__, str(exc), exc=exc)
            finally:
                self._adapter_lock.release()

        # AutomaticRefreshScheduler uses the established DS18B20 method name.
        refresh_temperature = refresh_measurements

        def _fail(
            self, error_type: str, detail: str, *, exc: Exception | None = None
        ) -> bool:
            self._last_refresh_error = f"{error_type}: {detail}"
            self.setDriver(
                AVAILABILITY_DRIVER, 0, report=True, force=True, uom=UOM_BOOLEAN
            )
            payload = {
                "status": "unavailable",
                "rom": self.rom.hex().upper(),
                "last_valid_measurement_stale": self._cached_reading is not None,
                "error_type": error_type,
                "detail": detail,
                "branch_path": (
                    branch_path_label(self._branch_path) if self._branch_path else None
                ),
                "restoration_verified": getattr(exc, "restoration_verified", None),
                "automatic_retries": 0,
                "eeprom_writes": 0,
            }
            self.poly.Notices[self._notice_key] = json.dumps(payload, sort_keys=True)
            logger.error("%s DS2438 refresh failed: %s", self.name, payload)
            return False

        def _report_branch_evidence(self, acquisition: Ds2438Acquisition) -> None:
            result = acquisition.branch_result
            if result is None:
                return
            payload = {
                "status": "ok",
                "rom": self.rom.hex().upper(),
                "branch_path": branch_path_label(self._branch_path),
                "restoration_verified": True,
                "event_flags_cleared": result.event_flags_cleared,
                "event_evidence": list(result.event_evidence),
                "automatic_retries": 0,
                "eeprom_writes": 0,
            }
            self.poly.Notices[self._notice_key] = json.dumps(payload, sort_keys=True)

        def set_branch_target(
            self,
            branch_path: tuple[BranchHop, ...],
            target: Ds2438BranchTarget | None,
            *,
            event_clear_allowed: bool,
            error: str | None,
        ) -> None:
            validated_path = _branch_path(branch_path)
            changed = (
                validated_path != self._branch_path
                or target != self._branch_target
                or event_clear_allowed != self._branch_event_clear_allowed
                or error != self._branch_refresh_error
            )
            self._branch_path = validated_path
            self._branch_target = target
            self._branch_event_clear_allowed = event_clear_allowed
            self._branch_refresh_error = error
            if changed and self._branch_path:
                self.setDriver(
                    AVAILABILITY_DRIVER, 0,
                    report=self._cached_reading is not None,
                    force=True, uom=UOM_BOOLEAN,
                )

        def set_application_authorized(self, authorized: bool) -> None:
            """Apply the explicit application selector without touching USB."""

            if not isinstance(authorized, bool):
                raise TypeError("application authorization must be Boolean")
            if authorized == self._application_authorized:
                return
            self._application_authorized = authorized
            if not authorized:
                self.setDriver(
                    AVAILABILITY_DRIVER, 0,
                    report=self._cached_reading is not None,
                    force=True, uom=UOM_BOOLEAN,
                )

        def set_temperature_presentation(
            self, presentation: TemperaturePresentation
        ) -> None:
            if presentation == self._temperature_presentation:
                return
            self._temperature_presentation = presentation
            if self._cached_reading is not None:
                value = presentation.publish(self._cached_reading.temperature_celsius)
                current = self._driver(TEMPERATURE_DRIVER)
                changed = (
                    current is None
                    or current.get("value") != value
                    or current.get("uom") != presentation.unit.uom
                )
                self.setDriver(
                    TEMPERATURE_DRIVER,
                    value,
                    report=changed,
                    force=False,
                    uom=presentation.unit.uom,
                )

        def set_desired_refresh_interval(self, seconds: int) -> None:
            self._refresh_interval_seconds = seconds
            self.setDriver(
                REFRESH_INTERVAL_DRIVER, seconds, report=False, force=True, uom=UOM_INDEX
            )

        def _set_refresh_interval_value(self, seconds: int) -> bool:
            if not self._set_refresh_interval(seconds):
                return False
            self.set_desired_refresh_interval(seconds)
            self.reportDriver(REFRESH_INTERVAL_DRIVER, True)
            return True

        def query(self, command: object | None = None) -> None:
            del command
            if self._cached_reading is not None:
                self.reportDriver(TEMPERATURE_DRIVER, True)
                self.reportDriver(HUMIDITY_DRIVER, True)
            self.reportDriver(AVAILABILITY_DRIVER, True)
            self.reportDriver(REFRESH_INTERVAL_DRIVER, True)

        def last_refresh_error(self) -> str | None:
            return self._last_refresh_error

        def _set_cached_drivers(
            self, reading: Hih4000Reading, *, report: bool
        ) -> None:
            self._set_or_append(
                TEMPERATURE_DRIVER,
                self._temperature_presentation.publish(reading.temperature_celsius),
                self._temperature_presentation.unit.uom,
                report,
            )
            humidity = float(
                Decimal(str(reading.humidity_percent)).quantize(
                    Decimal("0.1"), rounding=ROUND_HALF_EVEN
                )
            )
            self._set_or_append(HUMIDITY_DRIVER, humidity, UOM_PERCENT, report)

        def _set_or_append(
            self, driver: str, value: float, uom: int, report: bool
        ) -> None:
            if self._driver(driver) is None:
                self.drivers.append({"driver": driver, "value": value, "uom": uom})
            else:
                self.setDriver(driver, value, report=report, force=True, uom=uom)

        def _driver(self, driver: str) -> dict[str, object] | None:
            return next(
                (item for item in self.drivers if item.get("driver") == driver), None
            )

        def set_refresh_off(self, command: object | None = None) -> bool:
            del command
            return self._set_refresh_interval_value(0)

        def set_refresh_30(self, command: object | None = None) -> bool:
            del command
            return self._set_refresh_interval_value(30)

        def set_refresh_60(self, command: object | None = None) -> bool:
            del command
            return self._set_refresh_interval_value(60)

        def set_refresh_120(self, command: object | None = None) -> bool:
            del command
            return self._set_refresh_interval_value(120)

        def set_refresh_300(self, command: object | None = None) -> bool:
            del command
            return self._set_refresh_interval_value(300)

        commands = {
            REFRESH_COMMAND: refresh_measurements,
            "REFRESH_OFF": set_refresh_off,
            "REFRESH_30": set_refresh_30,
            "REFRESH_60": set_refresh_60,
            "REFRESH_120": set_refresh_120,
            "REFRESH_300": set_refresh_300,
        }

    Ds2438Hih4000Node.__name__ = "Ds2438Hih4000Node"
    Ds2438Hih4000Node.__qualname__ = "Ds2438Hih4000Node"
    return Ds2438Hih4000Node


def _branch_path(value: object) -> tuple[BranchHop, ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, BranchHop) for item in value):
        raise ValueError("branch path must contain validated BranchHop values")
    return value
