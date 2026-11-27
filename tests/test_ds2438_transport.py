from __future__ import annotations

import unittest

from eisy_onewire.ds2438 import Ds2438CrcError, Ds2438RestorationError
from eisy_onewire.ds2438_transport import (
    CONVERT_T,
    CONVERT_V,
    MATCH_ROM,
    PAGE_ZERO,
    READ_SCRATCHPAD,
    RECALL_MEMORY,
    WRITE_SCRATCHPAD,
    Ds2438Transport,
)
from eisy_onewire.ds2490_transport import UsbTransferTimeout
from eisy_onewire.onewire_discovery import dallas_crc8


ROM = bytes.fromhex("2666778899AABB54")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class StatefulDs2438Wire:
    """Model page-zero SRAM, Recall, AD selection, and conversion data."""

    def __init__(
        self,
        *,
        persistent_status: int = 0x01,
        temperature_celsius: float = 23.875,
        vad_raw: int = 198,
        vdd_raw: int = 521,
    ) -> None:
        self.persistent_status = persistent_status
        self.scratchpad = bytearray(8)
        self.scratchpad[0] = persistent_status
        self.temperature_raw = int(temperature_celsius * 256)
        self.vad_raw = vad_raw
        self.vdd_raw = vdd_raw
        self.payloads: list[bytes] = []
        self.selections: list[str] = []
        self.resets = 0
        self.read_count = 0
        self.recall_count = 0
        self.complete = True
        self.corrupt_read_number: int | None = None
        self.ignore_selector_writes = False
        self.corrupt_final_restore = False

    def _reset_bus(self) -> None:
        self.resets += 1

    def _block_io(self, payload: bytes) -> bytes:
        self.payloads.append(payload)
        if payload[:1] != bytes([MATCH_ROM]) or payload[1:9] != ROM:
            raise AssertionError("transport did not Match ROM before DS2438 command")
        command = payload[9]
        if command == RECALL_MEMORY:
            self._require_page_zero(payload)
            self.recall_count += 1
            self.scratchpad[0] = self.persistent_status
            if self.corrupt_final_restore and self.recall_count >= 5:
                self.scratchpad[0] ^= 0x08
            return payload
        if command == WRITE_SCRATCHPAD:
            self._require_page_zero(payload)
            if not self.ignore_selector_writes:
                self.scratchpad[0] = payload[11]
            return payload
        if command == READ_SCRATCHPAD:
            self._require_page_zero(payload)
            self.read_count += 1
            page = bytes(self.scratchpad)
            response = page + bytes([dallas_crc8(page)])
            if self.corrupt_read_number == self.read_count:
                response = response[:-1] + bytes([response[-1] ^ 1])
            return payload[:-9] + response
        if command == CONVERT_T:
            self.scratchpad[1:3] = self.temperature_raw.to_bytes(
                2, "little", signed=True
            )
            return payload
        if command == CONVERT_V:
            selected_vdd = bool(self.scratchpad[0] & 0x08)
            self.selections.append("vdd" if selected_vdd else "vad")
            raw = self.vdd_raw if selected_vdd else self.vad_raw
            self.scratchpad[3:5] = raw.to_bytes(2, "little")
            return payload
        if command == 0x48:
            raise AssertionError("Copy Scratchpad must never be issued")
        raise AssertionError(f"unexpected DS2438 command 0x{command:02X}")

    def _read_bit(self) -> int:
        return int(self.complete)

    @staticmethod
    def _require_page_zero(payload: bytes) -> None:
        if len(payload) < 11 or payload[10] != PAGE_ZERO:
            raise AssertionError("DS2438 operation did not select page zero")


class Ds2438TransportTests(unittest.TestCase):
    def test_low_solar_vad_uses_the_existing_conversion_sequence(self) -> None:
        wire = StatefulDs2438Wire(vad_raw=74, vdd_raw=497)

        result = Ds2438Transport(object(), wire=wire).read_measurements(ROM)  # type: ignore[arg-type]

        self.assertEqual(result.vad_volts, 0.74)
        self.assertEqual(result.vdd_volts, 4.97)
        self.assertEqual(wire.selections, ["vad", "vdd"])

    def test_stateful_model_proves_distinct_vad_vdd_selection_and_ram_restore(self) -> None:
        wire = StatefulDs2438Wire()

        result = Ds2438Transport(object(), wire=wire).read_measurements(ROM)  # type: ignore[arg-type]

        self.assertEqual(result.temperature_celsius, 23.875)
        self.assertEqual(result.temperature_fahrenheit, 74.975)
        self.assertEqual(result.vad_volts, 1.98)
        self.assertEqual(result.vdd_volts, 5.21)
        self.assertEqual(wire.selections, ["vad", "vdd"])
        self.assertEqual(wire.scratchpad[0], wire.persistent_status)
        self.assertEqual(
            sum(payload[9] == WRITE_SCRATCHPAD for payload in wire.payloads), 2
        )
        self.assertFalse(any(payload[9] == 0x48 for payload in wire.payloads))

    def test_crc_failure_still_recalls_and_verifies_original_selector(self) -> None:
        wire = StatefulDs2438Wire()
        wire.corrupt_read_number = 2

        with self.assertRaises(Ds2438CrcError):
            Ds2438Transport(object(), wire=wire).read_measurements(ROM)  # type: ignore[arg-type]

        self.assertGreaterEqual(wire.recall_count, 3)
        self.assertEqual(wire.scratchpad[0], wire.persistent_status)
        self.assertEqual(wire.selections, [])

    def test_conversion_timeout_is_bounded_and_selector_cleanup_is_attempted(self) -> None:
        clock = FakeClock()
        wire = StatefulDs2438Wire()
        wire.complete = False

        with self.assertRaises(UsbTransferTimeout):
            Ds2438Transport(
                object(),  # type: ignore[arg-type]
                wire=wire,
                monotonic=clock.monotonic,
                sleeper=clock.sleep,
            ).read_measurements(ROM)

        self.assertLessEqual(clock.now, 0.03)
        self.assertEqual(wire.scratchpad[0], wire.persistent_status)
        self.assertFalse(any(payload[9] == 0x48 for payload in wire.payloads))

    def test_unverified_selector_write_and_final_restoration_fail_closed(self) -> None:
        ignored = StatefulDs2438Wire(persistent_status=0x09)
        ignored.ignore_selector_writes = True
        with self.assertRaises(Ds2438RestorationError):
            Ds2438Transport(object(), wire=ignored).read_measurements(ROM)  # type: ignore[arg-type]

        wrong_restore = StatefulDs2438Wire()
        wrong_restore.corrupt_final_restore = True
        with self.assertRaises(Ds2438RestorationError):
            Ds2438Transport(object(), wire=wrong_restore).read_measurements(ROM)  # type: ignore[arg-type]

        self.assertEqual(wrong_restore.selections, ["vad", "vdd"])
        self.assertFalse(
            any(payload[9] == 0x48 for payload in wrong_restore.payloads)
        )


if __name__ == "__main__":
    unittest.main()
