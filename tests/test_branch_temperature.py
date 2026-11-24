from __future__ import annotations

import unittest
from unittest.mock import patch

from eisy_onewire.branch_temperature import (
    BranchEventPolicyError,
    BranchRestorationError,
    BranchTemperatureAdapter,
    BranchTemperatureError,
    BranchTemperatureTarget,
    read_branch_temperature,
)
from eisy_onewire.ds18b20 import PowerMode
from eisy_onewire.ds2409_transport import (
    BranchHop,
    Ds2409Branch,
    Ds2409State,
    Ds2409StatusInfo,
)
from eisy_onewire.temperature_service import FreshTemperature


SENSOR = bytes.fromhex("28778899AABBCC50")
ADAPTER = bytes.fromhex("814455667788999C")
COUPLER = bytes.fromhex("1F5566778899AA38")
TARGET = BranchTemperatureTarget(
    SENSOR, ADAPTER, (BranchHop(COUPLER, Ds2409Branch.MAIN),)
)


def status(
    state: Ds2409State,
    *,
    raw: int = 0x0F,
    main_event: bool = False,
    aux_event: bool = False,
) -> Ds2409StatusInfo:
    return Ds2409StatusInfo(state, main_event, aux_event, raw)


MEASUREMENT = FreshTemperature(
    SENSOR,
    PowerMode.EXTERNAL,
    12,
    bytes.fromhex("A2004B467FFF0C10BA"),
    10.125,
    50.225,
    0.75,
    2,
)


class FakeCoupler:
    def __init__(self, statuses: list[Ds2409StatusInfo]) -> None:
        self.statuses = list(statuses)
        self.calls: list[tuple[object, ...]] = []
        self.fail_restore = False
        self.fail_select = False

    def read_status(self, rom: bytes) -> Ds2409StatusInfo:
        self.calls.append(("status", rom))
        value = self.statuses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def select_branch(self, rom: bytes, branch: Ds2409Branch) -> int:
        self.calls.append(("select", rom, branch))
        if self.fail_select:
            raise RuntimeError("selection failed")
        return 0

    def all_lines_off(self, rom: bytes) -> None:
        self.calls.append(("off", rom))
        if self.fail_restore:
            raise RuntimeError("off failed")

    def restore_state(self, rom: bytes, state: Ds2409State) -> None:
        self.calls.append(("restore", rom, state))
        if self.fail_restore:
            raise RuntimeError("restore failed")


class FakeTemperature:
    def __init__(self, result: object = MEASUREMENT) -> None:
        self.result = result
        self.calls: list[bytes] = []

    def read(self, rom: bytes) -> FreshTemperature:
        self.calls.append(rom)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result  # type: ignore[return-value]


class BranchTemperatureTests(unittest.TestCase):
    def test_off_state_requires_distinct_event_clear_opt_in_before_selection(self) -> None:
        coupler = FakeCoupler([status(Ds2409State.OFF, raw=0x3F, main_event=True)])
        reader = FakeTemperature()

        with self.assertRaises(BranchEventPolicyError) as caught:
            read_branch_temperature(TARGET, coupler, reader)

        self.assertEqual(reader.calls, [])
        self.assertEqual([call[0] for call in coupler.calls], ["status"])
        self.assertTrue(caught.exception.event_evidence[0].main_event)

    def test_opt_in_reads_once_then_restores_off_and_records_irreversible_flags(self) -> None:
        coupler = FakeCoupler(
            [
                status(Ds2409State.OFF, raw=0x3F, main_event=True),
                status(Ds2409State.MAIN, raw=0x3E, main_event=True),
                status(Ds2409State.MAIN, raw=0x3E, main_event=True),
                status(Ds2409State.OFF, raw=0x0F),
            ]
        )
        reader = FakeTemperature()

        result = read_branch_temperature(
            TARGET, coupler, reader, allow_event_clear=True
        )

        self.assertIs(result.measurement, MEASUREMENT)
        self.assertTrue(result.event_clear_authorized)
        self.assertTrue(result.event_flags_cleared)
        self.assertEqual(reader.calls, [SENSOR])
        self.assertEqual(
            [call[0] for call in coupler.calls],
            ["status", "select", "status", "status", "off", "status"],
        )

    def test_already_selected_branch_preserves_flags_without_control_command(self) -> None:
        coupler = FakeCoupler(
            [
                status(Ds2409State.MAIN, raw=0x1E, main_event=True),
                status(Ds2409State.MAIN, raw=0x1E, main_event=True),
                status(Ds2409State.MAIN, raw=0x1E, main_event=True),
            ]
        )

        result = read_branch_temperature(TARGET, coupler, FakeTemperature())

        self.assertFalse(result.event_flags_cleared)
        self.assertEqual([call[0] for call in coupler.calls], ["status"] * 3)

        lost = FakeCoupler(
            [
                status(Ds2409State.MAIN, raw=0x1E, main_event=True),
                status(Ds2409State.MAIN, raw=0x1E, main_event=True),
                status(Ds2409State.MAIN, raw=0x0E, main_event=False),
            ]
        )
        with self.assertRaises(BranchRestorationError):
            read_branch_temperature(TARGET, lost, FakeTemperature())

    def test_temperature_failure_restores_previous_aux_state_before_failing(self) -> None:
        coupler = FakeCoupler(
            [
                status(Ds2409State.AUX),
                status(Ds2409State.MAIN),
                status(Ds2409State.MAIN),
                status(Ds2409State.AUX),
            ]
        )

        with self.assertRaises(BranchTemperatureError) as caught:
            read_branch_temperature(
                TARGET, coupler, FakeTemperature(RuntimeError("CRC failed"))
            )

        self.assertTrue(caught.exception.restoration_verified)
        self.assertIn(("restore", COUPLER, Ds2409State.AUX), coupler.calls)

        selection = FakeCoupler(
            [
                status(Ds2409State.AUX),
                status(Ds2409State.AUX),
                status(Ds2409State.AUX),
            ]
        )
        selection.fail_select = True
        reader = FakeTemperature()
        with self.assertRaises(BranchTemperatureError) as selection_error:
            read_branch_temperature(TARGET, selection, reader)
        self.assertTrue(selection_error.exception.restoration_verified)
        self.assertEqual(reader.calls, [])

    def test_restore_failure_or_wrong_readback_overrides_measurement(self) -> None:
        failing = FakeCoupler(
            [status(Ds2409State.AUX), status(Ds2409State.MAIN), status(Ds2409State.MAIN)]
        )
        failing.fail_restore = True
        with self.assertRaises(BranchRestorationError):
            read_branch_temperature(TARGET, failing, FakeTemperature())

        wrong = FakeCoupler(
            [
                status(Ds2409State.AUX),
                status(Ds2409State.MAIN),
                status(Ds2409State.MAIN),
                status(Ds2409State.MAIN),
            ]
        )
        with self.assertRaises(BranchRestorationError):
            read_branch_temperature(TARGET, wrong, FakeTemperature())

    def test_unknown_latch_state_before_all_lines_off_fails_after_verified_restore(self) -> None:
        coupler = FakeCoupler(
            [
                status(Ds2409State.OFF),
                status(Ds2409State.MAIN),
                RuntimeError("status read failed"),
                status(Ds2409State.OFF),
            ]
        )

        with self.assertRaises(BranchTemperatureError) as caught:
            read_branch_temperature(
                TARGET, coupler, FakeTemperature(), allow_event_clear=True
            )

        self.assertTrue(caught.exception.restoration_verified)
        self.assertTrue(caught.exception.event_state_unknown_before_clear)
        self.assertIn(("off", COUPLER), coupler.calls)

    def test_inactive_branch_low_fails_without_mutation_or_temperature(self) -> None:
        unsafe = Ds2409StatusInfo(
            Ds2409State.OFF, False, False, 0x05, main_level_high=False
        )
        coupler = FakeCoupler([unsafe])
        reader = FakeTemperature()

        with self.assertRaises(BranchTemperatureError):
            read_branch_temperature(TARGET, coupler, reader, allow_event_clear=True)

        self.assertEqual(reader.calls, [])
        self.assertEqual([call[0] for call in coupler.calls], ["status"])

    def test_owned_usb_session_exits_after_branch_failure(self) -> None:
        class Context:
            def __init__(self) -> None:
                self.session = object()
                self.exited = False

            def __enter__(self):
                return self.session

            def __exit__(self, *args):
                self.exited = True

        context = Context()

        class Factory:
            def open(self):
                return context

        coupler = FakeCoupler(
            [
                status(Ds2409State.MAIN),
                status(Ds2409State.MAIN),
                status(Ds2409State.MAIN),
            ]
        )
        reader = FakeTemperature(RuntimeError("missing sensor"))
        with (
            patch("eisy_onewire.branch_temperature.Ds2490Transport") as transport,
            patch(
                "eisy_onewire.branch_temperature.Ds2409Transport",
                return_value=coupler,
            ),
            patch(
                "eisy_onewire.branch_temperature.TemperatureService",
                return_value=reader,
            ),
        ):
            with self.assertRaises(BranchTemperatureError):
                BranchTemperatureAdapter(Factory()).read(TARGET)

        transport.return_value.initialize.assert_called_once_with()
        self.assertTrue(context.exited)


if __name__ == "__main__":
    unittest.main()
