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
    """Validate all ROM/name pairs atomically and return stable child identities."""

    version = parameters.get(CONFIG_VERSION_KEY)
    if version != CONFIG_VERSION:
        raise SensorConfigurationError(
            f"{CONFIG_VERSION_KEY} must be {CONFIG_VERSION!r}"
        )
    parse_ds2409_event_flag_policy(parameters)
    parse_ds2409_branch_read_event_flag_policy(parameters)
    parse_ds2409_unattended_branch_policy(parameters)

    configured: list[ConfiguredDs18b20] = []
    addresses: set[str] = set()
    names: set[str] = set()
    for rom_text, configured_name in parameters.items():
        if not isinstance(rom_text, str):
            raise SensorConfigurationError("sensor ROM keys must be strings")
        if rom_text in (
            CONFIG_VERSION_KEY,
            DS2409_EVENT_POLICY_KEY,
            DS2409_BRANCH_READ_EVENT_POLICY_KEY,
            DS2409_UNATTENDED_BRANCH_POLICY_KEY,
        ) or rom_text.startswith(DS2438_APPLICATION_PREFIX):
            continue
        try:
            rom = parse_ds18b20_rom(rom_text)
        except InvalidRomError as exc:
            raise SensorConfigurationError(f"invalid sensor ROM {rom_text!r}: {exc}") from exc
        if not isinstance(configured_name, str):
            raise SensorConfigurationError(
                f"friendly name for {rom_text.upper()} must be text"
            )
        name = configured_name.strip()
        if not name:
            raise SensorConfigurationError(
                f"friendly name for {rom_text.upper()} must not be empty"
            )
        if len(name) > MAX_FRIENDLY_NAME_LENGTH:
            raise SensorConfigurationError(
                f"friendly name for {rom_text.upper()} exceeds "
                f"{MAX_FRIENDLY_NAME_LENGTH} characters"
            )
        folded = name.casefold()
        if folded in names:
            raise SensorConfigurationError(f"duplicate friendly name {name!r}")
        names.add(folded)
        address = sensor_address(rom, adapter_namespace)
        if address in addresses:
            raise SensorConfigurationError(f"duplicate sensor address {address}")
        addresses.add(address)
        configured.append(ConfiguredDs18b20(rom, address, name))

    return tuple(sorted(configured, key=lambda item: item.address))


def parse_ds2438_applications(
    parameters: Mapping[str, object],
    *,
    adapter_namespace: str = DEFAULT_ADAPTER_NAMESPACE,
) -> tuple[ConfiguredDs2438, ...]:
    """Parse explicit per-ROM DS2438 application declarations.

    A discovered family-0x26 device remains inventory-only unless the operator
    adds ``_ds2438_application_<full ROM> = hih4000``.  The model token is
    deliberately narrow: there is no inference from family code or voltage.
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
                f"DS2438 application for {rom_text.upper()} must be hih4000"
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
