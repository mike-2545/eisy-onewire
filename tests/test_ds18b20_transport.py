from __future__ import annotations

import unittest

from eisy_onewire.ds18b20 import (
    ConversionTimeoutError,
    DeviceUnavailableError,
    PowerMode,
    UnsupportedPowerModeError,
)
from eisy_onewire.ds18b20_transport import (
    CONVERT_T,
    MATCH_ROM,
    READ_POWER_SUPPLY,
    Ds18b20Adapter,
    Ds18b20Transport,
)
from eisy_onewire.ds2490_transport import (
    Ds2490ProtocolError,
    FLEXIBLE_DATA_SAMPLE_OFFSET,
    FLEXIBLE_SLEW_RATE,
    FLEXIBLE_WRITE1_LOW_TIME,
    ONEWIRE_SPEED_FLEXIBLE,
    RESULT_NO_RESPONSE,
    UsbTransportError,
)
from eisy_onewire.onewire_discovery import dallas_crc8


ROM = bytes.fromhex("2811223344556656")


def status(*, flags: int = 0x20, data_in: int = 0, results: bytes = b"") -> bytes:
    packet = bytearray(16)
    packet[1] = ONEWIRE_SPEED_FLEXIBLE
    packet[4] = FLEXIBLE_SLEW_RATE
    packet[5] = FLEXIBLE_WRITE1_LOW_TIME
    packet[6] = FLEXIBLE_DATA_SAMPLE_OFFSET
    packet[8] = flags
    packet[13] = data_in
    packet.extend(results)
    return bytes(packet)


def scratchpad(raw_temperature: int = 0x0191, *, resolution: int = 12) -> bytes:
    config = {9: 0x1F, 10: 0x3F, 11: 0x5F, 12: 0x7F}[resolution]
    prefix = raw_temperature.to_bytes(2, "little", signed=True) + bytes(
        [0x4B, 0x46, config, 0xFF, 0x0C, 0x10]
    )
    return prefix + bytes([dallas_crc8(prefix)])


class FakeSession:
    def __init__(self) -> None:
        self.controls: list[tuple[int, int, int]] = []
        self.bulk_writes: list[bytes] = []
        self.bulk_read_requests: list[int] = []
        self.bulk_reads: list[bytes | BaseException] = []
        self.status_reads: list[bytes] = []

    def control(self, request: int, value: int, index: int) -> None:
        self.controls.append((request, value, index))

    def bulk_out(self, payload: bytes) -> None:
        self.bulk_writes.append(payload)

    def bulk_in(self, length: int) -> bytes:
        self.bulk_read_requests.append(length)
        result = self.bulk_reads.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def interrupt_in(self, length: int) -> bytes:
        if length != 32:
            raise AssertionError(f"unexpected status request {length}")
        return self.status_reads.pop(0)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeContext:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.exited = False
        self.exception_type: type[BaseException] | None = None

    def __enter__(self) -> FakeSession:
        return self.session

    def __exit__(self, exception_type, exception, traceback) -> None:
        self.exited = True
        self.exception_type = exception_type


class FakeFactory:
    def __init__(self, context: FakeContext) -> None:
        self.context = context

    def open(self) -> FakeContext:
        return self.context


class Ds18b20TransportTests(unittest.TestCase):
    def test_read_power_supply_reports_external_without_conversion(self) -> None:
        session = FakeSession()
        session.status_reads = [status(), status(data_in=10), status(data_in=1)]
        session.bulk_reads = [bytes(10), b"\x01"]
        transport = Ds18b20Transport(session)

        power = transport.read_power_supply(ROM)

        self.assertIs(power, PowerMode.EXTERNAL)
        self.assertEqual(
            session.bulk_writes,
            [bytes([MATCH_ROM]) + ROM + bytes([READ_POWER_SUPPLY])],
        )
        self.assertNotEqual(session.bulk_writes[0][-1], CONVERT_T)

    def test_read_power_supply_reports_parasite_without_conversion(self) -> None:
        session = FakeSession()
        session.status_reads = [status(), status(data_in=10), status(data_in=1)]
        session.bulk_reads = [bytes(10), b"\x00"]

        self.assertIs(
            Ds18b20Transport(session).read_power_supply(ROM), PowerMode.PARASITE
        )

    def test_parasite_conversion_is_rejected_before_any_transport_operation(self) -> None:
        session = FakeSession()
        transport = Ds18b20Transport(session)

        with self.assertRaises(UnsupportedPowerModeError):
            transport.reject_parasite_conversion()

        self.assertEqual(session.controls, [])
        self.assertEqual(session.bulk_writes, [])

    def test_read_scratchpad_returns_exact_crc_valid_nine_bytes(self) -> None:
        raw = scratchpad()
        session = FakeSession()
        session.status_reads = [status(), status(data_in=19)]
        session.bulk_reads = [bytes(10) + raw]

        returned = Ds18b20Transport(session).read_scratchpad(ROM)

        self.assertEqual(returned, raw)
        self.assertEqual(session.bulk_read_requests, [19])

    def test_missing_presence_and_all_ff_scratchpad_are_rejected(self) -> None:
        missing = FakeSession()
        missing.status_reads = [status(results=bytes([RESULT_NO_RESPONSE]))]
        with self.assertRaises(DeviceUnavailableError):
            Ds18b20Transport(missing).read_scratchpad(ROM)

        absent_target = FakeSession()
        absent_target.status_reads = [status(), status(data_in=19)]
        absent_target.bulk_reads = [bytes(10) + bytes([0xFF]) * 9]
        with self.assertRaises(DeviceUnavailableError):
            Ds18b20Transport(absent_target).read_scratchpad(ROM)

    def test_incomplete_scratchpad_and_disconnect_fail_closed(self) -> None:
        short = FakeSession()
        short.status_reads = [status(), status(data_in=19)]
        short.bulk_reads = [bytes(18)]
        with self.assertRaisesRegex(Ds2490ProtocolError, "returned 18"):
            Ds18b20Transport(short).read_scratchpad(ROM)

        disconnected = FakeSession()
        disconnected.status_reads = [status(), status(data_in=19)]
        disconnected.bulk_reads = [UsbTransportError("bulk IN transfer", -4)]
        with self.assertRaises(UsbTransportError):
            Ds18b20Transport(disconnected).read_scratchpad(ROM)

    def test_external_conversion_polls_until_complete(self) -> None:
        clock = FakeClock()
        session = FakeSession()
        session.status_reads = [
            status(),
            status(data_in=10),
            status(data_in=1),
            status(data_in=1),
        ]
        session.bulk_reads = [bytes(10), b"\x00", b"\x01"]
        transport = Ds18b20Transport(
            session, monotonic=clock.monotonic, sleeper=clock.sleep
        )

        receipt = transport.convert_temperature_external(ROM, resolution_bits=12)

        self.assertEqual(receipt.rom, ROM)
        self.assertEqual(receipt.resolution_bits, 12)
        self.assertEqual(receipt.maximum_seconds, 0.750)
        self.assertEqual(receipt.polls, 2)
        self.assertEqual(clock.sleeps, [0.010])
        self.assertEqual(
            session.bulk_writes, [bytes([MATCH_ROM]) + ROM + bytes([CONVERT_T])]
        )

    def test_conversion_timeout_is_bounded_and_does_not_read_scratchpad(self) -> None:
        clock = FakeClock()
        session = FakeSession()
        poll_count = 21
        session.status_reads = [status(), status(data_in=10)] + [
            status(data_in=1)
        ] * poll_count
        session.bulk_reads = [bytes(10)] + [b"\x00"] * poll_count
        transport = Ds18b20Transport(
            session, monotonic=clock.monotonic, sleeper=clock.sleep
        )

        with self.assertRaises(ConversionTimeoutError):
            transport.convert_temperature_external(ROM, resolution_bits=9)

        self.assertEqual(len(session.bulk_writes), 1)
        self.assertLessEqual(clock.now, 0.19375)

    def test_adapter_releases_session_when_scratchpad_transfer_fails(self) -> None:
        session = FakeSession()
        session.status_reads = [status(), status(), status(), status(data_in=19)]
        session.bulk_reads = [bytes(18)]
        context = FakeContext(session)

        with self.assertRaises(Ds2490ProtocolError):
            Ds18b20Adapter(FakeFactory(context)).detect_power(ROM)

        self.assertTrue(context.exited)
        self.assertIs(context.exception_type, Ds2490ProtocolError)

    def test_adapter_stops_before_convert_t_when_sensor_is_parasite_powered(self) -> None:
        raw = scratchpad()
        session = FakeSession()
        session.status_reads = [
            status(),
            status(),
            status(),
            status(data_in=19),
            status(),
            status(data_in=10),
            status(data_in=1),
        ]
        session.bulk_reads = [bytes(10) + raw, bytes(10), b"\x00"]
        context = FakeContext(session)

        with self.assertRaises(UnsupportedPowerModeError):
            Ds18b20Adapter(FakeFactory(context)).read_temperature(ROM)

        self.assertTrue(context.exited)
        self.assertEqual(len(session.bulk_writes), 2)
        self.assertFalse(
            any(payload.endswith(bytes([CONVERT_T])) for payload in session.bulk_writes)
        )


if __name__ == "__main__":
    unittest.main()
