"""One-shot DS18B20 read through one validated DS2409 branch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .ds18b20 import parse_ds18b20_rom
from .ds18b20_transport import Ds18b20SessionAdapter
from .ds2409_transport import (
    BranchHop,
    Ds2409Branch,
    Ds2409State,
    Ds2409StatusInfo,
    Ds2409Transport,
)
from .ds2490_transport import (
    CtypesLibUsbSessionFactory,
    Ds2490Transport,
    UsbSessionFactory,
)
from .onewire_discovery import dallas_crc8
from .temperature_service import FreshTemperature, TemperatureService


DS9490_ID_FAMILY = 0x81


class BranchTemperatureError(RuntimeError):
    """A branch read could not be accepted as a fresh measurement."""

    def __init__(
        self,
        message: str,
        *,
        restoration_verified: bool = False,
        event_evidence: tuple["EventLatchEvidence", ...] = (),
        event_state_unknown_before_clear: bool = False,
    ) -> None:
        super().__init__(message)
        self.restoration_verified = restoration_verified
        self.event_evidence = event_evidence
        self.event_state_unknown_before_clear = event_state_unknown_before_clear


class BranchEventPolicyError(BranchTemperatureError):
    """The requested read could require an unapproved event-latch clear."""


class BranchRestorationError(BranchTemperatureError):
    """The original DS2409 branch state could not be verified after a read."""


@dataclass(frozen=True, slots=True)
class BranchTemperatureTarget:
    rom: bytes
    adapter_rom: bytes
    path: tuple[BranchHop, ...]

    def __post_init__(self) -> None:
        parsed = parse_ds18b20_rom(self.rom.hex())
        if parsed != self.rom:
            raise ValueError("temperature target ROM is not canonical")
        if (
            len(self.adapter_rom) != 8
            or self.adapter_rom[0] != DS9490_ID_FAMILY
            or dallas_crc8(self.adapter_rom) != 0
        ):
            raise ValueError("branch target adapter must be a CRC-valid family-0x81 ROM")
        if len(self.path) != 1 or not isinstance(self.path[0], BranchHop):
            raise ValueError("branch temperature supports exactly one validated DS2409 hop")


@dataclass(frozen=True, slots=True)
class EventLatchEvidence:
    stage: str
    raw_status: int
    main_event: bool
    aux_event: bool


@dataclass(frozen=True, slots=True)
class BranchTemperatureResult:
    measurement: FreshTemperature
    target: BranchTemperatureTarget
    original_state: Ds2409State
    restored_state: Ds2409State
    event_evidence: tuple[EventLatchEvidence, ...]
    event_clear_authorized: bool
    event_flags_cleared: bool


class CouplerControl(Protocol):
    def read_status(self, coupler_rom: bytes) -> Ds2409StatusInfo: ...

    def select_branch(self, coupler_rom: bytes, branch: Ds2409Branch) -> int: ...

    def all_lines_off(self, coupler_rom: bytes) -> None: ...

    def restore_state(self, coupler_rom: bytes, state: Ds2409State) -> None: ...


class FreshTemperatureReader(Protocol):
    def read(self, rom: bytes) -> FreshTemperature: ...


class BranchTemperatureAdapter:
    """Own one claimed DS2490 session for the complete branch transaction."""

    def __init__(self, factory: UsbSessionFactory | None = None) -> None:
        self._factory = factory or CtypesLibUsbSessionFactory.for_search_diagnostic()

    def read(
        self,
        target: BranchTemperatureTarget,
        *,
        allow_event_clear: bool = False,
    ) -> BranchTemperatureResult:
        with self._factory.open() as session:
            Ds2490Transport(session).initialize()
            coupler = Ds2409Transport(session)
            temperature = TemperatureService(
                adapter_factory=lambda: Ds18b20SessionAdapter(session)
            )
            return read_branch_temperature(
                target,
                coupler,
                temperature,
                allow_event_clear=allow_event_clear,
            )


def read_branch_temperature(
    target: BranchTemperatureTarget,
    coupler: CouplerControl,
    temperature: FreshTemperatureReader,
    *,
    allow_event_clear: bool = False,
) -> BranchTemperatureResult:
    """Select, read, restore, and verify one branch without retrying."""

    hop = target.path[0]
    original = coupler.read_status(hop.coupler_rom)
    _reject_inactive_low(original)
    evidence = [_evidence("original", original)]
    if original.state is Ds2409State.OFF and not allow_event_clear:
        raise BranchEventPolicyError(
            "restoring an originally-off DS2409 requires All-Lines-Off, which "
            "can clear event latches; branch-read acknowledgement is disabled",
            event_evidence=tuple(evidence),
        )

    operation_error: Exception | None = None
    measurement: FreshTemperature | None = None
    selected = original.state.value == hop.branch.value
    try:
        if not selected:
            coupler.select_branch(hop.coupler_rom, hop.branch)
            selected_status = coupler.read_status(hop.coupler_rom)
            evidence.append(_evidence("selected", selected_status))
            if selected_status.state.value != hop.branch.value:
                raise BranchTemperatureError(
                    "DS2409 readback did not confirm the target branch"
                )
        measurement = temperature.read(target.rom)
    except Exception as exc:
        operation_error = exc

    before_restore: Ds2409StatusInfo | None = None
    event_state_unknown = False
    try:
        before_restore = coupler.read_status(hop.coupler_rom)
        evidence.append(_evidence("before_restore", before_restore))
    except Exception:
        event_state_unknown = True

    restoration_error: Exception | None = None
    try:
        if original.state is Ds2409State.OFF:
            coupler.all_lines_off(hop.coupler_rom)
        elif original.state.value != hop.branch.value:
            coupler.restore_state(hop.coupler_rom, original.state)
        final = coupler.read_status(hop.coupler_rom)
        evidence.append(_evidence("restored", final))
        if final.state is not original.state:
            raise BranchRestorationError(
                "DS2409 restoration readback did not match the original state"
            )
        if original.state is not Ds2409State.OFF and (
            (original.main_event and not final.main_event)
            or (original.aux_event and not final.aux_event)
        ):
            raise BranchRestorationError(
                "DS2409 event latch was unexpectedly cleared during branch restoration"
            )
    except Exception as exc:
        restoration_error = exc

    if restoration_error is not None:
        raise BranchRestorationError(
            f"DS2409 restoration failed: {restoration_error}",
            event_evidence=tuple(evidence),
            event_state_unknown_before_clear=event_state_unknown,
        ) from restoration_error
    if event_state_unknown and original.state is Ds2409State.OFF:
        raise BranchTemperatureError(
            "DS2409 event-latch state could not be captured before "
            "All-Lines-Off restoration",
            restoration_verified=True,
            event_evidence=tuple(evidence),
            event_state_unknown_before_clear=True,
        )
    if operation_error is not None:
        if isinstance(operation_error, BranchTemperatureError):
            operation_error.restoration_verified = True
            operation_error.event_evidence = tuple(evidence)
            operation_error.event_state_unknown_before_clear = event_state_unknown
            raise operation_error
        raise BranchTemperatureError(
            str(operation_error),
            restoration_verified=True,
            event_evidence=tuple(evidence),
            event_state_unknown_before_clear=event_state_unknown,
        ) from operation_error
    assert measurement is not None
    flags_cleared = original.state is Ds2409State.OFF and any(
        item.main_event or item.aux_event for item in evidence[:-1]
    )
    return BranchTemperatureResult(
        measurement=measurement,
        target=target,
        original_state=original.state,
        restored_state=final.state,
        event_evidence=tuple(evidence),
        event_clear_authorized=(
            allow_event_clear and original.state is Ds2409State.OFF
        ),
        event_flags_cleared=flags_cleared,
    )


def _reject_inactive_low(status: Ds2409StatusInfo) -> None:
    low = []
    if status.main_level_valid and not status.main_level_high:
        low.append("main")
    if status.aux_level_valid and not status.aux_level_high:
        low.append("aux")
    if low:
        raise BranchTemperatureError(
            "DS2409 inactive branch sensed low: " + ", ".join(low)
        )


def _evidence(stage: str, status: Ds2409StatusInfo) -> EventLatchEvidence:
    return EventLatchEvidence(
        stage,
        status.raw,
        status.main_event,
        status.aux_event,
    )
