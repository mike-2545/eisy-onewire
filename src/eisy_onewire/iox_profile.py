"""Build the IoX runtime JSON profile accepted by dynamic updateprofile."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .temperature_presentation import TemperaturePresentation, TemperatureUnit

# This is the dynamic IoX updateprofile shape, not the Local-store metadata
# shape in eisy-onewire.iox_plugin.json. The formats are intentionally kept
# separate because their editor and node-definition schemas differ.
_RUNTIME_PROFILE: dict[str, Any] = {
    "editors": [
        {
            "id": "temperature",
            "ranges": [
                {
                    "uom": "17",
                    "prec": 1,
                    "step": 0.1,
                    "min": -67,
                    "max": 257,
                }
            ],
        },
        {
            "id": "reading_current",
            "ranges": [
                {
                    "uom": "2",
                    "subset": "0,1",
                    "names": {"0": "False", "1": "True"},
                }
            ],
        },
        {
            "id": "refresh_interval",
            "ranges": [
                {
                    "uom": "25",
                    "subset": "0,30,60,120,300",
                    "names": {
                        "0": "Off",
                        "30": "30 seconds",
                        "60": "60 seconds",
                        "120": "120 seconds",
                        "300": "300 seconds",
                    },
                }
            ],
        },
        {
            "id": "temperature_unit",
            "ranges": [
                {
                    "uom": "25",
                    "subset": "0,1",
                    "names": {"0": "Fahrenheit", "1": "Celsius"},
                }
            ],
        },
        {
            "id": "decimal_precision",
            "ranges": [
                {
                    "uom": "25",
                    "subset": "0,1,2,3",
                    "names": {
                        "0": "0 decimal places",
                        "1": "1 decimal place",
                        "2": "2 decimal places",
                        "3": "3 decimal places",
                    },
                }
            ],
        },
        {
            "id": "relative_humidity",
            "ranges": [
                {
                    "uom": "51",
                    "prec": 1,
                    "step": 0.1,
                    "min": 0,
                    "max": 100,
                }
            ],
        },
        {
            "id": "analog_voltage",
            "ranges": [{"uom": "72", "prec": 2, "step": 0.01, "min": 0, "max": 10}],
        },
        {
            "id": "solar_irradiance",
            "ranges": [{"uom": "74", "prec": 1, "step": 0.1, "min": -5000, "max": 5000}],
        },
    ],
    "linkdefs": [],
    "nodedefs": [
        {
            "id": "onewire_controller",
            "nls": "ONEWIRE_CONTROLLER",
            "icon": "GenericCtl",
            "name": "1-Wire Controller",
            "properties": [
                {
                    "id": "ST",
                    "editor": "reading_current",
                    "name": "Adapter Accessible",
                },
                {
                    "id": "GV0",
                    "editor": "reading_current",
                    "name": "Automatic Refresh Enabled",
                },
                {
                    "id": "GV1",
                    "editor": "temperature_unit",
                    "name": "Temperature Unit",
                },
                {
                    "id": "GV2",
                    "editor": "decimal_precision",
                    "name": "Temperature Decimal Places",
                },
                {
                    "id": "GV3",
                    "editor": "reading_current",
                    "name": "Automatic Adapter Recovery Enabled",
                },
            ],
            "cmds": {
                "sends": [],
                "accepts": [
                    {
                        "id": "DISCOVER",
                        "native": "false",
                        "name": "Discover Devices",
                    },
                    {
                        "id": "DS2409_STATUS",
                        "native": "false",
                        "name": "Read DS2409 Status",
                    },
                    {
                        "id": "VERIFY_ADAPTER",
                        "native": "false",
                        "name": "Verify Adapter Identity",
                    },
                    {
                        "id": "AUTO_REFRESH_ENABLE",
                        "native": "false",
                        "name": "Automatic Refresh Enable",
                    },
                    {
                        "id": "AUTO_REFRESH_DISABLE",
                        "native": "false",
                        "name": "Automatic Refresh Disable",
                    },
                    {
                        "id": "ADAPTER_RECOVERY_ENABLE",
                        "native": "false",
                        "name": "Automatic Adapter Recovery Enable",
                    },
                    {
                        "id": "ADAPTER_RECOVERY_DISABLE",
                        "native": "false",
                        "name": "Automatic Adapter Recovery Disable",
                    },
                    {"id": "USE_FAHRENHEIT", "native": "false", "name": "Use Fahrenheit"},
                    {"id": "USE_CELSIUS", "native": "false", "name": "Use Celsius"},
                    {"id": "PRECISION_0", "native": "false", "name": "0 Decimal Places"},
                    {"id": "PRECISION_1", "native": "false", "name": "1 Decimal Place"},
                    {"id": "PRECISION_2", "native": "false", "name": "2 Decimal Places"},
                    {"id": "PRECISION_3", "native": "false", "name": "3 Decimal Places"},
                ],
            },
            "links": {"ctl": [], "rsp": []},
        },
        {
            "id": "ds18b20",
            "nls": "DS18B20",
            "icon": "Temperature",
            "name": "1-Wire Temperature Sensor",
            "properties": [
                {
                    "id": "ST",
                    "editor": "temperature",
                    "name": "Temperature",
                },
                {
                    "id": "GV0",
                    "editor": "reading_current",
                    "name": "Current Reading Valid",
                },
                {
                    "id": "GV1",
                    "editor": "refresh_interval",
                    "name": "Desired Refresh Interval",
                },
            ],
            "cmds": {
                "sends": [],
                "accepts": [
                    {
                        "id": "REFRESH",
                        "native": "false",
                        "name": "Refresh Temperature",
                    },
                    {"id": "REFRESH_OFF", "native": "false", "name": "Refresh Off"},
                    {"id": "REFRESH_30", "native": "false", "name": "Refresh 30 sec"},
                    {"id": "REFRESH_60", "native": "false", "name": "Refresh 60 sec"},
                    {"id": "REFRESH_120", "native": "false", "name": "Refresh 120 sec"},
                    {"id": "REFRESH_300", "native": "false", "name": "Refresh 300 sec"},
                ],
            },
            "links": {"ctl": [], "rsp": []},
        },
        {
            "id": "ds2438_hih4000",
            "nls": "DS2438_HIH4000",
            "icon": "Temperature",
            "name": "1-Wire Temperature and Humidity Sensor",
            "properties": [
                {"id": "ST", "editor": "temperature", "name": "Temperature"},
                {
                    "id": "GV0",
                    "editor": "reading_current",
                    "name": "Current Reading Valid",
                },
                {
                    "id": "GV1",
                    "editor": "refresh_interval",
                    "name": "Desired Refresh Interval",
                },
                {
                    "id": "GV2",
                    "editor": "relative_humidity",
                    "name": "Relative Humidity",
                },
            ],
            "cmds": {
                "sends": [],
                "accepts": [
                    {
                        "id": "REFRESH",
                        "native": "false",
                        "name": "Refresh Temperature and Humidity",
                    },
                    {"id": "REFRESH_OFF", "native": "false", "name": "Refresh Off"},
                    {"id": "REFRESH_30", "native": "false", "name": "Refresh 30 sec"},
                    {"id": "REFRESH_60", "native": "false", "name": "Refresh 60 sec"},
                    {"id": "REFRESH_120", "native": "false", "name": "Refresh 120 sec"},
                    {"id": "REFRESH_300", "native": "false", "name": "Refresh 300 sec"},
                ],
            },
            "links": {"ctl": [], "rsp": []},
        },
        {
            "id": "ds2438_analog_voltage",
            "nls": "DS2438_ANALOG_VOLTAGE",
            "icon": "Electricity",
            "name": "1-Wire Analog Voltage Sensor",
            "properties": [
                {"id": "ST", "editor": "analog_voltage", "name": "Analog Input Voltage"},
                {"id": "GV0", "editor": "reading_current", "name": "Current Reading Valid"},
                {"id": "GV1", "editor": "refresh_interval", "name": "Desired Refresh Interval"},
                {"id": "GV2", "editor": "analog_voltage", "name": "Supply Voltage"},
            ],
            "cmds": {
                "sends": [],
                "accepts": [
                    {"id": "REFRESH", "native": "false", "name": "Refresh Analog Voltages"},
                    {"id": "REFRESH_OFF", "native": "false", "name": "Refresh Off"},
                    {"id": "REFRESH_30", "native": "false", "name": "Refresh 30 sec"},
                    {"id": "REFRESH_60", "native": "false", "name": "Refresh 60 sec"},
                    {"id": "REFRESH_120", "native": "false", "name": "Refresh 120 sec"},
                    {"id": "REFRESH_300", "native": "false", "name": "Refresh 300 sec"},
                ],
            },
            "links": {"ctl": [], "rsp": []},
        },
        {
            "id": "ds2438_solar_irradiance",
            "nls": "DS2438_SOLAR_IRRADIANCE",
            "icon": "Sun",
            "name": "1-Wire Solar Irradiance Sensor",
            "properties": [
                {"id": "ST", "editor": "solar_irradiance", "name": "Solar Irradiance"},
                {"id": "GV0", "editor": "reading_current", "name": "Current Reading Valid"},
                {"id": "GV1", "editor": "refresh_interval", "name": "Desired Refresh Interval"},
                {"id": "GV2", "editor": "analog_voltage", "name": "Raw Analog Input Voltage"},
            ],
            "cmds": {
                "sends": [],
                "accepts": [
                    {"id": "REFRESH", "native": "false", "name": "Refresh Solar Irradiance"},
                    {"id": "REFRESH_OFF", "native": "false", "name": "Refresh Off"},
                    {"id": "REFRESH_30", "native": "false", "name": "Refresh 30 sec"},
                    {"id": "REFRESH_60", "native": "false", "name": "Refresh 60 sec"},
                    {"id": "REFRESH_120", "native": "false", "name": "Refresh 120 sec"},
                    {"id": "REFRESH_300", "native": "false", "name": "Refresh 300 sec"},
                ],
            },
            "links": {"ctl": [], "rsp": []},
        },
    ],
}


def build_runtime_json_profile(
    presentation: TemperaturePresentation | None = None,
) -> dict[str, Any]:
    """Return an independent additive updateprofile payload for this startup."""

    selected = presentation or TemperaturePresentation()
    profile = deepcopy(_RUNTIME_PROFILE)
    editor = next(item for item in profile["editors"] if item["id"] == "temperature")
    temperature_range = editor["ranges"][0]
    temperature_range["uom"] = str(selected.unit.uom)
    temperature_range["prec"] = selected.decimal_places
    temperature_range["step"] = 10 ** (-selected.decimal_places)
    if selected.unit is TemperatureUnit.CELSIUS:
        temperature_range["min"] = -55
        temperature_range["max"] = 125
    return profile
