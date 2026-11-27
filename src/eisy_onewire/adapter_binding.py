"""Explicit, process-lifetime binding to one verified DS9490 identity."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import AbstractContextManager
from typing import Any, Callable, Protocol

from .ds2490_transport import (
    AdapterNotFoundError,
    Ds2490Adapter,
    PersistentCtypesLibUsbSessionFactory,
    UsbSessionFactory,
)
from .onewire_discovery import DiscoveryReport, discover_devices, dallas_crc8
from .usb_detector import UsbDeviceDescriptor


DS9490_ID_FAMILY = 0x81


class AdapterBindingError(RuntimeError):
    """A durable identity could not be bound safely."""


class AdapterIdentityMismatchError(AdapterBindingError):
    """The connected adapter is not the accepted adapter."""


class PersistentFactory(UsbSessionFactory, Protocol):
    descriptor: UsbDeviceDescriptor

    @property
    def valid(self) -> bool: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AdapterBindingEvidence:
    identity_rom: bytes
    descriptor: UsbDeviceDescriptor
    root_device_count: int


class AdapterIdentityBinding:
    """Route operations only through an explicitly verified retained handle.

    USB location fields are recorded as evidence, never as identity.  This
    object never enumerates or opens a replacement from :meth:`open`.
    """

    def __init__(
        self,
        candidate_factory: Callable[[], PersistentFactory] = (
            PersistentCtypesLibUsbSessionFactory.establish
        ),
        identity_search: Callable[[UsbSessionFactory], DiscoveryReport] = (
            lambda factory: discover_devices(Ds2490Adapter(factory), max_devices=15)
        ),
        on_invalidated: Callable[[str], None] | None = None,
    ) -> None:
        self._candidate_factory = candidate_factory
        self._identity_search = identity_search
        self._on_invalidated = on_invalidated
        self._factory: PersistentFactory | None = None
        self._evidence: AdapterBindingEvidence | None = None

    @property
    def verified(self) -> bool:
        return self._factory is not None and self._factory.valid

    @property
    def evidence(self) -> AdapterBindingEvidence | None:
        return self._evidence if self.verified else None

    def verify(self, expected_identity_rom: bytes) -> AdapterBindingEvidence:
        """Perform one explicitly authorized root search and retain that handle."""

        _validate_identity(expected_identity_rom)
        if self.verified and self._evidence is not None:
            if self._evidence.identity_rom != expected_identity_rom:
                raise AdapterIdentityMismatchError(
                    "the live binding belongs to a different accepted adapter"
                )
            return self._evidence

        self._discard()
        candidate = self._candidate_factory()
        try:
            report = self._identity_search(candidate)
            if report.invalid_responses:
                raise AdapterBindingError(
                    "adapter identity verification returned CRC-invalid ROM data"
                )
            identities = tuple(
                item.rom for item in report.devices if item.family_code == DS9490_ID_FAMILY
            )
            if len(identities) != 1:
                raise AdapterBindingError(
                    "adapter identity verification requires exactly one CRC-valid "
                    "family-0x81 ROM"
                )
            if identities[0] != expected_identity_rom:
                raise AdapterIdentityMismatchError(
                    "connected DS9490 identity does not match the accepted inventory; "
                    "the binding was not changed"
                )
            evidence = AdapterBindingEvidence(
                identities[0], candidate.descriptor, len(report.devices)
            )
        except BaseException:
            candidate.close()
            raise
        self._factory = candidate
        self._evidence = evidence
        return evidence

    def set_invalidation_callback(self, callback: Callable[[str], None]) -> None:
        self._on_invalidated = callback

    def open(self):
        if not self.verified or self._factory is None:
            if self._factory is not None and not self._factory.valid:
                self._notify_invalidated(
                    "verified DS9490 retained handle became unusable"
                )
            self._evidence = None
            raise AdapterNotFoundError(
                "DS9490 identity is not verified for this plugin process; use "
                "Verify Adapter Identity before any sensor operation"
            )
        factory = self._factory
        try:
            session = factory.open()
        except BaseException:
            if not factory.valid:
                self._notify_invalidated(
                    "verified DS9490 retained handle became unusable"
                )
            raise
        return _BindingSessionContext(self, factory, session)

    def invalidate(self) -> None:
        """Discard a stale binding; never search for or substitute another device."""

        self._discard()

    def close(self) -> None:
        self._discard()

    def _discard(self) -> None:
        factory = self._factory
        self._factory = None
        self._evidence = None
        if factory is not None:
            factory.close()

    def _notify_invalidated(self, detail: str) -> None:
        # This is called only after a failed open, or after the active session
        # has completed its restoration/release path.  Discarding here closes
        # the now-untrusted retained handle without racing an in-flight claim.
        self._discard()
        if self._on_invalidated is not None:
            self._on_invalidated(detail)


class _BindingSessionContext(AbstractContextManager[Any]):
    def __init__(self, owner, factory, session) -> None:
        self._owner = owner
        self._factory = factory
        self._session = session

    def __enter__(self):
        return self._session.__enter__()

    def __exit__(self, exc_type, exc, traceback):
        try:
            return self._session.__exit__(exc_type, exc, traceback)
        finally:
            if not self._factory.valid:
                self._owner._notify_invalidated(
                    "verified DS9490 retained handle became unusable"
                )


def _validate_identity(identity_rom: bytes) -> None:
    if (
        len(identity_rom) != 8
        or identity_rom[0] != DS9490_ID_FAMILY
        or dallas_crc8(identity_rom) != 0
    ):
        raise AdapterBindingError(
            "accepted adapter identity must be one CRC-valid family-0x81 ROM"
        )
