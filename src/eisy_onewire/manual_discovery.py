"""Bounded manual discovery using the accepted DS2490 Search ROM path."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone

from .ds2490_transport import MAX_SEARCH_DEVICES, Ds2490Adapter
from .onewire_discovery import DiscoveryReport, discover_devices
from .topology_discovery import (
    EventFlagAcknowledgement,
    TopologyDiscoveryReport,
    discover_selected_adapter_topology,
)


class AdapterBusyError(RuntimeError):
    """A manual operation already owns the selected adapter."""


def search_accessible_segment() -> DiscoveryReport:
    """Run exactly one accepted, bounded Search Access transaction."""

    return discover_devices(Ds2490Adapter(), max_devices=MAX_SEARCH_DEVICES)


class ManualDiscoveryService:
    """Serialize one explicit scan with temperature operations on the adapter."""

    def __init__(
        self,
        adapter_lock: threading.Lock,
        *,
        search: Callable[..., DiscoveryReport | TopologyDiscoveryReport] = (
            discover_selected_adapter_topology
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._adapter_lock = adapter_lock
        self._search = search
        self._clock = clock

    def run_once(
        self,
        *,
        acknowledge_event_flags: bool = False,
        on_event_flags_cleared: Callable[
            [tuple[EventFlagAcknowledgement, ...]], None
        ]
        | None = None,
        commit: Callable[
            [DiscoveryReport | TopologyDiscoveryReport, str], None
        ]
        | None = None,
    ) -> tuple[DiscoveryReport | TopologyDiscoveryReport, str]:
        if not self._adapter_lock.acquire(blocking=False):
            raise AdapterBusyError("1-Wire adapter is busy with another manual operation")
        try:
            report = self._search(
                acknowledge_event_flags=acknowledge_event_flags,
                on_event_flags_cleared=on_event_flags_cleared,
            )
            observed = self._clock()
            if observed.tzinfo is None:
                raise ValueError("discovery clock must return a timezone-aware timestamp")
            timestamp = observed.astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
            if commit is not None:
                commit(report, timestamp)
            return report, timestamp
        finally:
            self._adapter_lock.release()
