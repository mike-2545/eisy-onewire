#!/usr/bin/env python3
"""eisy-ui entry point for configured 1-Wire temperature child nodes."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from eisy_onewire.plugin_wrapper import run_plugin, synchronize_json_profile  # noqa: E402
from eisy_onewire.iox_profile import build_runtime_json_profile  # noqa: E402
from eisy_onewire.inventory_evidence import installed_evidence_path  # noqa: E402
from eisy_onewire.controller_node import create_controller_node_class  # noqa: E402
from eisy_onewire.ds18b20_node import create_ds18b20_node_class  # noqa: E402
from eisy_onewire.ds2438_node import create_ds2438_node_class  # noqa: E402
from eisy_onewire.ds2438_analog_node import (  # noqa: E402
    create_ds2438_analog_node_class,
    create_ds2438_solar_node_class,
)
from eisy_onewire.node_registry import OneWireNodeRegistry  # noqa: E402
from eisy_onewire.sensor_config import load_default_parameters  # noqa: E402
from eisy_onewire.sensor_config import Ds2438Application  # noqa: E402
from eisy_onewire.usb_detector import DetectionStatus  # noqa: E402
from eisy_onewire.version import PLUGIN_VERSION  # noqa: E402


def main() -> None:
    """Start the platform interface; no temperature operation runs automatically."""
    import udi_interface

    controller_node = create_controller_node_class(udi_interface.Node)
    sensor_node = create_ds18b20_node_class(
        udi_interface.Node,
        logger=udi_interface.LOGGER,
    )
    ds2438_node = create_ds2438_node_class(
        udi_interface.Node,
        logger=udi_interface.LOGGER,
    )
    ds2438_analog_node = create_ds2438_analog_node_class(
        udi_interface.Node, logger=udi_interface.LOGGER
    )
    ds2438_solar_node = create_ds2438_solar_node_class(
        udi_interface.Node, logger=udi_interface.LOGGER
    )
    interface = udi_interface.Interface(
        [controller_node, sensor_node, ds2438_node, ds2438_analog_node, ds2438_solar_node]
    )
    custom_parameters = udi_interface.Custom(interface, "customparams")
    custom_data = udi_interface.Custom(interface, "customdata")
    registry = OneWireNodeRegistry(
        interface,
        udi_interface.LOGGER,
        custom_parameters,
        custom_data,
        load_default_parameters(ROOT / "default-sensors.json"),
        controller_node,
        sensor_node,
        ds2438_sensor_class=ds2438_node,
        ds2438_sensor_classes={
            Ds2438Application.HIH4000: ds2438_node,
            Ds2438Application.ANALOG_VOLTAGE: ds2438_analog_node,
            Ds2438Application.SOLAR_IRRADIANCE: ds2438_solar_node,
        },
        inventory_evidence_path=installed_evidence_path(),
        presentation_profile_updater=lambda presentation: synchronize_json_profile(
            interface, build_runtime_json_profile(presentation)
        ),
    )
    json_profile = build_runtime_json_profile()

    def setup_configuration() -> None:
        interface.setCustomParamsDoc()
        interface.subscribe(
            interface.CUSTOMPARAMS,
            registry.handle_custom_parameters,
        )
        interface.subscribe(
            interface.CUSTOMDATA,
            registry.handle_custom_data,
        )

    run_plugin(
        interface,
        udi_interface.LOGGER,
        version=PLUGIN_VERSION,
        setup=setup_configuration,
        profile_ready=lambda result: registry.activate(
            adapter_accessible=result.status is DetectionStatus.OPENED
        ),
        json_profile=json_profile,
        shutdown=registry.shutdown,
    )


if __name__ == "__main__":
    main()
