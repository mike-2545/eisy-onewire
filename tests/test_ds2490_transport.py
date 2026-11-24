from __future__ import annotations

import unittest

from eisy_onewire.ds2490_transport import (
    COMMUNICATION_COMMAND,
    COMM_FLUSH_ON_ERROR,
    COMM_IMMEDIATE,
    COMM_RESET,
    COMM_RETURN_DISCREPANCY,
    COMM_SEARCH_ACCESS,
    COMM_SEARCH_MODE,
    CONTROL_COMMAND,
    CONTROL_RESET_DEVICE,
    FLEXIBLE_DATA_SAMPLE_OFFSET,
    FLEXIBLE_SLEW_RATE,
    FLEXIBLE_WRITE1_LOW_TIME,
    MODE_1WIRE_SPEED,
    MODE_COMMAND,
    MODE_DATA_SAMPLE_OFFSET,
    MODE_PULLDOWN_SLEW_RATE,
    MODE_WRITE1_LOW_TIME,
    ONEWIRE_SPEED_FLEXIBLE,
    CtypesLibUsbSessionFactory,
    Ds2490Adapter,
    Ds2490ProtocolError,
    Ds2490Transport,
    RESULT_END_OF_SEARCH,
    RESULT_NO_RESPONSE,
    RESULT_SHORT,
    SEARCH_ROM,
    SearchLimitExceeded,
    AdapterNotFoundError,
    UsbPermissionError,
    UsbInterfaceBusyError,
    UsbConfigurationError,
    UsbEndpointUnavailableError,
    UsbTransportError,
    UsbTransferTimeout,
    _NativeUsbSession,
    _check_operation,
    _check_transfer,
)


def status(
    *,
    speed: int = ONEWIRE_SPEED_FLEXIBLE,
    slew_rate: int = FLEXIBLE_SLEW_RATE,
    write1_low_time: int = FLEXIBLE_WRITE1_LOW_TIME,
    data_sample_offset: int = FLEXIBLE_DATA_SAMPLE_OFFSET,
    flags: int = 0x20,
    data_in: int = 0,
    results: bytes = b"",
) -> bytes:
    packet = bytearray(16)
    packet[1] = speed
    packet[4] = slew_rate
    packet[5] = write1_low_time
    packet[6] = data_sample_offset
    packet[8] = flags
    packet[13] = data_in
    packet.extend(results)
    return bytes(packet)


class FakeSession:
    def __init__(self) -> None:
        self.controls: list[tuple[int, int, int]] = []
        self.bulk_writes: list[bytes] = []
        self.bulk_reads: list[bytes] = []
        self.bulk_read_requests: list[int] = []
        self.status_reads: list[bytes] = []

    def control(self, request: int, value: int, index: int) -> None:
        self.controls.append((request, value, index))

    def bulk_out(self, payload: bytes) -> None:
        self.bulk_writes.append(payload)

    def bulk_in(self, length: int) -> bytes:
        self.bulk_read_requests.append(length)
        return self.bulk_reads.pop(0)

    def interrupt_in(self, length: int) -> bytes:
        self.assert_status_size(length)
        return self.status_reads.pop(0)

    @staticmethod
    def assert_status_size(length: int) -> None:
        if length != 32:
            raise AssertionError(f"unexpected status size {length}")


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


class FakeOpenLibrary:
    def __init__(
        self,
        *,
        configuration: int = 1,
        claim_result: int = 0,
        alternate_result: int = 0,
        release_result: int = 0,
    ) -> None:
        self.configuration = configuration
        self.claim_result = claim_result
        self.alternate_result = alternate_result
        self.release_result = release_result
        self.calls: list[tuple[object, ...]] = []

    def libusb_init(self, context) -> int:
        self.calls.append(("init",))
        return 0

    def libusb_get_configuration(self, handle, configuration) -> int:
        self.calls.append(("get_configuration", handle))
        configuration._obj.value = self.configuration
        return 0

    def libusb_set_configuration(self, handle, configuration) -> int:
        self.calls.append(("set_configuration", handle, configuration))
        return 0

    def libusb_claim_interface(self, handle, interface) -> int:
        self.calls.append(("claim", handle, interface))
        return self.claim_result

    def libusb_set_interface_alt_setting(self, handle, interface, alternate) -> int:
        self.calls.append(("alternate", handle, interface, alternate))
        return self.alternate_result

    def libusb_release_interface(self, handle, interface) -> int:
        self.calls.append(("release", handle, interface))
        return self.release_result

    def libusb_close(self, handle) -> None:
        self.calls.append(("close", handle))

    def libusb_exit(self, context) -> None:
        self.calls.append(("exit",))


class FakeOpenFactory(CtypesLibUsbSessionFactory):
    def _open_matching(self, context):
        return "handle"


class FakeTransferLibrary:
    def libusb_bulk_transfer(
        self, handle, endpoint, buffer, length, transferred, timeout
    ) -> int:
        transferred._obj.value = 7
        return -99


class Ds2490TransportTests(unittest.TestCase):
    def test_initialization_and_multiple_rom_search_transactions(self) -> None:
        session = FakeSession()
        internal_id = bytes.fromhex("8194063200000014")
        sensor_a = bytes.fromhex("2811223344556656")
        session.status_reads = [
            status(),
            status(),
            status(data_in=16, results=bytes([RESULT_END_OF_SEARCH])),
        ]
        session.bulk_reads = [internal_id + sensor_a]
        transport = Ds2490Transport(session)

        transport.initialize()
        found = transport.search_roms(4)

        expected_search = (
            COMM_SEARCH_ACCESS
            | COMM_IMMEDIATE
            | COMM_SEARCH_MODE
            | COMM_RESET
            | COMM_FLUSH_ON_ERROR
            | COMM_RETURN_DISCREPANCY
        )
        self.assertEqual(
            session.controls,
            [
                (CONTROL_COMMAND, CONTROL_RESET_DEVICE, 0),
                (MODE_COMMAND, MODE_1WIRE_SPEED, ONEWIRE_SPEED_FLEXIBLE),
                (MODE_COMMAND, MODE_PULLDOWN_SLEW_RATE, FLEXIBLE_SLEW_RATE),
                (MODE_COMMAND, MODE_WRITE1_LOW_TIME, FLEXIBLE_WRITE1_LOW_TIME),
                (
                    MODE_COMMAND,
                    MODE_DATA_SAMPLE_OFFSET,
                    FLEXIBLE_DATA_SAMPLE_OFFSET,
                ),
                (COMMUNICATION_COMMAND, expected_search, (4 << 8) | SEARCH_ROM),
            ],
        )
        self.assertEqual(session.bulk_writes, [bytes(8)])
        self.assertEqual(session.bulk_read_requests, [16])
        self.assertEqual(found, (internal_id, sensor_a))

    def test_status_count_changes_are_not_read_until_idle(self) -> None:
        session = FakeSession()
        roms = tuple(bytes([family]) + bytes(7) for family in range(1, 6))
        session.status_reads = [
            status(flags=0, data_in=8),
            status(flags=0, data_in=24),
            status(data_in=40),
        ]
        session.bulk_reads = [b"".join(roms)]

        found = Ds2490Transport(session).search_roms()

        self.assertEqual(session.bulk_read_requests, [40])
        self.assertEqual(found, roms)

    def test_early_two_byte_short_packet_is_avoided_by_waiting_for_idle(self) -> None:
        valid_rom = bytes.fromhex("2811223344556656")

        class EarlyShortSession(FakeSession):
            def __init__(self) -> None:
                super().__init__()
                self.status_count = 0

            def interrupt_in(self, length: int) -> bytes:
                self.status_count += 1
                return super().interrupt_in(length)

            def bulk_in(self, length: int) -> bytes:
                self.bulk_read_requests.append(length)
                if self.status_count < 2:
                    return b"\x00\x00"
                return self.bulk_reads.pop(0)

        session = EarlyShortSession()
        session.status_reads = [
            status(flags=0, data_in=8),
            status(data_in=8),
        ]
        session.bulk_reads = [valid_rom]

        found = Ds2490Transport(session).search_roms()

        self.assertEqual(found, (valid_rom,))
        self.assertEqual(session.status_count, 2)
        self.assertEqual(session.bulk_read_requests, [8])

    def test_initialization_fails_if_flexible_profile_is_not_applied(self) -> None:
        session = FakeSession()
        session.status_reads = [status(), status(speed=0)]

        with self.assertRaisesRegex(Ds2490ProtocolError, "did not apply"):
            Ds2490Transport(session).initialize()

    def test_no_presence_is_an_empty_discovery(self) -> None:
        session = FakeSession()
        session.status_reads = [status(results=bytes([RESULT_NO_RESPONSE]))]

        self.assertEqual(Ds2490Transport(session).search_roms(), ())

    def test_partial_no_response_is_rejected(self) -> None:
        session = FakeSession()
        session.status_reads = [
            status(data_in=8, results=bytes([RESULT_NO_RESPONSE]))
        ]
        session.bulk_reads = [bytes(8)]

        with self.assertRaisesRegex(Ds2490ProtocolError, "partial data"):
            Ds2490Transport(session).search_roms()

    def test_bus_short_is_rejected_without_bulk_read(self) -> None:
        session = FakeSession()
        session.status_reads = [status(results=bytes([RESULT_SHORT]))]

        with self.assertRaisesRegex(Ds2490ProtocolError, "short"):
            Ds2490Transport(session).search_roms()

        self.assertEqual(session.bulk_reads, [])

    def test_malformed_bulk_response_is_rejected(self) -> None:
        session = FakeSession()
        session.status_reads = [status(data_in=7)]
        session.bulk_reads = [bytes(7)]

        with self.assertRaisesRegex(Ds2490ProtocolError, "complete ROM blocks"):
            Ds2490Transport(session).search_roms()

    def test_short_ep3_read_cannot_be_accepted_as_a_complete_device(self) -> None:
        session = FakeSession()
        valid_rom = bytes.fromhex("2811223344556656")
        session.status_reads = [status(data_in=16)]
        session.bulk_reads = [valid_rom]

        with self.assertRaisesRegex(
            Ds2490ProtocolError,
            "returned 8 bytes; final idle status required 16",
        ):
            Ds2490Transport(session).search_roms()

        self.assertEqual(session.bulk_read_requests, [16])

    def test_zero_length_ep3_read_is_rejected(self) -> None:
        session = FakeSession()
        session.status_reads = [status(data_in=8)]
        session.bulk_reads = [b""]

        with self.assertRaisesRegex(
            Ds2490ProtocolError,
            "returned 0 bytes; final idle status required 8",
        ):
            Ds2490Transport(session).search_roms()

        self.assertEqual(session.bulk_read_requests, [8])

    def test_ep3_other_error_records_snapshot_request_and_transfer_lengths(self) -> None:
        class FailingBulkSession(FakeSession):
            def bulk_in(self, length: int) -> bytes:
                self.bulk_read_requests.append(length)
                raise UsbTransportError(
                    "bulk IN transfer",
                    -99,
                    requested_length=length,
                    transferred_length=0,
                )

        session = FailingBulkSession()
        session.status_reads = [status(data_in=40)]

        with self.assertRaises(UsbTransportError) as raised:
            Ds2490Transport(session).search_roms()

        error = raised.exception
        self.assertEqual(error.code, -99)
        self.assertEqual(error.status_reported_length, 40)
        self.assertEqual(error.requested_length, 40)
        self.assertEqual(error.transferred_length, 0)
        self.assertIn("EP1 reported 40 bytes", str(error))
        self.assertIn("EP3 requested 40 bytes", str(error))

    def test_native_ep3_other_error_retains_libusb_transfer_count(self) -> None:
        session = _NativeUsbSession(FakeTransferLibrary(), "context", "handle")

        with self.assertRaises(UsbTransportError) as raised:
            session.bulk_in(128)

        error = raised.exception
        self.assertEqual(error.code, -99)
        self.assertEqual(error.requested_length, 128)
        self.assertEqual(error.transferred_length, 7)

    def test_status_packet_larger_than_endpoint_maximum_is_rejected(self) -> None:
        session = FakeSession()
        session.status_reads = [bytes(33)]

        with self.assertRaisesRegex(Ds2490ProtocolError, "16..32"):
            Ds2490Transport(session).search_roms()

    def test_full_fifo_is_read_only_after_idle_and_enforces_device_limit(self) -> None:
        session = FakeSession()
        session.status_reads = [status(flags=0, data_in=120), status(data_in=128)]
        session.bulk_reads = [bytes(128)]

        with self.assertRaises(SearchLimitExceeded):
            Ds2490Transport(session).search_roms()

        self.assertEqual(session.bulk_read_requests, [128])

    def test_discrepancy_block_beyond_limit_is_not_accepted_as_a_rom(self) -> None:
        session = FakeSession()
        session.status_reads = [status(data_in=16)]
        session.bulk_reads = [bytes(16)]

        with self.assertRaises(SearchLimitExceeded):
            Ds2490Transport(session).search_roms(max_devices=1)

    def test_status_poll_limit_becomes_transfer_timeout(self) -> None:
        session = FakeSession()
        session.status_reads = [status(flags=0), status(flags=0)]

        with self.assertRaises(UsbTransferTimeout):
            Ds2490Transport(session, max_status_polls=2).search_roms()

    def test_adapter_releases_session_after_protocol_error(self) -> None:
        session = FakeSession()
        session.status_reads = [status(), status(), b"short"]
        context = FakeContext(session)

        with self.assertRaises(Ds2490ProtocolError):
            Ds2490Adapter(FakeFactory(context)).search_roms()

        self.assertTrue(context.exited)
        self.assertIs(context.exception_type, Ds2490ProtocolError)

    def test_adapter_releases_without_search_if_timing_verification_fails(self) -> None:
        session = FakeSession()
        session.status_reads = [status(), status(speed=0)]
        context = FakeContext(session)

        with self.assertRaisesRegex(Ds2490ProtocolError, "did not apply"):
            Ds2490Adapter(FakeFactory(context)).search_roms()

        self.assertTrue(context.exited)
        self.assertIs(context.exception_type, Ds2490ProtocolError)
        self.assertEqual(session.bulk_writes, [])
        self.assertNotIn(COMMUNICATION_COMMAND, [item[0] for item in session.controls])

    def test_adapter_releases_session_after_short_ep3_read(self) -> None:
        session = FakeSession()
        session.status_reads = [status(), status(), status(data_in=16)]
        session.bulk_reads = [bytes.fromhex("2811223344556656")]
        context = FakeContext(session)

        with self.assertRaisesRegex(
            Ds2490ProtocolError, "final idle status required 16"
        ):
            Ds2490Adapter(FakeFactory(context)).search_roms()

        self.assertTrue(context.exited)
        self.assertIs(context.exception_type, Ds2490ProtocolError)

    def test_adapter_releases_session_after_success(self) -> None:
        session = FakeSession()
        session.status_reads = [status(), status(), status()]
        context = FakeContext(session)

        self.assertEqual(Ds2490Adapter(FakeFactory(context)).search_roms(), ())
        self.assertTrue(context.exited)
        self.assertIsNone(context.exception_type)

    def test_native_error_codes_have_distinct_public_failures(self) -> None:
        with self.assertRaises(AdapterNotFoundError):
            _check_operation("open DS9490", -4)
        with self.assertRaises(UsbPermissionError):
            _check_operation("claim DS2490 interface", -3)
        with self.assertRaises(UsbInterfaceBusyError):
            _check_operation("claim DS2490 interface", -6)
        with self.assertRaises(UsbTransferTimeout):
            _check_transfer("bulk IN transfer", -7)
        with self.assertRaises(UsbEndpointUnavailableError):
            _check_transfer("interrupt IN transfer", -9)

    def test_native_open_failure_after_claim_releases_every_resource(self) -> None:
        library = FakeOpenLibrary(alternate_result=-1)
        factory = FakeOpenFactory(library)

        with self.assertRaisesRegex(UsbTransportError, "alternate setting"):
            factory.open()

        self.assertIn(("release", "handle", 0), library.calls)
        self.assertIn(("close", "handle"), library.calls)
        self.assertIn(("exit",), library.calls)
        self.assertLess(
            library.calls.index(("release", "handle", 0)),
            library.calls.index(("close", "handle")),
        )

    def test_status_policy_preserves_configuration_and_alternate_setting(self) -> None:
        library = FakeOpenLibrary()
        factory = FakeOpenFactory.for_status_diagnostic(library)

        with factory.open():
            pass

        operation_names = [call[0] for call in library.calls]
        self.assertNotIn("set_configuration", operation_names)
        self.assertNotIn("alternate", operation_names)
        self.assertEqual(operation_names.count("claim"), 1)
        self.assertEqual(operation_names.count("release"), 1)
        self.assertEqual(operation_names.count("close"), 1)
        self.assertEqual(operation_names.count("exit"), 1)

    def test_search_policy_preserves_configuration_and_alternate_setting(self) -> None:
        library = FakeOpenLibrary()
        factory = FakeOpenFactory.for_search_diagnostic(library)

        with factory.open():
            pass

        operation_names = [call[0] for call in library.calls]
        self.assertNotIn("set_configuration", operation_names)
        self.assertNotIn("alternate", operation_names)
        self.assertEqual(operation_names.count("claim"), 1)
        self.assertEqual(operation_names.count("release"), 1)

    def test_status_policy_stops_if_configuration_one_is_not_active(self) -> None:
        library = FakeOpenLibrary(configuration=0)
        factory = FakeOpenFactory.for_status_diagnostic(library)

        with self.assertRaisesRegex(UsbConfigurationError, "left it unchanged"):
            factory.open()

        operation_names = [call[0] for call in library.calls]
        self.assertNotIn("set_configuration", operation_names)
        self.assertNotIn("claim", operation_names)
        self.assertNotIn("alternate", operation_names)
        self.assertIn("close", operation_names)
        self.assertIn("exit", operation_names)

    def test_claim_busy_closes_handle_without_release_attempt(self) -> None:
        library = FakeOpenLibrary(claim_result=-6)
        factory = FakeOpenFactory.for_status_diagnostic(library)

        with self.assertRaises(UsbInterfaceBusyError):
            factory.open()

        operation_names = [call[0] for call in library.calls]
        self.assertNotIn("release", operation_names)
        self.assertIn("close", operation_names)
        self.assertIn("exit", operation_names)

    def test_release_failure_still_closes_handle_and_context(self) -> None:
        library = FakeOpenLibrary(release_result=-4)
        factory = FakeOpenFactory.for_status_diagnostic(library)

        with self.assertRaises(AdapterNotFoundError):
            with factory.open():
                pass

        operation_names = [call[0] for call in library.calls]
        self.assertIn("release", operation_names)
        self.assertIn("close", operation_names)
        self.assertIn("exit", operation_names)


if __name__ == "__main__":
    unittest.main()
