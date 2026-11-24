"""One-shot, one-level DS2409 topology discovery.

The accepted DS2490 Search Access implementation is reused unchanged.  This
module only sequences branch state around repeated bounded searches.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .ds2409_transport import (
    BranchHop,
    Ds2409Branch,
    Ds2409State,
    Ds2409StatusInfo,
    Ds2409Transport,
)
from .ds2490_transport import (
    MAX_SEARCH_DEVICES,
    CtypesLibUsbSessionFactory,
    Ds2490Transport,
    UsbSessionFactory,
)
from .onewire_discovery import DiscoveryReport, InvalidRom, OneWireDevice, discover_devices


DS2409_FAMILY = 0x1F
DS9490_ID_FAMILY = 0x81
MAX_SUPPORTED_ROOT_COUPLERS = 1


class TopologyDiscoveryError(RuntimeError):
    """A complete and unambiguous topology result could not be produced."""


class UnsupportedTopologyError(TopologyDiscoveryError):
    """The observed hierarchy exceeds this release's reviewed scope."""


class TopologyRestorationError(TopologyDiscoveryError):
    """One or more DS2409 devices could not be returned to prior state."""


@dataclass(frozen=True, slots=True)
class LocatedDevice:
    device: OneWireDevice
    adapter_rom: bytes
    path: tuple[BranchHop, ...] = ()


@dataclass(frozen=True, slots=True)
class EventFlagAcknowledgement:
    """Pre-clear DS2409 event evidence that cannot be restored afterward."""

    coupler_rom: bytes
    raw_status: int
    main_event: bool
    aux_event: bool


@dataclass(frozen=True, slots=True)
class TopologyDiscoveryReport:
    adapter_rom: bytes
    devices: tuple[LocatedDevice, ...]
    scanned_branches: tuple[tuple[BranchHop, ...], ...]
    branches_not_scanned: tuple[tuple[BranchHop, ...], ...]
    invalid_responses: tuple[InvalidRom, ...] = ()
    event_flags_acknowledged: tuple[EventFlagAcknowledgement, ...] = ()


class _SessionSearch:
    def __init__(self, transport: Ds2490Transport) -> None:
        self._transport = transport

    def search_roms(self, max_devices: int) -> tuple[bytes, ...]:
        return self._transport.search_roms(max_devices)


class CouplerControl(Protocol):
    def read_status(self, coupler_rom: bytes) -> Ds2409StatusInfo: ...

    def select_branch(self, coupler_rom: bytes, branch: Ds2409Branch) -> int: ...

    def all_lines_off(self, coupler_rom: bytes) -> None: ...

    def restore_state(self, coupler_rom: bytes, state: Ds2409State) -> None: ...


class Ds2409TopologyAdapter:
    """Claim one adapter for baseline, branch scans, and restoration."""

    def __init__(self, factory: UsbSessionFactory | None = None) -> None:
        self._factory = factory or CtypesLibUsbSessionFactory.for_search_diagnostic()

    def discover(
        self,
        *,
        acknowledge_event_flags: bool = False,
        on_event_flags_cleared: Callable[
            [tuple[EventFlagAcknowledgement, ...]], None
        ]
        | None = None,
    ) -> TopologyDiscoveryReport:
        with self._factory.open() as session:
            search_transport = Ds2490Transport(session)
            search_transport.initialize()
            search = _SessionSearch(search_transport)
            coupler_transport = Ds2409Transport(session)
            return discover_one_level_topology(
                lambda: discover_devices(search, max_devices=MAX_SEARCH_DEVICES),
                coupler_transport,
                acknowledge_event_flags=acknowledge_event_flags,
                on_event_flags_cleared=on_event_flags_cleared,
            )


def discover_selected_adapter_topology(
    *,
    acknowledge_event_flags: bool = False,
    on_event_flags_cleared: Callable[
        [tuple[EventFlagAcknowledgement, ...]], None
    ]
    | None = None,
) -> TopologyDiscoveryReport:
    """Production factory used only by the explicit Discover Devices command."""

    return Ds2409TopologyAdapter().discover(
        acknowledge_event_flags=acknowledge_event_flags,
        on_event_flags_cleared=on_event_flags_cleared,
    )


def discover_one_level_topology(
    search: Callable[[], DiscoveryReport],
    coupler_transport: CouplerControl,
    *,
    acknowledge_event_flags: bool = False,
    on_event_flags_cleared: Callable[
        [tuple[EventFlagAcknowledgement, ...]], None
    ]
    | None = None,
) -> TopologyDiscoveryReport:
    """Run bounded trunk/main/aux scans and restore observed branch state."""

    initial = search()
    _require_valid(initial, "initial accessible-segment scan")
    adapter_rom = _single_adapter_identity(initial)
    couplers = tuple(
        device.rom
        for device in initial.devices
        if device.family_code == DS2409_FAMILY
    )
    if len(couplers) > MAX_SUPPORTED_ROOT_COUPLERS:
        raise UnsupportedTopologyError(
            "more than one accessible DS2409 makes root ancestry ambiguous; "
            "no branch command was issued"
        )
    if not couplers:
        return TopologyDiscoveryReport(
            adapter_rom,
            tuple(LocatedDevice(item, adapter_rom) for item in initial.devices),
            (),
            (),
        )

    snapshots = {coupler: coupler_transport.read_status(coupler) for coupler in couplers}
    unsafe_levels = [
        f"{coupler.hex().upper()}/{branch}"
        for coupler, snapshot in snapshots.items()
        for branch, valid, high in (
            ("main", snapshot.main_level_valid, snapshot.main_level_high),
            ("aux", snapshot.aux_level_valid, snapshot.aux_level_high),
        )
        if valid and not high
    ]
    if unsafe_levels:
        raise TopologyDiscoveryError(
            "DS2409 inactive branch sensed low; no mutating branch command was "
            "issued for " + ", ".join(unsafe_levels)
        )
    acknowledgements = tuple(
        EventFlagAcknowledgement(
            coupler,
            snapshot.raw,
            snapshot.main_event,
            snapshot.aux_event,
        )
        for coupler, snapshot in snapshots.items()
        if snapshot.main_event or snapshot.aux_event
    )
    flagged = [item.coupler_rom.hex().upper() for item in acknowledgements]
    if flagged:
        if not acknowledge_event_flags:
            raise TopologyDiscoveryError(
                "DS2409 event flags are set and All-Lines-Off would clear them; "
                "no branch command was issued for " + ", ".join(flagged)
            )
    original_states = {
        coupler: snapshot.state for coupler, snapshot in snapshots.items()
    }
    try:
        _turn_all_off(coupler_transport, couplers)
        if acknowledgements and on_event_flags_cleared is not None:
            on_event_flags_cleared(acknowledgements)
        root = search()
        _require_valid(root, "trunk-only scan")
        root_roms = {item.rom for item in root.devices}
        if adapter_rom not in root_roms or not set(couplers).issubset(root_roms):
            raise TopologyDiscoveryError(
                "trunk-only scan lost the adapter identity or root DS2409"
            )

        located = [LocatedDevice(item, adapter_rom) for item in root.devices]
        assigned = set(root_roms)
        scanned: list[tuple[BranchHop, ...]] = []

        for coupler in couplers:
            for branch in (Ds2409Branch.MAIN, Ds2409Branch.AUX):
                _turn_all_off(coupler_transport, couplers)
                coupler_transport.select_branch(coupler, branch)
                branch_report = search()
                path = (BranchHop(coupler, branch),)
                _require_valid(branch_report, f"{path[0].label} scan")
                branch_roms = {item.rom for item in branch_report.devices}
                if not root_roms.issubset(branch_roms):
                    raise TopologyDiscoveryError(
                        f"{path[0].label} scan lost a trunk device"
                    )
                for item in branch_report.devices:
                    if item.rom in root_roms:
                        continue
                    if item.rom in assigned:
                        raise TopologyDiscoveryError(
                            f"ROM {item.rom_address} appeared on multiple branches"
                        )
                    assigned.add(item.rom)
                    located.append(LocatedDevice(item, adapter_rom, path))
                scanned.append(path)

        unscanned: list[tuple[BranchHop, ...]] = []
        for item in located:
            if item.device.family_code != DS2409_FAMILY or not item.path:
                continue
            for branch in (Ds2409Branch.MAIN, Ds2409Branch.AUX):
                unscanned.append(item.path + (BranchHop(item.device.rom, branch),))

        return TopologyDiscoveryReport(
            adapter_rom=adapter_rom,
            devices=tuple(sorted(located, key=lambda item: item.device.rom)),
            scanned_branches=tuple(scanned),
            branches_not_scanned=tuple(unscanned),
            event_flags_acknowledged=acknowledgements,
        )
    finally:
        _restore_states(coupler_transport, original_states)


def _single_adapter_identity(report: DiscoveryReport) -> bytes:
    identities = [
        item.rom for item in report.devices if item.family_code == DS9490_ID_FAMILY
    ]
    if len(identities) != 1:
        raise TopologyDiscoveryError(
            "topology discovery requires exactly one family-0x81 adapter identity"
        )
    return identities[0]


def _require_valid(report: DiscoveryReport, operation: str) -> None:
    if report.invalid_responses:
        reasons = "; ".join(item.reason for item in report.invalid_responses)
        raise TopologyDiscoveryError(f"{operation} returned invalid ROM data: {reasons}")


def _turn_all_off(transport: CouplerControl, couplers: tuple[bytes, ...]) -> None:
    for coupler in couplers:
        transport.all_lines_off(coupler)


def _restore_states(
    transport: CouplerControl, states: dict[bytes, Ds2409State]
) -> None:
    failures: list[str] = []
    for coupler in states:
        try:
            transport.all_lines_off(coupler)
        except Exception as exc:  # Continue independent cleanup; never retry one command.
            failures.append(f"{coupler.hex().upper()} off: {exc}")
    for coupler, state in states.items():
        if state is Ds2409State.OFF:
            continue
        try:
            transport.restore_state(coupler, state)
        except Exception as exc:  # Continue independent cleanup; never retry one command.
            failures.append(f"{coupler.hex().upper()} {state.value}: {exc}")
    for coupler, expected in states.items():
        try:
            actual = transport.read_status(coupler).state
            if actual is not expected:
                failures.append(
                    f"{coupler.hex().upper()} state verification: "
                    f"expected {expected.value}, observed {actual.value}"
                )
        except Exception as exc:
            failures.append(f"{coupler.hex().upper()} state verification: {exc}")
    if failures:
        raise TopologyRestorationError(
            "DS2409 restoration failed: " + "; ".join(failures)
        )
