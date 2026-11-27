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
from eisy_onewire.adapter_binding import AdapterIdentityBinding  # noqa: E402
from eisy_onewire.temperature_service import TemperatureService  # noqa: E402
from eisy_onewire.ds18b20_transport import Ds18b20Adapter  # noqa: E402
from eisy_onewire.branch_temperature import BranchTemperatureAdapter  # noqa: E402
from eisy_onewire.ds18b20_node import create_ds18b20_node_class  # noqa: E402
from eisy_onewire.ds2438_node import create_ds2438_node_class  # noqa: E402
from eisy_onewire.ds2438_analog_node import (  # noqa: E402
    create_ds2438_analog_node_class,
    create_ds2438_solar_node_class,
)
from eisy_onewire.node_registry import OneWireNodeRegistry  # noqa: E402
from eisy_onewire.ds2438_service import (  # noqa: E402
    RootDs2438Adapter,
    BranchDs2438Adapter,
    RootDs2438MeasurementAdapter,
    BranchDs2438MeasurementAdapter,
)
from eisy_onewire.manual_discovery import ManualDiscoveryService  # noqa: E402
from eisy_onewire.topology_discovery import Ds2409TopologyAdapter  # noqa: E402
from eisy_onewire.ds2409_status import (  # noqa: E402
    Ds2409StatusAdapter,
    ManualDs2409StatusService,
)
from eisy_onewire.sensor_config import load_default_parameters  # noqa: E402
from eisy_onewire.sensor_config import Ds2438Application  # noqa: E402
from eisy_onewire.usb_detector import DetectionStatus  # noqa: E402
from eisy_onewire.version import PLUGIN_VERSION  # noqa: E402


def main() -> None:
    """Start the platform interface with policy-gated adapter recovery."""
    import udi_interface

    adapter_binding = AdapterIdentityBinding()
    controller_node = create_controller_node_class(udi_interface.Node)
    sensor_node = create_ds18b20_node_class(
        udi_interface.Node,
        logger=udi_interface.LOGGER,
        service_factory=lambda: TemperatureService(
            adapter_factory=lambda: Ds18b20Adapter(adapter_binding)
        ),
        branch_service_factory=lambda: BranchTemperatureAdapter(adapter_binding),
    )
    ds2438_node = create_ds2438_node_class(
        udi_interface.Node,
        logger=udi_interface.LOGGER,
        root_service_factory=lambda: RootDs2438Adapter(adapter_binding),
        branch_service_factory=lambda: BranchDs2438Adapter(adapter_binding),
    )
    ds2438_analog_node = create_ds2438_analog_node_class(
        udi_interface.Node,
        logger=udi_interface.LOGGER,
        root_service_factory=lambda: RootDs2438MeasurementAdapter(adapter_binding),
        branch_service_factory=lambda: BranchDs2438MeasurementAdapter(adapter_binding),
    )
    ds2438_solar_node = create_ds2438_solar_node_class(
        udi_interface.Node,
        logger=udi_interface.LOGGER,
        root_service_factory=lambda: RootDs2438MeasurementAdapter(adapter_binding),
        branch_service_factory=lambda: BranchDs2438MeasurementAdapter(adapter_binding),
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
        adapter_binding=adapter_binding,
        discovery_factory=lambda lock: ManualDiscoveryService(
            lock, search=Ds2409TopologyAdapter(adapter_binding).discover
        ),
        status_factory=lambda lock: ManualDs2409StatusService(
            lock, read=Ds2409StatusAdapter(adapter_binding).read
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
