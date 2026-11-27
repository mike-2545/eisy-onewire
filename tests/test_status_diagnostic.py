from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr

from eisy_onewire.ds2490_transport import (
    AdapterNotFoundError,
    Ds2490ProtocolError,
    UsbEndpointUnavailableError,
    UsbPermissionError,
    UsbTransferTimeout,
)
from eisy_onewire.status_diagnostic import (
    Ds2490StatusDiagnostic,
    diagnostic_report,
    main,
    parse_adapter_status,
)

ADAPTER_ROM = "814455667788999C"


class ImmediateAccess:
    def run(self, expected, operation):
        self.expected = expected
        return operation(object(), object())


def valid_status(*, results: bytes = b"") -> bytes:
    packet = bytearray(16)
    packet[0] = 0x01
    packet[1] = 0x00
    packet[2] = 0x04
    packet[4] = 0x04
    packet[5] = 0x04
    packet[6] = 0x04
    packet[8] = 0x20
    packet.extend(results)
    return bytes(packet)


class RestrictedSession:
    """Fail the test if the diagnostic attempts any command or bulk I/O."""

    def __init__(self, response: bytes | BaseException) -> None:
        self.response = response
        self.calls: list[tuple[object, ...]] = []

    def control(self, request: int, value: int, index: int) -> None:
        raise AssertionError("status diagnostic issued a forbidden control transfer")

    def bulk_out(self, payload: bytes) -> None:
        raise AssertionError("status diagnostic issued a forbidden bulk OUT transfer")

    def bulk_in(self, length: int) -> bytes:
        raise AssertionError("status diagnostic issued a forbidden bulk IN transfer")

    def interrupt_in(self, length: int) -> bytes:
        self.calls.append(("interrupt_in", length))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class RecordingContext:
    def __init__(self, session: RestrictedSession) -> None:
        self.session = session
        self.exited = False
        self.exception_type: type[BaseException] | None = None

    def __enter__(self) -> RestrictedSession:
        return self.session

    def __exit__(self, exception_type, exception, traceback) -> None:
        self.exited = True
        self.exception_type = exception_type


class RecordingFactory:
    def __init__(self, context: RecordingContext) -> None:
        self.context = context
        self.opens = 0

    def open(self) -> RecordingContext:
        self.opens += 1
        return self.context


class NeverConstructDiagnostic:
    def __init__(self) -> None:
        raise AssertionError("diagnostic constructed without explicit subcommand")


class StatusDiagnosticTests(unittest.TestCase):
    def test_one_shot_reads_only_ep1_and_returns_validated_status(self) -> None:
        session = RestrictedSession(valid_status(results=b"\xa5"))
        context = RecordingContext(session)
        factory = RecordingFactory(context)

        result = Ds2490StatusDiagnostic(factory).run()

        self.assertEqual(session.calls, [("interrupt_in", 32)])
        self.assertEqual(result.transfer_length, 17)
        self.assertEqual(result.status_flags, 0x20)
        self.assertEqual(result.result_bytes, (0xA5,))
        self.assertTrue(context.exited)
        self.assertIsNone(context.exception_type)

    def test_report_records_the_operation_restrictions(self) -> None:
        report = diagnostic_report(parse_adapter_status(valid_status()))

        self.assertEqual(report["status"], "ok")
        self.assertEqual(
            report["usb"],
            {
                "configuration": 1,
                "interface": 0,
                "endpoint": "0x81",
                "configuration_changed": False,
                "alternate_setting_changed": False,
                "control_transfers": 0,
                "bulk_transfers": 0,
                "interrupt_reads": 1,
            },
        )

    def test_malformed_status_lengths_are_rejected_and_cleaned_up(self) -> None:
        for response in (bytes(15), bytes(33)):
            with self.subTest(length=len(response)):
                session = RestrictedSession(response)
                context = RecordingContext(session)
                with self.assertRaisesRegex(Ds2490ProtocolError, "16..32"):
                    Ds2490StatusDiagnostic(RecordingFactory(context)).run()
                self.assertTrue(context.exited)
                self.assertIs(context.exception_type, Ds2490ProtocolError)

    def test_malformed_state_fields_are_rejected(self) -> None:
        cases = (
            (0, 0x80, "enable flags"),
            (1, 0x03, "speed code"),
            (8, 0x40, "reserved flag"),
            (11, 17, "command buffer"),
            (12, 129, "data-out"),
            (13, 129, "data-in"),
        )
        for offset, value, message in cases:
            with self.subTest(offset=offset):
                packet = bytearray(valid_status())
                packet[offset] = value
                with self.assertRaisesRegex(Ds2490ProtocolError, message):
                    parse_adapter_status(bytes(packet))

    def test_every_transfer_failure_exits_the_claimed_session(self) -> None:
        errors = (
            UsbPermissionError("denied"),
            UsbEndpointUnavailableError("missing EP1"),
            UsbTransferTimeout("timed out"),
            AdapterNotFoundError("disconnected"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                context = RecordingContext(RestrictedSession(error))
                with self.assertRaises(type(error)):
                    Ds2490StatusDiagnostic(RecordingFactory(context)).run()
                self.assertTrue(context.exited)
                self.assertIs(context.exception_type, type(error))

    def test_cli_requires_explicit_subcommand_before_constructing_diagnostic(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main([], diagnostic_factory=NeverConstructDiagnostic)
        self.assertEqual(raised.exception.code, 2)

    def test_cli_runs_once_and_outputs_json_for_explicit_subcommand(self) -> None:
        session = RestrictedSession(valid_status())
        context = RecordingContext(session)
        output = io.StringIO()

        class Diagnostic:
            def run(self):
                return Ds2490StatusDiagnostic(RecordingFactory(context)).run()

        code = main(
            ["adapter-status", "--adapter-rom", ADAPTER_ROM],
            diagnostic_factory=lambda _binding: Diagnostic(),
            access_factory=ImmediateAccess,
            output=output,
        )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "ok")
        self.assertEqual(session.calls, [("interrupt_in", 32)])

    def test_cli_reports_failure_without_retrying(self) -> None:
        class FailingDiagnostic:
            def run(self):
                raise UsbTransferTimeout("timed out")

        output = io.StringIO()
        error_output = io.StringIO()
        code = main(
            ["adapter-status", "--adapter-rom", ADAPTER_ROM],
            diagnostic_factory=lambda _binding: FailingDiagnostic(),
            access_factory=ImmediateAccess,
            output=output,
            error_output=error_output,
        )

        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        report = json.loads(error_output.getvalue())
        self.assertEqual(report["error_type"], "UsbTransferTimeout")


if __name__ == "__main__":
    unittest.main()
