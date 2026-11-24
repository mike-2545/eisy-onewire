from __future__ import annotations

import io
import json
import unittest

from eisy_onewire.ds18b20 import PowerMode, UnsupportedPowerModeError
from eisy_onewire.ds18b20_transport import ConversionReceipt
from eisy_onewire.onewire_discovery import dallas_crc8
from eisy_onewire.temperature_diagnostic import main


ROM_TEXT = "2811223344556656"
ROM = bytes.fromhex(ROM_TEXT)


def scratchpad(raw_temperature: int = 0x0191) -> bytes:
    prefix = raw_temperature.to_bytes(2, "little", signed=True) + bytes(
        [0x4B, 0x46, 0x7F, 0xFF, 0x0C, 0x10]
    )
    return prefix + bytes([dallas_crc8(prefix)])


class FakeAdapter:
    def __init__(
        self,
        *,
        power: PowerMode = PowerMode.EXTERNAL,
        raw: bytes | None = None,
        failure: BaseException | None = None,
        receipt: ConversionReceipt | None = None,
    ) -> None:
        self.power = power
        self.raw = raw or scratchpad()
        self.failure = failure
        self.receipt = receipt
        self.calls: list[tuple[str, bytes]] = []

    def detect_power(self, rom: bytes) -> PowerMode:
        self.calls.append(("power", rom))
        if self.failure:
            raise self.failure
        return self.power

    def read_temperature(self, rom: bytes):
        self.calls.append(("temperature", rom))
        if self.failure:
            raise self.failure
        return self.power, self.raw, self.receipt or ConversionReceipt(rom, 12, 0.750, 3)


class TemperatureDiagnosticTests(unittest.TestCase):
    def test_power_only_reports_external_and_never_converts(self) -> None:
        adapter = FakeAdapter(power=PowerMode.EXTERNAL)
        output = io.StringIO()

        code = main(
            ["temperature-power", "--rom", ROM_TEXT],
            adapter_factory=lambda: adapter,
            output=output,
        )

        rendered = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(adapter.calls, [("power", ROM)])
        self.assertEqual(rendered["power_mode"], "external")
        self.assertFalse(rendered["convert_t_issued"])
        self.assertFalse(rendered["strong_pullup_enabled"])
        self.assertEqual(rendered["automatic_retries"], 0)

    def test_power_only_reports_parasite_without_converting(self) -> None:
        adapter = FakeAdapter(power=PowerMode.PARASITE)
        output = io.StringIO()

        code = main(
            ["temperature-power", "--rom", ROM_TEXT],
            adapter_factory=lambda: adapter,
            output=output,
        )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["power_mode"], "parasite")
        self.assertEqual(adapter.calls, [("power", ROM)])

    def test_temperature_read_reports_fahrenheit_and_fresh_conversion(self) -> None:
        adapter = FakeAdapter(raw=scratchpad(-162))
        output = io.StringIO()

        code = main(
            ["temperature-read", "--rom", ROM_TEXT],
            adapter_factory=lambda: adapter,
            output=output,
        )

        rendered = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(adapter.calls, [("temperature", ROM)])
        self.assertTrue(rendered["conversion_complete"])
        self.assertTrue(rendered["scratchpad_crc_valid"])
        self.assertEqual(rendered["temperature_celsius"], -10.125)
        self.assertAlmostEqual(rendered["temperature_fahrenheit"], 13.775)
        self.assertEqual(rendered["iox_writes"], 0)

    def test_unsupported_parasite_conversion_is_reported_without_retry(self) -> None:
        adapter = FakeAdapter(
            power=PowerMode.PARASITE,
            failure=UnsupportedPowerModeError("strong pull-up not approved"),
        )
        errors = io.StringIO()

        code = main(
            ["temperature-read", "--rom", ROM_TEXT],
            adapter_factory=lambda: adapter,
            error_output=errors,
        )

        rendered = json.loads(errors.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(rendered["error_type"], "UnsupportedPowerModeError")
        self.assertEqual(rendered["automatic_retries"], 0)
        self.assertEqual(adapter.calls, [("temperature", ROM)])

    def test_mismatched_conversion_receipt_is_rejected_as_stale(self) -> None:
        adapter = FakeAdapter(
            receipt=ConversionReceipt(bytes.fromhex("2822334455667727"), 12, 0.750, 1)
        )
        errors = io.StringIO()

        code = main(
            ["temperature-read", "--rom", ROM_TEXT],
            adapter_factory=lambda: adapter,
            error_output=errors,
        )

        rendered = json.loads(errors.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(rendered["error_type"], "StaleConversionError")

    def test_invalid_rom_fails_before_adapter_construction(self) -> None:
        creations = 0

        def factory():
            nonlocal creations
            creations += 1
            return FakeAdapter()

        errors = io.StringIO()
        code = main(
            ["temperature-power", "--rom", "2811223344556657"],
            adapter_factory=factory,
            error_output=errors,
        )

        self.assertEqual(code, 1)
        self.assertEqual(creations, 0)
        self.assertEqual(json.loads(errors.getvalue())["error_type"], "InvalidRomError")


if __name__ == "__main__":
    unittest.main()
