from __future__ import annotations

import io
import json
import unittest

from eisy_onewire.ds2490_transport import (
    SearchLimitExceeded,
    UsbTransferTimeout,
    UsbTransportError,
)
from eisy_onewire.onewire_discovery import DiscoveryReport, OneWireDevice
from eisy_onewire.onewire_discovery import dallas_crc8
from eisy_onewire.search_diagnostic import (
    Ds2490SearchDiagnostic,
    diagnostic_report,
    main,
)


def rom(prefix: str) -> bytes:
    raw = bytes.fromhex(prefix)
    return raw + bytes([dallas_crc8(raw)])


class FakeAdapter:
    def __init__(self, responses: tuple[bytes, ...]) -> None:
        self.responses = responses
        self.calls: list[int] = []

    def search_roms(self, max_devices: int) -> tuple[bytes, ...]:
        self.calls.append(max_devices)
        return self.responses


class SearchDiagnosticTests(unittest.TestCase):
    def test_internal_device_alone_is_a_successful_complete_search(self) -> None:
        internal_id = bytes.fromhex("8194063200000014")
        self.assertEqual(dallas_crc8(internal_id), 0)
        adapter = FakeAdapter((internal_id,))
        diagnostic = Ds2490SearchDiagnostic(adapter)  # type: ignore[arg-type]

        report = diagnostic_report(diagnostic.run())

        self.assertEqual(adapter.calls, [15])
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["device_count"], 1)
        self.assertEqual(report["devices"][0]["rom"], "8194063200000014")
        self.assertEqual(report["devices"][0]["owfs_identifier"], "81.940632000000")
        self.assertEqual(report["devices"][0]["family_code"], "0x81")
        self.assertTrue(report["devices"][0]["crc_valid"])
        self.assertEqual(report["operation"]["search_commands"], 1)
        self.assertEqual(report["operation"]["automatic_retries"], 0)
        self.assertEqual(report["operation"]["bus_timing"]["speed"], "flexible")
        self.assertTrue(
            report["operation"]["bus_timing"]["verified_before_search"]
        )
        self.assertEqual(report["operation"]["temperature_commands"], 0)
        self.assertEqual(report["operation"]["iox_writes"], 0)

    def test_internal_device_plus_multiple_sensors_reports_every_rom(self) -> None:
        internal_id = bytes.fromhex("8194063200000014")
        first_sensor = bytes.fromhex("2811223344556656")
        second_sensor = rom("28010203040506")
        adapter = FakeAdapter((internal_id, first_sensor, second_sensor))

        rendered = diagnostic_report(
            Ds2490SearchDiagnostic(adapter).run()  # type: ignore[arg-type]
        )

        self.assertEqual(rendered["status"], "ok")
        self.assertEqual(rendered["device_count"], 3)
        self.assertEqual(
            [device["rom"] for device in rendered["devices"]],
            [
                internal_id.hex().upper(),
                first_sensor.hex().upper(),
                second_sensor.hex().upper(),
            ],
        )
        self.assertEqual(
            [device["family_code"] for device in rendered["devices"]],
            ["0x81", "0x28", "0x28"],
        )
        self.assertTrue(all(device["crc_valid"] for device in rendered["devices"]))

    def test_empty_bus_is_distinct_success(self) -> None:
        adapter = FakeAdapter(())
        diagnostic = Ds2490SearchDiagnostic(adapter)  # type: ignore[arg-type]
        output = io.StringIO()

        code = main(
            ["search-rom"],
            diagnostic_factory=lambda: diagnostic,
            output=output,
        )

        rendered = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(adapter.calls, [15])
        self.assertEqual(rendered["status"], "empty_bus")
        self.assertEqual(rendered["device_count"], 0)
        self.assertEqual(rendered["devices"], [])

    def test_invalid_rom_is_reported_and_never_accepted(self) -> None:
        valid = rom("28010203040506")
        invalid = valid[:-1] + bytes([valid[-1] ^ 1])
        adapter = FakeAdapter((invalid, b"short"))

        rendered = diagnostic_report(
            Ds2490SearchDiagnostic(adapter).run()  # type: ignore[arg-type]
        )

        self.assertEqual(rendered["status"], "invalid_rom_responses")
        self.assertEqual(rendered["device_count"], 0)
        self.assertEqual(rendered["devices"], [])
        self.assertEqual(rendered["invalid_responses"][0]["raw"], invalid.hex().upper())
        self.assertIn("CRC-8", rendered["invalid_responses"][0]["reason"])
        self.assertEqual(rendered["invalid_responses"][1]["raw"], b"short".hex().upper())
        self.assertIn("not 8 bytes", rendered["invalid_responses"][1]["reason"])

    def test_any_valid_family_is_successful_without_a_friendly_name(self) -> None:
        prefix = bytes.fromhex("10010203040506")
        other = OneWireDevice(prefix + bytes([dallas_crc8(prefix)]))

        rendered = diagnostic_report(DiscoveryReport((other,), ()))

        self.assertEqual(rendered["status"], "ok")
        self.assertEqual(rendered["devices"][0]["family_code"], "0x10")
        self.assertNotIn("matched_name", rendered["devices"][0])
        self.assertNotIn("expected_device", rendered)

    def test_device_limit_error_is_explicit_and_not_retried(self) -> None:
        creations = 0

        class LimitedDiagnostic:
            def run(self):
                raise SearchLimitExceeded("more than 15 devices are present")

        def factory():
            nonlocal creations
            creations += 1
            return LimitedDiagnostic()

        errors = io.StringIO()
        code = main(
            ["search-rom"],
            diagnostic_factory=factory,  # type: ignore[arg-type]
            error_output=errors,
        )

        rendered = json.loads(errors.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(creations, 1)
        self.assertEqual(rendered["status"], "device_limit_exceeded")
        self.assertEqual(rendered["error_type"], "SearchLimitExceeded")
        self.assertEqual(rendered["max_devices"], 15)
        self.assertEqual(rendered["automatic_retries"], 0)

    def test_cli_error_is_not_retried(self) -> None:
        creations = 0

        class FailingDiagnostic:
            def run(self):
                raise UsbTransferTimeout("bounded EP1 poll timed out")

        def factory():
            nonlocal creations
            creations += 1
            return FailingDiagnostic()

        output = io.StringIO()
        errors = io.StringIO()

        code = main(
            ["search-rom"],
            diagnostic_factory=factory,  # type: ignore[arg-type]
            output=output,
            error_output=errors,
        )

        self.assertEqual(code, 1)
        self.assertEqual(creations, 1)
        self.assertEqual(output.getvalue(), "")
        rendered = json.loads(errors.getvalue())
        self.assertEqual(rendered["status"], "error")
        self.assertEqual(rendered["search_attempts"], 1)
        self.assertEqual(rendered["automatic_retries"], 0)

    def test_cli_reports_ep3_transfer_context_without_retrying(self) -> None:
        class FailingDiagnostic:
            def run(self):
                raise UsbTransportError(
                    "bulk IN transfer",
                    -99,
                    requested_length=40,
                    transferred_length=0,
                    status_reported_length=40,
                )

        errors = io.StringIO()
        code = main(
            ["search-rom"],
            diagnostic_factory=FailingDiagnostic,  # type: ignore[arg-type]
            error_output=errors,
        )

        rendered = json.loads(errors.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(rendered["search_attempts"], 1)
        self.assertEqual(rendered["automatic_retries"], 0)
        self.assertFalse(rendered["alternate_setting_changed"])
        self.assertIsNone(rendered["active_alternate_setting"])
        self.assertEqual(
            rendered["usb_transfer"],
            {
                "endpoint": "0x83",
                "status_reported_bytes": 40,
                "requested_bytes": 40,
                "transferred_bytes": 0,
                "libusb_error_code": -99,
            },
        )

    def test_cli_success_runs_exactly_once(self) -> None:
        adapter = FakeAdapter((bytes.fromhex("8194063200000014"),))
        diagnostic = Ds2490SearchDiagnostic(adapter)  # type: ignore[arg-type]
        output = io.StringIO()

        code = main(
            ["search-rom"],
            diagnostic_factory=lambda: diagnostic,
            output=output,
        )

        self.assertEqual(code, 0)
        self.assertEqual(adapter.calls, [15])
        self.assertEqual(json.loads(output.getvalue())["status"], "ok")


if __name__ == "__main__":
    unittest.main()
