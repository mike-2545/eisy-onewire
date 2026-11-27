"""Reusable IoX presentation for one configured DS18B20 ROM."""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from .branch_temperature import (
    BranchTemperatureAdapter,
    BranchTemperatureResult,
    BranchTemperatureTarget,
)
from .ds18b20 import parse_ds18b20_rom
from .ds2409_transport import BranchHop, branch_path_label
from .temperature_service import FreshTemperature, TemperatureService
from .temperature_presentation import (
    TemperaturePresentation,
    TemperaturePresentationError,
    UOM_CELSIUS,
    UOM_FAHRENHEIT,
    celsius_from_published,
)


NODE_DEFINITION_ID = "ds18b20"
TEMPERATURE_DRIVER = "ST"
AVAILABILITY_DRIVER = "GV0"
REFRESH_INTERVAL_DRIVER = "GV1"
UOM_BOOLEAN = 2
UOM_INDEX = 25
REFRESH_COMMAND = "REFRESH"
REFRESH_OFF_COMMAND = "REFRESH_OFF"
REFRESH_30_COMMAND = "REFRESH_30"
REFRESH_60_COMMAND = "REFRESH_60"
REFRESH_120_COMMAND = "REFRESH_120"
REFRESH_300_COMMAND = "REFRESH_300"
REFRESH_INTERVALS = {
    REFRESH_OFF_COMMAND: 0,
    REFRESH_30_COMMAND: 30,
    REFRESH_60_COMMAND: 60,
    REFRESH_120_COMMAND: 120,
    REFRESH_300_COMMAND: 300,
}


class Logger(Protocol):
    def info(self, message: str, *args: object) -> None: ...

    def warning(self, message: str, *args: object) -> None: ...

    def error(self, message: str, *args: object) -> None: ...


class TemperatureReader(Protocol):
    def read(self, rom: bytes) -> FreshTemperature: ...


class BranchTemperatureReader(Protocol):
    def read(
        self,
        target: BranchTemperatureTarget,
        *,
        allow_event_clear: bool = False,
    ) -> BranchTemperatureResult: ...


NodeType = TypeVar("NodeType")


def create_ds18b20_node_class(
    node_base: type[NodeType],
    *,
    logger: Logger,
    service_factory: Callable[[], TemperatureReader] = TemperatureService,
    branch_service_factory: Callable[[], BranchTemperatureReader] = (
        BranchTemperatureAdapter
    ),
) -> type[NodeType]:
    """Create one reusable node class; identity is supplied per instance."""

    class Ds18b20Node(node_base):  # type: ignore[misc, valid-type]
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
            branch_path: tuple[BranchHop, ...] = (),
            branch_target: BranchTemperatureTarget | None = None,
            branch_event_clear_allowed: bool = False,
            branch_refresh_error: str | None = None,
            refresh_interval_seconds: int = 0,
            set_refresh_interval: Callable[[int], bool] = lambda interval: False,
            temperature_presentation: TemperaturePresentation = TemperaturePresentation(),
            record_temperature: Callable[[bytes, float], bool] = (
                lambda rom, celsius: True
            ),
        ) -> None:
            validated_rom = parse_ds18b20_rom(rom.hex())
            if not address or not name.strip():
                raise ValueError("sensor address and friendly name are required")
            super().__init__(polyglot, primary, address, name.strip())
            self.rom = validated_rom
            self._adapter_lock = adapter_lock
            self._branch_path = _validated_branch_path(branch_path)
            self._branch_target = branch_target
            self._branch_event_clear_allowed = branch_event_clear_allowed
            self._branch_refresh_error = branch_refresh_error
            self._refresh_interval_seconds = refresh_interval_seconds
            self._set_refresh_interval = set_refresh_interval
            self._temperature_presentation = temperature_presentation
            self._record_temperature = record_temperature
            self._notice_key = f"temperature_{address}"
            self._last_refresh_error: str | None = None
            self._restore_persisted_temperature(polyglot)
            self._raw_temperature_celsius = temperature_presentation.raw_celsius(
                validated_rom
            )
            if self._raw_temperature_celsius is None:
                self._raw_temperature_celsius = self._legacy_raw_celsius()
                if self._raw_temperature_celsius is not None:
                    # Preserve the full legacy value before later presentation
                    # rounding can replace the persisted IoX ST representation.
                    self._record_temperature(
                        self.rom, self._raw_temperature_celsius
                    )
            if self._raw_temperature_celsius is not None:
                self._apply_temperature_presentation(report=False)
            self._has_valid_temperature = self._temperature_driver_is_numeric()
            self.setDriver(
                AVAILABILITY_DRIVER,
                0,
                report=self._has_valid_temperature,
                force=True,
                uom=UOM_BOOLEAN,
            )
            self.setDriver(
                REFRESH_INTERVAL_DRIVER,
                refresh_interval_seconds,
                report=False,
                force=True,
                uom=UOM_INDEX,
            )

        def refresh_temperature(self, command: object | None = None) -> bool:
            """Run one fresh read while rejecting concurrent adapter operations."""

            del command
            return self._refresh_temperature(
                branch_event_clear_allowed=self._branch_event_clear_allowed
            )

        def scheduled_refresh_temperature(
            self, *, branch_event_clear_allowed: bool
        ) -> bool:
            """Run the same read path using only the unattended branch policy."""

            return self._refresh_temperature(
                branch_event_clear_allowed=branch_event_clear_allowed
            )

        def _refresh_temperature(self, *, branch_event_clear_allowed: bool) -> bool:
            if not self._adapter_lock.acquire(blocking=False):
                self._last_refresh_error = "adapter is busy"
                self.setDriver(
                    AVAILABILITY_DRIVER,
                    0,
                    report=True,
                    force=True,
                    uom=UOM_BOOLEAN,
                )
                logger.warning(
                    "%s temperature refresh rejected: adapter is busy", self.name
                )
                return False
            try:
                branch_result = None
                if self._branch_path:
                    if self._branch_target is None:
                        return self._reject_branch_refresh()
                    branch_result = branch_service_factory().read(
                        self._branch_target,
                        allow_event_clear=branch_event_clear_allowed,
                    )
                    measurement = branch_result.measurement
                else:
                    measurement = service_factory().read(self.rom)
                raw_celsius = measurement.temperature_celsius
                if (
                    not math.isfinite(raw_celsius)
                    or not -55.0 <= raw_celsius <= 125.0
                ):
                    raise ValueError(
                        "temperature result is outside the DS18B20 Celsius range"
                    )
                if not self._record_temperature(self.rom, raw_celsius):
                    raise RuntimeError("raw temperature cache persistence failed")
                self._raw_temperature_celsius = raw_celsius
                value = self._temperature_presentation.publish(raw_celsius)
                value_uom = self._temperature_presentation.unit.uom
                first_temperature = not self._has_driver(TEMPERATURE_DRIVER)
                if first_temperature:
                    self.drivers.append(
                        {
                            "driver": TEMPERATURE_DRIVER,
                            "value": value,
                            "uom": value_uom,
                        }
                    )
                else:
                    self.setDriver(
                        TEMPERATURE_DRIVER,
                        value,
                        report=False,
                        force=True,
                        uom=value_uom,
                    )
                self._has_valid_temperature = True
                self._last_refresh_error = None
                self.setDriver(
                    AVAILABILITY_DRIVER,
                    1,
                    report=False,
                    force=True,
                    uom=UOM_BOOLEAN,
                )
                if first_temperature:
                    self.poly.addNode(self)
                self.reportDriver(TEMPERATURE_DRIVER, True)
                self.reportDriver(AVAILABILITY_DRIVER, True)
                if branch_result is None:
                    self.poly.Notices.delete(self._notice_key)
                else:
                    branch_evidence = {
                        "status": "ok",
                        "rom": self.rom.hex().upper(),
                        "branch_path": branch_path_label(self._branch_path),
                        "restoration_verified": True,
                        "event_clear_authorized": (
                            branch_result.event_clear_authorized
                        ),
                        "event_flags_cleared": branch_result.event_flags_cleared,
                        "event_evidence": _event_evidence_payload(
                            branch_result.event_evidence
                        ),
                        "detail": (
                            "DS2409 event flags were explicitly acknowledged "
                            "and cannot be restored"
                            if branch_result.event_flags_cleared
                            else "DS2409 event-latch evidence retained; no "
                            "flag clear was observed"
                        ),
                    }
                    rendered_evidence = json.dumps(branch_evidence, sort_keys=True)
                    self.poly.Notices[self._notice_key] = rendered_evidence
                    if branch_result.event_flags_cleared:
                        logger.warning(
                            "%s branch temperature read irreversibly cleared "
                            "acknowledged DS2409 event flags: %s",
                            self.name,
                            rendered_evidence,
                        )
                    else:
                        logger.info(
                            "%s branch temperature event/restoration evidence: %s",
                            self.name,
                            rendered_evidence,
                        )
                logger.info(
                    "%s (%s) temperature refreshed: %.4f C, published %s UOM %d "
                    "(%d-bit, %s power)",
                    self.name,
                    self.rom.hex().upper(),
                    raw_celsius,
                    value,
                    value_uom,
                    measurement.resolution_bits,
                    measurement.power_mode.value,
                )
                return True
            except Exception as exc:
                self._last_refresh_error = f"{type(exc).__name__}: {exc}"
                self.setDriver(
                    AVAILABILITY_DRIVER,
                    0,
                    report=True,
                    force=True,
                    uom=UOM_BOOLEAN,
                )
                detail = json.dumps(
                    {
                        "status": "unavailable",
                        "rom": self.rom.hex().upper(),
                        "last_valid_temperature_stale": self._has_valid_temperature,
                        "error_type": type(exc).__name__,
                        "detail": str(exc),
                        "branch_path": (
                            branch_path_label(self._branch_path)
                            if self._branch_path
                            else None
                        ),
                        "restoration_verified": getattr(
                            exc, "restoration_verified", None
                        ),
                        "event_state_unknown_before_clear": getattr(
                            exc, "event_state_unknown_before_clear", False
                        ),
                        "event_evidence": _event_evidence_payload(
                            getattr(exc, "event_evidence", ())
                        ),
                    },
                    sort_keys=True,
                )
                self.poly.Notices[self._notice_key] = detail
                logger.error("%s temperature refresh failed: %s", self.name, detail)
                return False
            finally:
                self._adapter_lock.release()

        def set_branch_path(self, branch_path: tuple[BranchHop, ...]) -> None:
            """Update persisted topology without performing a bus operation."""

            self._branch_path = _validated_branch_path(branch_path)
            self._branch_target = None
            self._branch_refresh_error = (
                "validated persisted branch target is unavailable"
                if self._branch_path
                else None
            )
            if self._branch_path:
                self.setDriver(
                    AVAILABILITY_DRIVER,
                    0,
                    report=self._has_valid_temperature,
                    force=True,
                    uom=UOM_BOOLEAN,
                )

        def set_branch_target(
            self,
            branch_path: tuple[BranchHop, ...],
            target: BranchTemperatureTarget | None,
            *,
            event_clear_allowed: bool,
            error: str | None,
        ) -> None:
            """Apply only a registry-validated persisted topology target."""

            validated_path = _validated_branch_path(branch_path)
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
                    AVAILABILITY_DRIVER,
                    0,
                    report=self._has_valid_temperature,
                    force=True,
                    uom=UOM_BOOLEAN,
                )

        def _reject_branch_refresh(self) -> bool:
            self._last_refresh_error = (
                self._branch_refresh_error
                or "validated persisted branch target is unavailable"
            )
            self.setDriver(
                AVAILABILITY_DRIVER,
                0,
                report=True,
                force=True,
                uom=UOM_BOOLEAN,
            )
            detail = json.dumps(
                {
                    "status": "branch_refresh_unsupported",
                    "rom": self.rom.hex().upper(),
                    "branch_path": branch_path_label(self._branch_path),
                    "last_valid_temperature_stale": self._has_valid_temperature,
                    "detail": (
                        self._branch_refresh_error
                        or "validated persisted branch target is unavailable"
                    ),
                },
                sort_keys=True,
            )
            self.poly.Notices[self._notice_key] = detail
            logger.warning("%s temperature refresh rejected: %s", self.name, detail)
            return False

        def last_refresh_error(self) -> str | None:
            """Return scheduler-safe error detail from the latest attempt."""

            return self._last_refresh_error

        def query(self, command: object | None = None) -> None:
            del command
            if self._has_valid_temperature:
                self.reportDriver(TEMPERATURE_DRIVER, True)
            self.reportDriver(AVAILABILITY_DRIVER, True)
            self.reportDriver(REFRESH_INTERVAL_DRIVER, True)

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

        def _set_refresh_interval_value(self, seconds: int) -> bool:
            if not self._set_refresh_interval(seconds):
                return False
            self._refresh_interval_seconds = seconds
            self.setDriver(
                REFRESH_INTERVAL_DRIVER,
                seconds,
                report=True,
                force=True,
                uom=UOM_INDEX,
            )
            return True

        def set_desired_refresh_interval(self, seconds: int) -> None:
            """Apply validated persisted configuration without reading USB."""

            self._refresh_interval_seconds = seconds
            self.setDriver(
                REFRESH_INTERVAL_DRIVER,
                seconds,
                report=False,
                force=True,
                uom=UOM_INDEX,
            )

        def set_temperature_presentation(
            self, presentation: TemperaturePresentation
        ) -> None:
            """Re-render the cached value only; never access the adapter."""

            if presentation == self._temperature_presentation:
                return
            self._temperature_presentation = presentation
            self._apply_temperature_presentation(report=True)

        def _apply_temperature_presentation(self, *, report: bool) -> None:
            if self._raw_temperature_celsius is None:
                return
            value = self._temperature_presentation.publish(
                self._raw_temperature_celsius
            )
            uom = self._temperature_presentation.unit.uom
            if not self._has_driver(TEMPERATURE_DRIVER):
                self.drivers.append(
                    {"driver": TEMPERATURE_DRIVER, "value": value, "uom": uom}
                )
                if report:
                    self.poly.addNode(self)
                    self.reportDriver(TEMPERATURE_DRIVER, True)
                return
            current = next(
                driver
                for driver in self.drivers
                if driver.get("driver") == TEMPERATURE_DRIVER
            )
            changed = current.get("value") != value or current.get("uom") != uom
            self.setDriver(
                TEMPERATURE_DRIVER,
                value,
                report=report and changed,
                force=False,
                uom=uom,
            )

        def _legacy_raw_celsius(self) -> float | None:
            temperature = next(
                (
                    driver
                    for driver in self.drivers
                    if driver.get("driver") == TEMPERATURE_DRIVER
                ),
                None,
            )
            if temperature is None:
                return None
            try:
                return celsius_from_published(
                    temperature.get("value"), temperature.get("uom")
                )
            except TemperaturePresentationError:
                return None

        def updateDrivers(self, drivers: object) -> None:
            self.drivers = _safe_driver_list(
                drivers, interval_seconds=self._refresh_interval_seconds
            )
            self._has_valid_temperature = self._temperature_driver_is_numeric()
            self.setDriver(
                AVAILABILITY_DRIVER,
                0,
                report=self._has_valid_temperature,
                force=True,
                uom=UOM_BOOLEAN,
            )

        def _restore_persisted_temperature(self, polyglot: Any) -> None:
            getter = getattr(polyglot, "getNodesFromDb", None)
            if getter is None:
                getter = polyglot.db_getNodeDrivers
            restored = _safe_driver_list(getter(self.address))
            temperature = next(
                (
                    driver
                    for driver in restored
                    if driver["driver"] == TEMPERATURE_DRIVER
                ),
                None,
            )
            if temperature is not None and not self._has_driver(TEMPERATURE_DRIVER):
                self.drivers.append(temperature)

        def _has_driver(self, driver_id: str) -> bool:
            return any(driver.get("driver") == driver_id for driver in self.drivers)

        def _temperature_driver_is_numeric(self) -> bool:
            return any(
                driver.get("driver") == TEMPERATURE_DRIVER
                and _coerce_numeric_temperature(
                    driver.get("value"), driver.get("uom")
                )
                is not None
                for driver in self.drivers
            )

        commands = {
            REFRESH_COMMAND: refresh_temperature,
            REFRESH_OFF_COMMAND: set_refresh_off,
            REFRESH_30_COMMAND: set_refresh_30,
            REFRESH_60_COMMAND: set_refresh_60,
            REFRESH_120_COMMAND: set_refresh_120,
            REFRESH_300_COMMAND: set_refresh_300,
        }

    Ds18b20Node.__name__ = "Ds18b20Node"
    Ds18b20Node.__qualname__ = "Ds18b20Node"
    return Ds18b20Node


def _validated_branch_path(value: object) -> tuple[BranchHop, ...]:
    if not isinstance(value, tuple) or not all(
        isinstance(hop, BranchHop) for hop in value
    ):
        raise ValueError("branch_path must be a tuple of BranchHop values")
    return value


def _coerce_numeric_temperature(value: object, uom: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    if uom == UOM_FAHRENHEIT and -67.0 <= number <= 257.0:
        return number
    if uom == UOM_CELSIUS and -55.0 <= number <= 125.0:
        return number
    else:
        return None


def _safe_driver_list(
    drivers: object, *, interval_seconds: int = 0
) -> list[dict[str, object]]:
    restored: list[dict[str, object]] = [
        {"driver": AVAILABILITY_DRIVER, "value": 0, "uom": UOM_BOOLEAN},
        {
            "driver": REFRESH_INTERVAL_DRIVER,
            "value": interval_seconds,
            "uom": UOM_INDEX,
        },
    ]
    if not isinstance(drivers, list):
        return restored
    for driver in drivers:
        if not isinstance(driver, dict) or driver.get("driver") != TEMPERATURE_DRIVER:
            continue
        uom = driver.get("uom")
        value = _coerce_numeric_temperature(driver.get("value"), uom)
        if value is not None:
            restored.append(
                {
                    "driver": TEMPERATURE_DRIVER,
                    "value": value,
                    "uom": uom,
                }
            )
            break
    return restored


def _event_evidence_payload(evidence: object) -> list[dict[str, object]]:
    if not isinstance(evidence, tuple):
        return []
    return [
        {
            "stage": item.stage,
            "raw_status": f"0x{item.raw_status:02X}",
            "main_event": item.main_event,
            "aux_event": item.aux_event,
        }
        for item in evidence
    ]
