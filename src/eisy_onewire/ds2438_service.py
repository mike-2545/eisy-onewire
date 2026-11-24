"""One-shot DS2438/HIH-4000 acquisition using the verified transport path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .ds2409_transport import Ds2409Transport
from .ds2438_diagnostic import (
    Ds2438BranchTarget,
    Ds2438DiagnosticResult,
    read_branch_ds2438,
)
from .ds2438_humidity import Hih4000Reading, reading_from_measurement
from .ds2438_transport import Ds2438Transport
from .ds2490_transport import CtypesLibUsbSessionFactory, Ds2490Transport, UsbSessionFactory


@dataclass(frozen=True, slots=True)
class Ds2438Acquisition:
    reading: Hih4000Reading
    branch_result: Ds2438DiagnosticResult | None = None


class Ds2438Reader(Protocol):
    def read(self, rom: bytes) -> Ds2438Acquisition: ...


class Ds2438BranchReader(Protocol):
    def read(
        self,
        target: Ds2438BranchTarget,
        *,
        allow_event_clear: bool = False,
    ) -> Ds2438Acquisition: ...


class RootDs2438Adapter:
    """Read one validated root-bus DS2438 without discovery or retries."""

    def __init__(self, factory: UsbSessionFactory | None = None) -> None:
        self._factory = factory or CtypesLibUsbSessionFactory.for_search_diagnostic()

    def read(self, rom: bytes) -> Ds2438Acquisition:
        with self._factory.open() as session:
            Ds2490Transport(session).initialize()
            measurement = Ds2438Transport(session).read_measurements(rom)
            return Ds2438Acquisition(reading_from_measurement(measurement))


class BranchDs2438Adapter:
    """Read one DS2438 through one validated DS2409 hop and restore it."""

    def __init__(self, factory: UsbSessionFactory | None = None) -> None:
        self._factory = factory or CtypesLibUsbSessionFactory.for_search_diagnostic()

    def read(
        self,
        target: Ds2438BranchTarget,
        *,
        allow_event_clear: bool = False,
    ) -> Ds2438Acquisition:
        with self._factory.open() as session:
            Ds2490Transport(session).initialize()
            result = read_branch_ds2438(
                target,
                Ds2409Transport(session),
                Ds2438Transport(session),
                allow_event_clear=allow_event_clear,
            )
            return Ds2438Acquisition(
                reading_from_measurement(result.measurement),
                branch_result=result,
            )
