"""Generic primary node for one configured 1-Wire adapter namespace."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable
from typing import Any, TypeVar

from .node_identity import DEFAULT_CONTROLLER_ADDRESS
from .temperature_presentation import TemperaturePresentation, TemperatureUnit


NodeType = TypeVar("NodeType")
NODE_DEFINITION_ID = "onewire_controller"
CONTROLLER_NAME = "1-Wire Controller"
STATUS_DRIVER = "ST"
AUTOMATIC_REFRESH_DRIVER = "GV0"
TEMPERATURE_UNIT_DRIVER = "GV1"
TEMPERATURE_PRECISION_DRIVER = "GV2"
UOM_BOOLEAN = 2
UOM_INDEX = 25
DISCOVER_COMMAND = "DISCOVER"
DS2409_STATUS_COMMAND = "DS2409_STATUS"
AUTOMATIC_REFRESH_ENABLE_COMMAND = "AUTO_REFRESH_ENABLE"
AUTOMATIC_REFRESH_DISABLE_COMMAND = "AUTO_REFRESH_DISABLE"
USE_FAHRENHEIT_COMMAND = "USE_FAHRENHEIT"
USE_CELSIUS_COMMAND = "USE_CELSIUS"
PRECISION_COMMANDS = {
    "PRECISION_0": 0,
    "PRECISION_1": 1,
    "PRECISION_2": 2,
    "PRECISION_3": 3,
}


def create_controller_node_class(node_base: type[NodeType]) -> type[NodeType]:
    """Create the generic controller class without importing udi_interface in tests."""

    class OneWireController(node_base):  # type: ignore[misc, valid-type]
        id = NODE_DEFINITION_ID
        drivers = [
            {"driver": STATUS_DRIVER, "value": 0, "uom": UOM_BOOLEAN},
            {"driver": AUTOMATIC_REFRESH_DRIVER, "value": 0, "uom": UOM_BOOLEAN},
            {"driver": TEMPERATURE_UNIT_DRIVER, "value": 0, "uom": UOM_INDEX},
            {"driver": TEMPERATURE_PRECISION_DRIVER, "value": 1, "uom": UOM_INDEX},
        ]

        def __init__(
            self,
            polyglot: Any,
            *,
            adapter_accessible: bool,
            discover: Callable[[], bool],
            read_ds2409_status: Callable[[], bool],
            automatic_refresh_enabled: bool = False,
            set_automatic_refresh: Callable[[bool], bool] = lambda enabled: False,
            temperature_presentation: TemperaturePresentation = TemperaturePresentation(),
            set_temperature_unit: Callable[[TemperatureUnit], bool] = lambda unit: False,
            set_temperature_precision: Callable[[int], bool] = lambda places: False,
        ) -> None:
            super().__init__(
                polyglot,
                DEFAULT_CONTROLLER_ADDRESS,
                DEFAULT_CONTROLLER_ADDRESS,
                CONTROLLER_NAME,
            )
            self.drivers = deepcopy(type(self).drivers)
            self._discover = discover
            self._read_ds2409_status = read_ds2409_status
            self._set_automatic_refresh = set_automatic_refresh
            self._set_temperature_unit = set_temperature_unit
            self._set_temperature_precision = set_temperature_precision
            self.setDriver(
                STATUS_DRIVER,
                int(adapter_accessible),
                report=False,
                force=True,
                uom=UOM_BOOLEAN,
            )
            self.set_temperature_presentation(temperature_presentation, report=False)
            self.setDriver(
                AUTOMATIC_REFRESH_DRIVER,
                int(automatic_refresh_enabled),
                report=False,
                force=True,
                uom=UOM_BOOLEAN,
            )

        def query(self, command: object | None = None) -> None:
            del command
            self.reportDriver(STATUS_DRIVER, True)
            self.reportDriver(AUTOMATIC_REFRESH_DRIVER, True)
            self.reportDriver(TEMPERATURE_UNIT_DRIVER, True)
            self.reportDriver(TEMPERATURE_PRECISION_DRIVER, True)

        def discover_devices(self, command: object | None = None) -> bool:
            """Run only in response to the explicit IoX controller command."""

            del command
            return self._discover()

        def ds2409_status(self, command: object | None = None) -> bool:
            """Read one known root coupler only after an explicit IoX command."""

            del command
            return self._read_ds2409_status()

        def automatic_refresh_enable(self, command: object | None = None) -> bool:
            del command
            return self._set_automatic_refresh_value(True)

        def automatic_refresh_disable(self, command: object | None = None) -> bool:
            del command
            return self._set_automatic_refresh_value(False)

        def use_fahrenheit(self, command: object | None = None) -> bool:
            del command
            return self._set_temperature_unit(TemperatureUnit.FAHRENHEIT)

        def use_celsius(self, command: object | None = None) -> bool:
            del command
            return self._set_temperature_unit(TemperatureUnit.CELSIUS)

        def precision_0(self, command: object | None = None) -> bool:
            del command
            return self._set_temperature_precision(0)

        def precision_1(self, command: object | None = None) -> bool:
            del command
            return self._set_temperature_precision(1)

        def precision_2(self, command: object | None = None) -> bool:
            del command
            return self._set_temperature_precision(2)

        def precision_3(self, command: object | None = None) -> bool:
            del command
            return self._set_temperature_precision(3)

        def _set_automatic_refresh_value(self, enabled: bool) -> bool:
            if not self._set_automatic_refresh(enabled):
                return False
            self.setDriver(
                AUTOMATIC_REFRESH_DRIVER,
                int(enabled),
                report=True,
                force=True,
                uom=UOM_BOOLEAN,
            )
            return True

        def set_automatic_refresh_enabled(self, enabled: bool) -> None:
            """Apply validated persisted state without starting a scheduler."""

            self.setDriver(
                AUTOMATIC_REFRESH_DRIVER,
                int(enabled),
                report=False,
                force=True,
                uom=UOM_BOOLEAN,
            )

        def set_temperature_presentation(
            self, presentation: TemperaturePresentation, *, report: bool = True
        ) -> None:
            """Display validated global preferences without touching hardware."""

            self.setDriver(
                TEMPERATURE_UNIT_DRIVER,
                presentation.unit.controller_value,
                report=report,
                force=False,
                uom=UOM_INDEX,
            )
            self.setDriver(
                TEMPERATURE_PRECISION_DRIVER,
                presentation.decimal_places,
                report=report,
                force=False,
                uom=UOM_INDEX,
            )

        commands = {
            DISCOVER_COMMAND: discover_devices,
            DS2409_STATUS_COMMAND: ds2409_status,
            AUTOMATIC_REFRESH_ENABLE_COMMAND: automatic_refresh_enable,
            AUTOMATIC_REFRESH_DISABLE_COMMAND: automatic_refresh_disable,
            USE_FAHRENHEIT_COMMAND: use_fahrenheit,
            USE_CELSIUS_COMMAND: use_celsius,
            "PRECISION_0": precision_0,
            "PRECISION_1": precision_1,
            "PRECISION_2": precision_2,
            "PRECISION_3": precision_3,
        }

    OneWireController.__name__ = "OneWireController"
    OneWireController.__qualname__ = "OneWireController"
    return OneWireController
