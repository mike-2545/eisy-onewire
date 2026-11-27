"""Validated ROM-keyed sensor configuration from eisy-ui custom parameters."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .ds18b20 import InvalidRomError, parse_ds18b20_rom
from .ds2438 import parse_ds2438_rom
from .node_identity import DEFAULT_ADAPTER_NAMESPACE, sensor_address


CONFIG_VERSION_KEY = "_config_version"
CONFIG_VERSION = "1"
DS2409_EVENT_POLICY_KEY = "_ds2409_event_flag_policy"
DS2409_BRANCH_READ_EVENT_POLICY_KEY = "_ds2409_branch_read_event_flag_policy"
DS2409_UNATTENDED_BRANCH_POLICY_KEY = "_ds2409_unattended_branch_access_policy"
MAX_FRIENDLY_NAME_LENGTH = 64
DS2438_APPLICATION_PREFIX = "_ds2438_application_"


class SensorConfigurationError(ValueError):
    """One or more user-editable sensor mappings are unsafe to register."""


class Ds2409EventFlagPolicy(str, Enum):
    PRESERVE = "preserve"
    ACKNOWLEDGE = "acknowledge"


class Ds2409UnattendedBranchPolicy(str, Enum):
    """Independent authorization for scheduled DS2409 branch transactions."""

    DISABLED = "disabled"
    PRESERVE = "preserve"
    ACKNOWLEDGE = "acknowledge"


class Ds2438Application(str, Enum):
    """Explicitly identified analog application attached to one DS2438."""

    UNASSIGNED = "unassigned"
    HIH4000 = "hih4000"


@dataclass(frozen=True, slots=True)
class ConfiguredDs18b20:
    rom: bytes
    address: str
    name: str


@dataclass(frozen=True, slots=True)
class ConfiguredDs2438:
    rom: bytes
    address: str
    application: Ds2438Application


@dataclass(frozen=True, slots=True)
class FriendlyNameOverride:
    """Optional, cosmetic name associated with one supported sensor ROM."""

    rom: bytes
    address: str
    name: str


def load_default_parameters(path: Path) -> dict[str, str]:
    """Load the packaged initial mapping without embedding friendly names in code."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw.items()
    ):
        raise SensorConfigurationError("default sensor configuration must be string pairs")
    if raw.get(CONFIG_VERSION_KEY) != CONFIG_VERSION:
        raise SensorConfigurationError("default sensor configuration version is unsupported")
    # Validate packaged defaults through the same path as user input.
    parse_sensor_parameters(raw)
    return dict(raw)


def parse_sensor_parameters(
    parameters: Mapping[str, object],
    *,
    adapter_namespace: str = DEFAULT_ADAPTER_NAMESPACE,
) -> tuple[ConfiguredDs18b20, ...]:
    """Strict compatibility parser for family-0x28 name overrides.

    Runtime reconciliation uses :func:`parse_friendly_name_overrides` so one
    malformed cosmetic entry cannot disable otherwise safe configuration.
    """

    version = parameters.get(CONFIG_VERSION_KEY)
    if version != CONFIG_VERSION:
        raise SensorConfigurationError(
            f"{CONFIG_VERSION_KEY} must be {CONFIG_VERSION!r}"
        )
    parse_ds2409_event_flag_policy(parameters)
    parse_ds2409_branch_read_event_flag_policy(parameters)
    parse_ds2409_unattended_branch_policy(parameters)

    overrides, errors = parse_friendly_name_overrides(
        parameters, adapter_namespace=adapter_namespace
    )
    if errors:
        raise SensorConfigurationError("; ".join(errors))
    configured: list[ConfiguredDs18b20] = []
    for item in overrides:
        if item.rom[0] == 0x28:
            configured.append(ConfiguredDs18b20(item.rom, item.address, item.name))
    return tuple(configured)


def parse_friendly_name_overrides(
    parameters: Mapping[str, object],
    *,
    adapter_namespace: str = DEFAULT_ADAPTER_NAMESPACE,
    previous: Mapping[bytes, FriendlyNameOverride] | None = None,
) -> tuple[tuple[FriendlyNameOverride, ...], tuple[str, ...]]:
    """Parse optional names independently, preserving a prior valid value.

    Only non-underscore keys are naming overrides.  Errors are returned per
    entry; safety-critical parameters are deliberately outside this parser.
    """

    retained = dict(previous or {})
    configured: dict[bytes, FriendlyNameOverride] = {}
    errors: list[str] = []
    names: dict[str, bytes] = {}
    for rom_text, configured_name in parameters.items():
        if not isinstance(rom_text, str):
            errors.append("friendly-name ROM keys must be strings")
            continue
        if rom_text.startswith("_"):
            continue
        try:
            raw_rom = bytes.fromhex(rom_text)
            family = raw_rom[0]
            if family == 0x28:
                rom = parse_ds18b20_rom(rom_text)
            elif family == 0x26:
                rom = parse_ds2438_rom(rom_text)
            else:
                raise ValueError(f"unsupported family 0x{family:02X}")
        except (InvalidRomError, TypeError, ValueError, IndexError) as exc:
            errors.append(f"invalid optional friendly-name ROM {rom_text!r}: {exc}")
            continue
        if not isinstance(configured_name, str):
            errors.append(f"friendly name for {rom_text.upper()} must be text")
            if rom in retained:
                configured[rom] = retained[rom]
            continue
        name = configured_name.strip()
        if not name:
            errors.append(f"friendly name for {rom_text.upper()} must not be empty")
            if rom in retained:
                configured[rom] = retained[rom]
            continue
        if len(name) > MAX_FRIENDLY_NAME_LENGTH:
            errors.append(
                f"friendly name for {rom_text.upper()} exceeds "
                f"{MAX_FRIENDLY_NAME_LENGTH} characters"
            )
            if rom in retained:
                configured[rom] = retained[rom]
            continue
        folded = name.casefold()
        if folded in names:
            errors.append(
                f"duplicate optional friendly name {name!r} for "
                f"{rom_text.upper()} and {names[folded].hex().upper()}"
            )
            if rom in retained:
                configured[rom] = retained[rom]
            continue
        names[folded] = rom
        address = sensor_address(rom, adapter_namespace)
        configured[rom] = FriendlyNameOverride(rom, address, name)

    return (
        tuple(sorted(configured.values(), key=lambda item: item.address)),
        tuple(errors),
    )


def parse_ds2438_applications(
    parameters: Mapping[str, object],
    *,
    adapter_namespace: str = DEFAULT_ADAPTER_NAMESPACE,
) -> tuple[ConfiguredDs2438, ...]:
    """Parse explicit per-ROM DS2438 application declarations.

    A discovered family-0x26 device remains inventory-only while its generated
    declaration is ``unassigned``.  ``hih4000`` is always an explicit operator
    selection; there is no inference from family code or voltage.
    """

    configured: list[ConfiguredDs2438] = []
    seen: set[bytes] = set()
    for key, value in parameters.items():
        if not isinstance(key, str) or not key.startswith(DS2438_APPLICATION_PREFIX):
            continue
        rom_text = key[len(DS2438_APPLICATION_PREFIX) :]
        try:
            rom = parse_ds2438_rom(rom_text)
        except (TypeError, ValueError) as exc:
            raise SensorConfigurationError(
                f"invalid DS2438 application ROM {rom_text!r}: {exc}"
            ) from exc
        if not isinstance(value, str):
            raise SensorConfigurationError(
                f"DS2438 application for {rom_text.upper()} must be text"
            )
        try:
            application = Ds2438Application(value.strip().lower())
        except ValueError as exc:
            raise SensorConfigurationError(
                f"DS2438 application for {rom_text.upper()} must be one of: "
                "unassigned, hih4000"
            ) from exc
        if rom in seen:
            raise SensorConfigurationError(
                f"duplicate DS2438 application for {rom_text.upper()}"
            )
        seen.add(rom)
        configured.append(
            ConfiguredDs2438(
                rom,
                sensor_address(rom, adapter_namespace),
                application,
            )
        )
    return tuple(sorted(configured, key=lambda item: item.address))


def parse_ds2409_event_flag_policy(
    parameters: Mapping[str, object],
) -> Ds2409EventFlagPolicy:
    """Return the explicit event-latch policy; omission always preserves flags."""

    raw = parameters.get(
        DS2409_EVENT_POLICY_KEY, Ds2409EventFlagPolicy.PRESERVE.value
    )
    if not isinstance(raw, str):
        raise SensorConfigurationError(
            f"{DS2409_EVENT_POLICY_KEY} must be text"
        )
    try:
        return Ds2409EventFlagPolicy(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in Ds2409EventFlagPolicy)
        raise SensorConfigurationError(
            f"{DS2409_EVENT_POLICY_KEY} must be one of: {allowed}"
        ) from exc


def parse_ds2409_branch_read_event_flag_policy(
    parameters: Mapping[str, object],
) -> Ds2409EventFlagPolicy:
    """Return branch-read permission independently from discovery permission."""

    raw = parameters.get(
        DS2409_BRANCH_READ_EVENT_POLICY_KEY,
        Ds2409EventFlagPolicy.PRESERVE.value,
    )
    if not isinstance(raw, str):
        raise SensorConfigurationError(
            f"{DS2409_BRANCH_READ_EVENT_POLICY_KEY} must be text"
        )
    try:
        return Ds2409EventFlagPolicy(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in Ds2409EventFlagPolicy)
        raise SensorConfigurationError(
            f"{DS2409_BRANCH_READ_EVENT_POLICY_KEY} must be one of: {allowed}"
        ) from exc


def parse_ds2409_unattended_branch_policy(
    parameters: Mapping[str, object],
) -> Ds2409UnattendedBranchPolicy:
    """Return only the dedicated unattended policy; no manual policy implies it."""

    raw = parameters.get(
        DS2409_UNATTENDED_BRANCH_POLICY_KEY,
        Ds2409UnattendedBranchPolicy.DISABLED.value,
    )
    if not isinstance(raw, str):
        raise SensorConfigurationError(
            f"{DS2409_UNATTENDED_BRANCH_POLICY_KEY} must be text"
        )
    try:
        return Ds2409UnattendedBranchPolicy(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in Ds2409UnattendedBranchPolicy)
        raise SensorConfigurationError(
            f"{DS2409_UNATTENDED_BRANCH_POLICY_KEY} must be one of: {allowed}"
        ) from exc
