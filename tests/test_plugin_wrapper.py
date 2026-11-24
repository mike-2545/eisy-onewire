from __future__ import annotations

import unittest

from eisy_onewire.plugin_wrapper import (
    NOTICE_KEY,
    run_plugin,
    synchronize_json_profile,
)
from eisy_onewire.version import PLUGIN_VERSION
from eisy_onewire.usb_detector import (
    DetectionResult,
    DetectionStatus,
    UsbDeviceDescriptor,
)


class FakeInterface:
    def __init__(
        self,
        *,
        run_error: Exception | None = None,
        nodes: dict[str, object] | None = None,
        profile_response: object = ...,
    ) -> None:
        self.Notices: dict[str, str] = {}
        self.events: list[object] = []
        self.run_error = run_error
        self.nodes = nodes if nodes is not None else {}
        self.profile_response = profile_response
        self.profile_updates = 0

    def start(self, options: dict[str, object]) -> None:
        self.events.append(("start", options))

    def ready(self) -> None:
        self.events.append("ready")

    def runForever(self) -> None:
        self.events.append("runForever")
        if self.run_error is not None:
            raise self.run_error

    def stop(self) -> None:
        self.events.append("stop")

    def addNode(self, node: object) -> None:
        self.nodes[node.address] = node
        self.events.append(("addNode", node.address))

    def getNode(self, address: str) -> object | None:
        return self.nodes.get(address)

    def updateJsonProfile(
        self,
        profile: dict[str, object],
        options: dict[str, object] | None = None,
    ) -> object:
        self.profile_updates += 1
        request_id = f"profile-{self.profile_updates}"
        profile["requestId"] = request_id
        self.events.append(("updateJsonProfile", options, profile.copy()))
        if isinstance(self.profile_response, Exception):
            raise self.profile_response
        if self.profile_response is ...:
            return {"success": True, "requestId": request_id}
        return self.profile_response


class FakeLogger:
    def __init__(self) -> None:
        self.entries: list[tuple[str, tuple[object, ...]]] = []

    def info(self, message: str, *args: object) -> None:
        self.entries.append((message, args))


def opened_result() -> DetectionResult:
    return DetectionResult(
        status=DetectionStatus.OPENED,
        descriptor=UsbDeviceDescriptor(
            vendor_id=0x04FA,
            product_id=0x2490,
            usb_version=0x0110,
            device_version=0x0002,
            device_class=0,
            device_subclass=0,
            device_protocol=0,
            max_packet_size0=8,
            num_configurations=1,
            bus_number=0,
            device_address=4,
        ),
        detail="matching DS9490 opened and closed successfully",
    )


class PluginWrapperTests(unittest.TestCase):
    profile = {
        "editors": [{"id": "temperature_f"}, {"id": "reading_current"}],
        "nodedefs": [{"id": "ds18b20"}],
    }

    def test_displays_one_probe_without_registering_nodes(self) -> None:
        interface = FakeInterface()
        logger = FakeLogger()

        report = run_plugin(
            interface,
            logger,
            version=PLUGIN_VERSION,
            detector=opened_result,
        )

        self.assertEqual(report["status"], "opened")
        self.assertEqual(report["device"]["vendor_id"], "04fa")
        self.assertEqual(report["device"]["product_id"], "2490")
        self.assertIn('"status": "opened"', interface.Notices[NOTICE_KEY])
        self.assertEqual(
            interface.events,
            [("start", {"version": PLUGIN_VERSION}), "ready", "runForever", "stop"],
        )
        self.assertEqual(len(logger.entries), 1)

    def test_synchronizes_profile_before_runtime_activation(self) -> None:
        interface = FakeInterface()

        def activate(result: DetectionResult) -> None:
            self.assertEqual(result.status, DetectionStatus.OPENED)
            interface.events.append("activate")

        run_plugin(
            interface,
            FakeLogger(),
            version=PLUGIN_VERSION,
            detector=opened_result,
            profile_ready=activate,
            json_profile=self.profile,
        )
        profile_event = next(
            event
            for event in interface.events
            if isinstance(event, tuple) and event[0] == "updateJsonProfile"
        )
        self.assertEqual(profile_event[1], {"waitResponse": True})
        self.assertLess(
            interface.events.index(profile_event),
            interface.events.index("activate"),
        )
        self.assertNotIn("requestId", self.profile)

        restarted = FakeInterface()
        run_plugin(
            restarted,
            FakeLogger(),
            version=PLUGIN_VERSION,
            detector=opened_result,
            profile_ready=lambda result: restarted.events.append("activate"),
            json_profile=self.profile,
        )
        self.assertEqual(restarted.events.count("activate"), 1)
        self.assertEqual(restarted.profile_updates, 1)

    def test_profile_timeout_fails_closed_before_node_registration(self) -> None:
        interface = FakeInterface(profile_response=TimeoutError("profile timeout"))

        with self.assertRaisesRegex(TimeoutError, "profile timeout"):
            run_plugin(
                interface,
                FakeLogger(),
                version=PLUGIN_VERSION,
                detector=opened_result,
                profile_ready=lambda result: interface.events.append("activate"),
                json_profile=self.profile,
            )

        self.assertNotIn("activate", interface.events)
        self.assertEqual(interface.events[-1], "stop")

    def test_profile_ready_requires_json_profile(self) -> None:
        interface = FakeInterface()

        with self.assertRaisesRegex(ValueError, "json_profile is required"):
            run_plugin(
                interface,
                FakeLogger(),
                version=PLUGIN_VERSION,
                detector=opened_result,
                profile_ready=lambda result: None,
            )
        self.assertEqual(interface.events[-1], "stop")

    def test_setup_occurs_after_start_and_before_detector(self) -> None:
        interface = FakeInterface()

        def setup() -> None:
            interface.events.append("setup")

        run_plugin(
            interface,
            FakeLogger(),
            version=PLUGIN_VERSION,
            setup=setup,
            detector=opened_result,
        )
        self.assertEqual(interface.events[:3], [("start", {"version": PLUGIN_VERSION}), "setup", "ready"])

    def test_unsuccessful_profile_ack_fails_closed(self) -> None:
        interface = FakeInterface(
            profile_response={
                "success": False,
                "requestId": "profile-1",
                "message": "IoX rejected profile",
            }
        )

        with self.assertRaisesRegex(RuntimeError, "IoX rejected profile"):
            synchronize_json_profile(interface, self.profile)

    def test_missing_or_mismatched_profile_ack_fails_closed(self) -> None:
        for response, message in (
            (None, "no acknowledgment"),
            ({"success": True, "requestId": "wrong"}, "did not match"),
            ({"requestId": "profile-1"}, "profile update failed"),
        ):
            with self.subTest(response=response):
                interface = FakeInterface(profile_response=response)
                with self.assertRaisesRegex(RuntimeError, message):
                    synchronize_json_profile(interface, self.profile)

    def test_unsupported_profile_api_fails_closed(self) -> None:
        interface = FakeInterface()
        interface.updateJsonProfile = None  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "does not support"):
            synchronize_json_profile(interface, self.profile)

    def test_stops_platform_interface_when_runtime_exits_with_error(self) -> None:
        interface = FakeInterface(run_error=RuntimeError("runtime stopped"))

        with self.assertRaisesRegex(RuntimeError, "runtime stopped"):
            run_plugin(
                interface,
                FakeLogger(),
                version=PLUGIN_VERSION,
                detector=opened_result,
            )

        self.assertEqual(interface.events[-1], "stop")

    def test_scheduler_shutdown_precedes_platform_stop_on_success_and_error(self) -> None:
        for run_error in (None, RuntimeError("runtime stopped")):
            with self.subTest(run_error=run_error):
                interface = FakeInterface(run_error=run_error)

                def run() -> None:
                    run_plugin(
                        interface,
                        FakeLogger(),
                        version=PLUGIN_VERSION,
                        detector=opened_result,
                        shutdown=lambda: interface.events.append("shutdown"),
                    )

                if run_error is None:
                    run()
                else:
                    with self.assertRaisesRegex(RuntimeError, "runtime stopped"):
                        run()
                self.assertEqual(interface.events[-2:], ["shutdown", "stop"])

    def test_scheduler_quiescence_failure_prevents_platform_stop(self) -> None:
        interface = FakeInterface()

        with self.assertRaisesRegex(RuntimeError, "worker still active"):
            run_plugin(
                interface,
                FakeLogger(),
                version=PLUGIN_VERSION,
                detector=opened_result,
                shutdown=lambda: (_ for _ in ()).throw(
                    RuntimeError("worker still active")
                ),
            )

        self.assertNotIn("stop", interface.events)

    def test_stops_platform_interface_when_detector_raises(self) -> None:
        interface = FakeInterface()

        def broken_detector() -> DetectionResult:
            raise RuntimeError("detector failed")

        with self.assertRaisesRegex(RuntimeError, "detector failed"):
            run_plugin(
                interface,
                FakeLogger(),
                version=PLUGIN_VERSION,
                detector=broken_detector,
            )

        self.assertEqual(
            interface.events,
            [("start", {"version": PLUGIN_VERSION}), "stop"],
        )


if __name__ == "__main__":
    unittest.main()
