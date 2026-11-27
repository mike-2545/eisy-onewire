"""Shared identity-bound access for every packaged hardware diagnostic."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, TypeVar

from .adapter_binding import AdapterIdentityBinding
from .adapter_exclusion import AdapterAccessLock
from .discovery_inventory import DS9490_ID_FAMILY, DiscoveryInventory
from .inventory_evidence import installed_evidence_path, load_inventory_evidence
from .onewire_discovery import dallas_crc8
from .ds2490_transport import UsbSessionFactory


class DiagnosticAccessError(RuntimeError):
    """A standalone diagnostic could not prove exclusive adapter identity."""


class DiagnosticAdapterBusyError(DiagnosticAccessError):
    """The running plugin or another diagnostic owns the adapter transaction."""


class InventoryLoader(Protocol):
    def __call__(self, path: Path) -> DiscoveryInventory: ...


ResultT = TypeVar("ResultT")
PreparedT = TypeVar("PreparedT")


def parse_adapter_identity(text: str) -> bytes:
    """Parse one complete, CRC-valid family-0x81 identity ROM."""

    try:
        identity = bytes.fromhex(text)
    except ValueError as exc:
        raise DiagnosticAccessError(
            "adapter identity must be 16 hexadecimal wire-order characters"
        ) from exc
    if (
        len(identity) != 8
        or identity[0] != DS9490_ID_FAMILY
        or dallas_crc8(identity) != 0
    ):
        raise DiagnosticAccessError(
            "adapter identity must be one CRC-valid family-0x81 ROM"
        )
    return identity


def accepted_adapter_identity(inventory: DiscoveryInventory) -> bytes:
    """Return the sole persisted root adapter identity, or fail closed."""

    identities = tuple(
        item.rom
        for item in inventory.devices
        if item.family_code == DS9490_ID_FAMILY and not item.path
    )
    if len(identities) != 1:
        raise DiagnosticAccessError(
            "persisted discovery evidence must contain exactly one root "
            "family-0x81 adapter identity"
        )
    identity = identities[0]
    if (
        len(identity) != 8
        or identity[0] != DS9490_ID_FAMILY
        or dallas_crc8(identity) != 0
    ):
        raise DiagnosticAccessError(
            "persisted adapter identity has an invalid Dallas CRC-8"
        )
    return identity


@dataclass(slots=True)
class VerifiedDiagnosticAccess:
    """Hold one cross-process lock from evidence validation through cleanup.

    Identity verification opens one candidate, performs one root Search ROM,
    and retains that exact native handle.  The diagnostic operation receives
    only that binding.  No operation can enumerate or open a replacement.
    """

    lock: AdapterAccessLock
    binding: AdapterIdentityBinding
    evidence_path: Path
    inventory_loader: InventoryLoader = load_inventory_evidence

    @classmethod
    def native(cls) -> "VerifiedDiagnosticAccess":
        return cls(
            AdapterAccessLock(),
            AdapterIdentityBinding(),
            installed_evidence_path(),
        )

    def run(
        self,
        expected_identity: bytes,
        operation: Callable[[UsbSessionFactory, PreparedT], ResultT],
        *,
        preflight: Callable[[DiscoveryInventory], PreparedT] = lambda inventory: inventory,
    ) -> ResultT:
        """Prepare from evidence, then verify and run on one retained handle.

        ``preflight`` is the only diagnostic-specific evidence-validation
        phase.  It runs under cross-process exclusion before libusb opens, and
        its immutable result is passed directly to ``operation`` so hardware
        code cannot repeat or race an evidence lookup.
        """

        if not self.lock.acquire(blocking=False):
            raise DiagnosticAdapterBusyError(
                "DS9490 is busy in the running plugin or another diagnostic process"
            )
        try:
            # Evidence is loaded only after exclusion and before libusb opens.
            inventory = self.inventory_loader(self.evidence_path)
            accepted = accepted_adapter_identity(inventory)
            if expected_identity != accepted:
                raise DiagnosticAccessError(
                    "requested adapter identity differs from accepted persisted evidence"
                )
            prepared = preflight(inventory)
            self.binding.verify(expected_identity)
            return operation(self.binding, prepared)
        finally:
            try:
                self.binding.close()
            finally:
                self.lock.release()
