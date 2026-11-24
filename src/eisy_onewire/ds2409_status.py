"""Explicit, non-mutating DS2409 status diagnostic.

The diagnostic is invoked only by the controller command.  It shares the
adapter lock with discovery and temperature operations and issues one verified
Status Read/Write transaction with the read-only 0xFF control byte.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from .ds2409_transport import Ds2409StatusInfo, Ds2409Transport
from .ds2490_transport import CtypesLibUsbSessionFactory, Ds2490Transport, UsbSessionFactory
from .manual_discovery import AdapterBusyError


@dataclass(frozen=True, slots=True)
class Ds2409StatusReport:
    adapter_rom: bytes
    coupler_rom: bytes
    observed_at: str
    status: Ds2409StatusInfo

    def to_notice(self) -> dict[str, object]:
        status = self.status
        return {
            "status": "ok",
            "adapter_rom": self.adapter_rom.hex().upper(),
            "coupler_rom": self.coupler_rom.hex().upper(),
            "observed_at": self.observed_at,
            "raw_status": f"0x{status.raw:02X}",
            "branch_state": status.state.value,
            "main": {
                "active": status.state.value == "main",
                "event_flag": status.main_event,
                "sensed_level": "high" if status.main_level_high else "low",
                "sensed_level_valid": status.main_level_valid,
            },
            "aux": {
                "active": status.state.value == "aux",
                "event_flag": status.aux_event,
                "sensed_level": "high" if status.aux_level_high else "low",
                "sensed_level_valid": status.aux_level_valid,
            },
            "control": {
                "mode": status.control_mode.value,
                "association": (
                    status.control_association.value
                    if status.control_association is not None
                    else None
                ),
                "output_on": status.control_output_on,
            },
            "event_flags_preserved": True,
            "mutating_ds2409_commands": 0,
            "automatic_retries": 0,
        }


class Ds2409StatusAdapter:
    """Open one bounded native-libusb session and read one coupler status."""

    def __init__(self, factory: UsbSessionFactory | None = None) -> None:
        self._factory = factory or CtypesLibUsbSessionFactory.for_search_diagnostic()

    def read(self, coupler_rom: bytes) -> Ds2409StatusInfo:
        with self._factory.open() as session:
            Ds2490Transport(session).initialize()
            return Ds2409Transport(session).read_status(coupler_rom)


def read_selected_adapter_ds2409_status(coupler_rom: bytes) -> Ds2409StatusInfo:
    return Ds2409StatusAdapter().read(coupler_rom)


class ManualDs2409StatusService:
    """Serialize one explicit status read with every other adapter operation."""

    def __init__(
        self,
        adapter_lock: threading.Lock,
        *,
        read: Callable[[bytes], Ds2409StatusInfo] = read_selected_adapter_ds2409_status,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._adapter_lock = adapter_lock
        self._read = read
        self._clock = clock

    def run_once(self, adapter_rom: bytes, coupler_rom: bytes) -> Ds2409StatusReport:
        if not self._adapter_lock.acquire(blocking=False):
            raise AdapterBusyError("1-Wire adapter is busy with another manual operation")
        try:
            status = self._read(coupler_rom)
            observed = self._clock()
            if observed.tzinfo is None:
                raise ValueError("status clock must return a timezone-aware timestamp")
            timestamp = observed.astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
            return Ds2409StatusReport(adapter_rom, coupler_rom, timestamp, status)
        finally:
            self._adapter_lock.release()
