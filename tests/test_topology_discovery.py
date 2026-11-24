from __future__ import annotations

import unittest

from eisy_onewire.ds2409_transport import (
    Ds2409Branch,
    Ds2409State,
    Ds2409StatusInfo,
)
from eisy_onewire.onewire_discovery import DiscoveryReport, OneWireDevice, dallas_crc8
from eisy_onewire.topology_discovery import (
    TopologyDiscoveryError,
    TopologyRestorationError,
    UnsupportedTopologyError,
    discover_one_level_topology,
)


ADAPTER = bytes.fromhex("814455667788999C")
COUPLER = bytes.fromhex("1F5566778899AA38")
SENSOR_A = bytes.fromhex("2811223344556656")
SENSOR_TWO = bytes.fromhex("2822334455667727")
SENSOR_THREE = bytes.fromhex("28334455667788E7")
BRANCH_SENSOR = bytes.fromhex("28778899AABBCC50")
BRANCH_DS2438 = bytes.fromhex("2666778899AABB54")


def make_rom(prefix: str) -> bytes:
    raw = bytes.fromhex(prefix)
    return raw + bytes([dallas_crc8(raw)])


def report(*roms: bytes) -> DiscoveryReport:
    return DiscoveryReport(tuple(OneWireDevice(rom) for rom in roms), ())


class SearchSequence:
    def __init__(self, *results: DiscoveryReport | BaseException) -> None:
        self.results = list(results)
        self.calls = 0

    def __call__(self) -> DiscoveryReport:
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeCoupler:
    def __init__(
        self,
        state: Ds2409State = Ds2409State.MAIN,
        *,
        fail_restore: bool = False,
        main_event: bool = False,
        aux_event: bool = False,
        main_level_high: bool = True,
        aux_level_high: bool = True,
        verified_state: Ds2409State | None = None,
    ) -> None:
        self.state = state
        self.fail_restore = fail_restore
        self.main_event = main_event
        self.aux_event = aux_event
        self.main_level_high = main_level_high
        self.aux_level_high = aux_level_high
        self.verified_state = verified_state
        self.calls: list[tuple[object, ...]] = []

    def read_status(self, rom: bytes) -> Ds2409StatusInfo:
        prior_reads = sum(call[0] == "read" for call in self.calls)
        self.calls.append(("read", rom))
        state = (
            self.verified_state
            if prior_reads and self.verified_state is not None
            else self.state
        )
        raw = {
            Ds2409State.OFF: 0x05,
            Ds2409State.MAIN: 0x04,
            Ds2409State.AUX: 0x01,
        }[state]
        raw |= 0x02 if self.main_level_high else 0
        raw |= 0x08 if self.aux_level_high else 0
        raw |= 0x10 if self.main_event else 0
        raw |= 0x20 if self.aux_event else 0
        return Ds2409StatusInfo(
            state,
            self.main_event,
            self.aux_event,
            raw,
            self.main_level_high,
            self.aux_level_high,
        )

    def all_lines_off(self, rom: bytes) -> None:
        self.calls.append(("off", rom))

    def select_branch(self, rom: bytes, branch: Ds2409Branch) -> int:
        self.calls.append(("select", rom, branch))
        return 0

    def restore_state(self, rom: bytes, state: Ds2409State) -> None:
        self.calls.append(("restore", rom, state))
        if self.fail_restore:
            raise RuntimeError("restore failed")


class TopologyDiscoveryTests(unittest.TestCase):
    def test_main_branch_devices_are_attributed_and_aux_empty(self) -> None:
        root = report(ADAPTER, COUPLER, SENSOR_A, SENSOR_TWO, SENSOR_THREE)
        initial = report(
            ADAPTER,
            COUPLER,
            SENSOR_A,
            SENSOR_TWO,
            SENSOR_THREE,
            BRANCH_SENSOR,
            BRANCH_DS2438,
        )
        search = SearchSequence(
            initial,
            root,
            report(*[item.rom for item in root.devices], BRANCH_SENSOR, BRANCH_DS2438),
            root,
        )
        control = FakeCoupler(Ds2409State.MAIN)

        found = discover_one_level_topology(search, control)

        by_rom = {item.device.rom: item for item in found.devices}
        self.assertEqual(by_rom[SENSOR_A].path, ())
        self.assertEqual(by_rom[BRANCH_SENSOR].path[0].branch, Ds2409Branch.MAIN)
        self.assertEqual(by_rom[BRANCH_DS2438].path[0].branch, Ds2409Branch.MAIN)
        self.assertEqual(found.branches_not_scanned, ())
        self.assertEqual(
            [path[0].branch for path in found.scanned_branches],
            [Ds2409Branch.MAIN, Ds2409Branch.AUX],
        )
        self.assertEqual(search.calls, 4)
        self.assertEqual(
            control.calls,
            [
                ("read", COUPLER),
                ("off", COUPLER),
                ("off", COUPLER),
                ("select", COUPLER, Ds2409Branch.MAIN),
                ("off", COUPLER),
                ("select", COUPLER, Ds2409Branch.AUX),
                ("off", COUPLER),
                ("restore", COUPLER, Ds2409State.MAIN),
                ("read", COUPLER),
            ],
        )

    def test_scan_error_restores_original_state_without_retry(self) -> None:
        root = report(ADAPTER, COUPLER)
        search = SearchSequence(root, root, TimeoutError("bounded timeout"))
        control = FakeCoupler(Ds2409State.AUX)

        with self.assertRaisesRegex(TimeoutError, "bounded timeout"):
            discover_one_level_topology(search, control)

        self.assertEqual(search.calls, 3)
        self.assertEqual(
            control.calls[-3:],
            [
                ("off", COUPLER),
                ("restore", COUPLER, Ds2409State.AUX),
                ("read", COUPLER),
            ],
        )

    def test_restoration_failure_overrides_success_and_fails_closed(self) -> None:
        root = report(ADAPTER, COUPLER)
        search = SearchSequence(root, root, root, root)
        control = FakeCoupler(Ds2409State.MAIN, fail_restore=True)

        with self.assertRaisesRegex(TopologyRestorationError, "restore failed"):
            discover_one_level_topology(search, control)

        self.assertEqual(search.calls, 4)

    def test_same_device_on_two_branches_is_ambiguous(self) -> None:
        root = report(ADAPTER, COUPLER)
        branch = report(ADAPTER, COUPLER, BRANCH_SENSOR)
        search = SearchSequence(root, root, branch, branch)
        control = FakeCoupler(Ds2409State.OFF)

        with self.assertRaisesRegex(TopologyDiscoveryError, "multiple branches"):
            discover_one_level_topology(search, control)

        self.assertEqual(control.calls[-2:], [("off", COUPLER), ("read", COUPLER)])

    def test_successful_restore_echo_with_wrong_readback_fails_closed(self) -> None:
        root = report(ADAPTER, COUPLER)
        search = SearchSequence(root, root, root, root)
        control = FakeCoupler(
            Ds2409State.MAIN, verified_state=Ds2409State.AUX
        )

        with self.assertRaisesRegex(
            TopologyRestorationError, "expected main, observed aux"
        ):
            discover_one_level_topology(search, control)

        self.assertEqual(
            control.calls[-3:],
            [
                ("off", COUPLER),
                ("restore", COUPLER, Ds2409State.MAIN),
                ("read", COUPLER),
            ],
        )

    def test_nested_coupler_is_inventoried_but_its_branches_are_not_toggled(self) -> None:
        nested = make_rom("1F010203040506")
        root = report(ADAPTER, COUPLER)
        search = SearchSequence(root, root, report(ADAPTER, COUPLER, nested), root)
        control = FakeCoupler(Ds2409State.OFF)

        found = discover_one_level_topology(search, control)

        self.assertEqual(len(found.branches_not_scanned), 2)
        self.assertEqual(
            {path[-1].branch for path in found.branches_not_scanned},
            {Ds2409Branch.MAIN, Ds2409Branch.AUX},
        )
        self.assertFalse(any(call[1] == nested for call in control.calls))

    def test_multiple_accessible_couplers_fail_before_branch_commands(self) -> None:
        second = make_rom("1F111213141516")
        search = SearchSequence(report(ADAPTER, COUPLER, second))
        control = FakeCoupler()

        with self.assertRaisesRegex(UnsupportedTopologyError, "more than one"):
            discover_one_level_topology(search, control)

        self.assertEqual(search.calls, 1)
        self.assertEqual(control.calls, [])

    def test_latched_event_flag_fails_before_destructive_all_lines_off(self) -> None:
        search = SearchSequence(report(ADAPTER, COUPLER))
        control = FakeCoupler(main_event=True)

        with self.assertRaisesRegex(TopologyDiscoveryError, "event flags are set"):
            discover_one_level_topology(search, control)

        self.assertEqual(search.calls, 1)
        self.assertEqual(control.calls, [("read", COUPLER)])

    def test_explicit_opt_in_records_flags_then_runs_one_scan(self) -> None:
        root = report(ADAPTER, COUPLER)
        search = SearchSequence(root, root, root, root)
        control = FakeCoupler(main_event=True, aux_event=True)
        captured = []

        found = discover_one_level_topology(
            search,
            control,
            acknowledge_event_flags=True,
            on_event_flags_cleared=lambda evidence: captured.append(
                (evidence, tuple(control.calls))
            ),
        )

        self.assertEqual(search.calls, 4)
        self.assertEqual(len(found.event_flags_acknowledged), 1)
        acknowledgement = found.event_flags_acknowledged[0]
        self.assertEqual(acknowledgement.coupler_rom, COUPLER)
        self.assertEqual(acknowledgement.raw_status & 0x30, 0x30)
        self.assertTrue(acknowledgement.main_event)
        self.assertTrue(acknowledgement.aux_event)
        self.assertEqual(control.calls[0], ("read", COUPLER))
        self.assertEqual(control.calls[1], ("off", COUPLER))
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], found.event_flags_acknowledged)
        self.assertEqual(
            captured[0][1], (("read", COUPLER), ("off", COUPLER))
        )

    def test_inactive_low_line_fails_before_acknowledgement_or_mutation(self) -> None:
        search = SearchSequence(report(ADAPTER, COUPLER))
        control = FakeCoupler(
            Ds2409State.OFF,
            main_event=True,
            main_level_high=False,
        )

        with self.assertRaisesRegex(TopologyDiscoveryError, "sensed low"):
            discover_one_level_topology(
                search, control, acknowledge_event_flags=True
            )

        self.assertEqual(search.calls, 1)
        self.assertEqual(control.calls, [("read", COUPLER)])


if __name__ == "__main__":
    unittest.main()
