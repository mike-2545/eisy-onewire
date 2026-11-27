"""Minimal eisy-ui wrapper for a single read-only DS9490 access probe."""

from __future__ import annotations

import json
from copy import deepcopy
from collections.abc import Callable
from typing import Any, Protocol, cast

from .usb_detector import DetectionResult, detect_ds9490

NOTICE_KEY = "ds9490_usb_access"


class PluginInterface(Protocol):
    """Small injectable subset of the platform interface used by this plugin."""

    Notices: dict[str, str]

    def start(self, options: dict[str, object]) -> None: ...

    def ready(self) -> None: ...

    def runForever(self) -> None: ...

    def stop(self) -> None: ...

    def updateJsonProfile(
        self,
        profile: dict[str, object],
        options: dict[str, object] | None = None,
    ) -> object: ...


class Logger(Protocol):
    def info(self, message: str, *args: object) -> None: ...


def detection_report(result: DetectionResult) -> dict[str, Any]:
    """Convert a detector result into the stable UI/log representation."""
    descriptor = result.descriptor
    report: dict[str, Any] = {
        "status": result.status.value,
        "detail": result.detail,
        "error_code": result.error_code,
        "matching_adapter_count": result.matching_adapter_count,
        "safety": "probe performs at most an immediate open/close; no interface claimed or transfer sent",
    }
    if descriptor is not None:
        report["device"] = {
            "vendor_id": f"{descriptor.vendor_id:04x}",
            "product_id": f"{descriptor.product_id:04x}",
            "bus_number": descriptor.bus_number,
            "device_address": descriptor.device_address,
            "port_path": list(descriptor.port_path) if descriptor.port_path else None,
        }
    if result.descriptors:
        report["matching_adapters"] = [
            {
                "bus_number": item.bus_number,
                "device_address": item.device_address,
                "port_path": list(item.port_path) if item.port_path else None,
            }
            for item in result.descriptors
        ]
    return report


def synchronize_json_profile(
    interface: PluginInterface,
    profile: dict[str, object],
) -> dict[str, object]:
    """Install an additive JSON profile and require its correlated success ack."""

    updater = getattr(interface, "updateJsonProfile", None)
    if not callable(updater):
        raise RuntimeError("udi_interface does not support updateJsonProfile")

    request = deepcopy(profile)
    response = updater(request, {"waitResponse": True})
    request_id = request.get("requestId")
    if not isinstance(request_id, str) or not request_id:
        raise RuntimeError("profile update did not assign a requestId")
    if not isinstance(response, dict):
        raise RuntimeError("profile update returned no acknowledgment")
    response = cast(dict[str, object], response)
    if response.get("requestId") != request_id:
        raise RuntimeError("profile update acknowledgment requestId did not match")
    if response.get("success") is not True:
        detail = response.get("message", "unsuccessful acknowledgment")
        raise RuntimeError(f"profile update failed: {detail}")
    return response


def run_plugin(
    interface: PluginInterface,
    logger: Logger,
    *,
    version: str,
    detector: Callable[[], DetectionResult] = detect_ds9490,
    setup: Callable[[], None] | None = None,
    profile_ready: Callable[[DetectionResult], None] | None = None,
    json_profile: dict[str, object] | None = None,
    shutdown: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Probe access, synchronize the profile, activate nodes, and stay alive."""
    interface.start({"version": version})
    try:
        if setup is not None:
            setup()
        result = detector()
        report = detection_report(result)
        rendered = json.dumps(report, sort_keys=True)
        logger.info("DS9490 USB access result: %s", rendered)
        interface.Notices[NOTICE_KEY] = rendered
        interface.ready()
        if json_profile is not None:
            synchronize_json_profile(interface, json_profile)
            if profile_ready is not None:
                profile_ready(result)
        elif profile_ready is not None:
            raise ValueError("json_profile is required with profile_ready")
        interface.runForever()
        return report
    finally:
        # A worker shutdown failure deliberately prevents interface.stop().
        # Stopping the platform while a worker can still publish profiles,
        # drivers, or Notices is less safe than surfacing a fatal quiescence
        # error.
        if shutdown is not None:
            shutdown()
        interface.stop()
