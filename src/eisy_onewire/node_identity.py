"""Stable IoX identities for 1-Wire adapters and ROM-addressed children."""

from __future__ import annotations

import hashlib


DEFAULT_ADAPTER_NAMESPACE = "default"
DEFAULT_CONTROLLER_ADDRESS = "onewire"


def controller_address(adapter_namespace: str = DEFAULT_ADAPTER_NAMESPACE) -> str:
    """Return the stable controller address for one configured adapter namespace."""

    if adapter_namespace == DEFAULT_ADAPTER_NAMESPACE:
        return DEFAULT_CONTROLLER_ADDRESS
    return "ow" + _digest(adapter_namespace.encode("utf-8"), 12)


def sensor_address(
    rom: bytes,
    adapter_namespace: str = DEFAULT_ADAPTER_NAMESPACE,
) -> str:
    """Return a stable IoX address, retaining legacy addresses on the first bus."""

    if len(rom) != 8:
        raise ValueError("a 1-Wire ROM must contain exactly 8 bytes")
    if adapter_namespace == DEFAULT_ADAPTER_NAMESPACE:
        # The established eisy-onewire address is the seven ROM bytes before
        # the CRC. Preserve it for an in-place 0.3.3 -> 0.3.4 migration.
        return rom[:7].hex()
    return "w" + _digest(adapter_namespace.encode("utf-8") + rom, 13)


def _digest(value: bytes, length: int) -> str:
    return hashlib.sha256(value).hexdigest()[:length]
