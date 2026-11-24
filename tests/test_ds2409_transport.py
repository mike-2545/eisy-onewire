from __future__ import annotations

import unittest

from eisy_onewire.ds2409_transport import (
    ALL_LINES_OFF,
    MATCH_ROM,
    READ_SLOT,
    SMART_ON_AUX,
    SMART_ON_MAIN,
    STATUS_READ_WRITE,
    Ds2409Branch,
    Ds2409ControlMode,
    Ds2409ProtocolError,
    Ds2409State,
    Ds2409Transport,
)
from eisy_onewire.ds2490_transport import (
    FLEXIBLE_DATA_SAMPLE_OFFSET,
    FLEXIBLE_SLEW_RATE,
    FLEXIBLE_WRITE1_LOW_TIME,
    ONEWIRE_SPEED_FLEXIBLE,
)


COUPLER = bytes.fromhex("1F5566778899AA38")


def status(*, data_in: int = 0, flags: int = 0x20) -> bytes:
    packet = bytearray(16)
    packet[1] = ONEWIRE_SPEED_FLEXIBLE
    packet[4] = FLEXIBLE_SLEW_RATE
    packet[5] = FLEXIBLE_WRITE1_LOW_TIME
    packet[6] = FLEXIBLE_DATA_SAMPLE_OFFSET
    packet[8] = flags
    packet[13] = data_in
    return bytes(packet)


class FakeSession:
    def __init__(self) -> None:
        self.controls: list[tuple[int, int, int]] = []
        self.bulk_writes: list[bytes] = []
        self.bulk_reads: list[bytes] = []
        self.status_reads: list[bytes] = []

    def control(self, request: int, value: int, index: int) -> None:
        self.controls.append((request, value, index))

    def bulk_out(self, payload: bytes) -> None:
        self.bulk_writes.append(payload)

    def bulk_in(self, length: int) -> bytes:
        return self.bulk_reads.pop(0)

    def interrupt_in(self, length: int) -> bytes:
        self.assertEqual(length, 32)
        return self.status_reads.pop(0)

    @staticmethod
    def assertEqual(left, right) -> None:
        if left != right:
            raise AssertionError(f"{left!r} != {right!r}")


class Ds2409TransportTests(unittest.TestCase):
    def test_read_state_is_nonmutating_and_decodes_active_low_bits(self) -> None:
        for info, expected in (
            (0x05, Ds2409State.OFF),
            (0x04, Ds2409State.MAIN),
            (0x01, Ds2409State.AUX),
        ):
            with self.subTest(expected=expected):
                session = FakeSession()
                session.status_reads = [status(), status(data_in=13)]
                session.bulk_reads = [bytes(11) + bytes([info, info])]

                actual = Ds2409Transport(session).read_state(COUPLER)

                self.assertIs(actual, expected)
                self.assertEqual(
                    session.bulk_writes,
                    [
                        bytes([MATCH_ROM])
                        + COUPLER
                        + bytes(
                            [
                                STATUS_READ_WRITE,
                                READ_SLOT,
                                READ_SLOT,
                                READ_SLOT,
                            ]
                        )
                    ],
                )

    def test_smart_on_main_and_aux_verify_confirmation(self) -> None:
        for branch, command in (
            (Ds2409Branch.MAIN, SMART_ON_MAIN),
            (Ds2409Branch.AUX, SMART_ON_AUX),
        ):
            with self.subTest(branch=branch):
                session = FakeSession()
                session.status_reads = [status(), status(data_in=13)]
                session.bulk_reads = [bytes(11) + bytes([0x00, command])]

                reset_response = Ds2409Transport(session).select_branch(
                    COUPLER, branch
                )

                self.assertEqual(reset_response, 0x00)
                self.assertEqual(
                    session.bulk_writes,
                    [
                        bytes([MATCH_ROM])
                        + COUPLER
                        + bytes([command, READ_SLOT, READ_SLOT, READ_SLOT])
                    ],
                )

    def test_read_status_reports_nonrestorable_event_flags(self) -> None:
        session = FakeSession()
        session.status_reads = [status(), status(data_in=13)]
        session.bulk_reads = [bytes(11) + b"\x7F\x7F"]

        actual = Ds2409Transport(session).read_status(COUPLER)

        self.assertIs(actual.state, Ds2409State.OFF)
        self.assertTrue(actual.main_event)
        self.assertTrue(actual.aux_event)
        self.assertEqual(actual.raw, 0x7F)
        self.assertTrue(actual.main_level_high)
        self.assertTrue(actual.aux_level_high)
        self.assertTrue(actual.main_level_valid)
        self.assertTrue(actual.aux_level_valid)
        self.assertIs(actual.control_mode, Ds2409ControlMode.AUTO)
        self.assertIs(actual.control_association, Ds2409Branch.AUX)
        self.assertIsNone(actual.control_output_on)
        self.assertEqual(
            session.bulk_writes[0][-4:],
            bytes([STATUS_READ_WRITE, READ_SLOT, READ_SLOT, READ_SLOT]),
        )

    def test_manual_control_status_is_decoded_without_writing_configuration(self) -> None:
        session = FakeSession()
        session.status_reads = [status(), status(data_in=13)]
        session.bulk_reads = [bytes(11) + b"\xC5\xC5"]

        actual = Ds2409Transport(session).read_status(COUPLER)

        self.assertIs(actual.control_mode, Ds2409ControlMode.MANUAL)
        self.assertIsNone(actual.control_association)
        self.assertTrue(actual.control_output_on)

    def test_status_mismatch_and_short_read_fail_before_branch_control(self) -> None:
        mismatch = FakeSession()
        mismatch.status_reads = [status(), status(data_in=13)]
        mismatch.bulk_reads = [bytes(11) + b"\x05\x04"]
        with self.assertRaisesRegex(Ds2409ProtocolError, "status confirmation mismatch"):
            Ds2409Transport(mismatch).read_status(COUPLER)
        self.assertEqual(len(mismatch.bulk_writes), 1)

        short = FakeSession()
        short.status_reads = [status(), status(data_in=13)]
        short.bulk_reads = [bytes(12)]
        with self.assertRaisesRegex(Ds2409ProtocolError, "returned 12"):
            Ds2409Transport(short).read_status(COUPLER)
        self.assertEqual(len(short.bulk_writes), 1)

    def test_all_lines_off_is_exact_and_confirmed(self) -> None:
        session = FakeSession()
        session.status_reads = [status(), status(data_in=11)]
        session.bulk_reads = [bytes(10) + bytes([ALL_LINES_OFF])]

        Ds2409Transport(session).all_lines_off(COUPLER)

        self.assertEqual(
            session.bulk_writes,
            [bytes([MATCH_ROM]) + COUPLER + bytes([ALL_LINES_OFF, READ_SLOT])],
        )

    def test_confirmation_and_short_read_fail_closed_without_retry(self) -> None:
        mismatch = FakeSession()
        mismatch.status_reads = [status(), status(data_in=13)]
        mismatch.bulk_reads = [bytes(11) + bytes([0x00, 0xFF])]
        with self.assertRaisesRegex(Ds2409ProtocolError, "confirmation mismatch"):
            Ds2409Transport(mismatch).select_branch(COUPLER, Ds2409Branch.MAIN)
        self.assertEqual(len(mismatch.bulk_writes), 1)

        short = FakeSession()
        short.status_reads = [status(), status(data_in=11)]
        short.bulk_reads = [bytes(10)]
        with self.assertRaisesRegex(Ds2409ProtocolError, "returned 10"):
            Ds2409Transport(short).all_lines_off(COUPLER)
        self.assertEqual(len(short.bulk_writes), 1)

    def test_invalid_or_contradictory_state_never_selects_a_branch(self) -> None:
        session = FakeSession()
        with self.assertRaisesRegex(ValueError, "family-0x1F"):
            Ds2409Transport(session).select_branch(
                bytes.fromhex("2811223344556656"), Ds2409Branch.MAIN
            )
        self.assertEqual(session.controls, [])

        session.status_reads = [status(), status(data_in=13)]
        session.bulk_reads = [bytes(11) + b"\x00\x00"]
        with self.assertRaisesRegex(Ds2409ProtocolError, "simultaneously"):
            Ds2409Transport(session).read_state(COUPLER)


if __name__ == "__main__":
    unittest.main()
