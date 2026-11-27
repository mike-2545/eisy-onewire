"""Validated configuration state for future automatic temperature refresh."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

from .discovery_inventory import DS18B20_FAMILY, DS2438_FAMILY, DiscoveryInventory
from .ds18b20 import InvalidRomError, parse_ds18b20_rom
from .ds2438 import parse_ds2438_rom
from .node_identity import sensor_address


REFRESH_CONFIGURATION_KEY = "automatic_refresh_configuration"
REFRESH_CONFIGURATION_SCHEMA = 2
LEGACY_REFRESH_CONFIGURATION_SCHEMA = 1
ALLOWED_INTERVALS = (0, 30, 60, 120, 300)
MAX_FRIENDLY_NAME_LENGTH = 64


class RefreshConfigurationError(ValueError):
    """Persisted refresh/name state is malformed or not inventory-backed."""


@dataclass(frozen=True, slots=True)
class SensorRefreshConfiguration:
    rom: bytes
    address: str
    interval_seconds: int
    legacy_name_override: str | None
    effective_name: str


@dataclass(frozen=True, slots=True)
class RefreshConfiguration:
    master_enabled: bool = False
    sensors: tuple[SensorRefreshConfiguration, ...] = ()
    scheduler_armed: bool = False

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": REFRESH_CONFIGURATION_SCHEMA,
            "master_enabled": self.master_enabled,
            "scheduler_armed": self.scheduler_armed,
            "sensors": [
                {
                    "rom": item.rom.hex().upper(),
                    "address": item.address,
                    "interval_seconds": item.interval_seconds,
                    "legacy_name_override": item.legacy_name_override,
                    "effective_name": item.effective_name,
                }
                for item in self.sensors
            ],
        }

    @classmethod
    def from_data(
        cls, raw: object, inventory: DiscoveryInventory
    ) -> "RefreshConfiguration":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise RefreshConfigurationError("refresh configuration must be an object")
        schema = raw.get("schema_version")
        if schema not in (
            LEGACY_REFRESH_CONFIGURATION_SCHEMA,
            REFRESH_CONFIGURATION_SCHEMA,
        ):
            raise RefreshConfigurationError("refresh configuration schema is unsupported")
        master = raw.get("master_enabled")
        if not isinstance(master, bool):
            raise RefreshConfigurationError("master_enabled must be Boolean")
        if schema == LEGACY_REFRESH_CONFIGURATION_SCHEMA:
            # v0.3.10 stored configuration intent only.  It never authorized
            # unattended USB traffic, so an upgrade is deliberately disarmed.
            master = False
            armed = False
        else:
            armed = raw.get("scheduler_armed")
            if not isinstance(armed, bool):
                raise RefreshConfigurationError("scheduler_armed must be Boolean")
            if armed and not master:
                raise RefreshConfigurationError(
                    "scheduler_armed cannot be true while master is disabled"
                )
        rows = raw.get("sensors")
        if not isinstance(rows, list):
            raise RefreshConfigurationError("refresh configuration sensors must be a list")

        inventory_by_rom = {
            item.rom: item
            for item in inventory.devices
            if item.family_code in (DS18B20_FAMILY, DS2438_FAMILY)
        }
        configured: list[SensorRefreshConfiguration] = []
        seen_roms: set[bytes] = set()
        seen_addresses: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise RefreshConfigurationError("refresh sensor row must be an object")
            try:
                rom = _parse_supported_rom(row.get("rom"))
            except (TypeError, ValueError, InvalidRomError) as exc:
                raise RefreshConfigurationError("refresh sensor ROM is invalid") from exc
            address = row.get("address")
            expected_address = sensor_address(rom)
            if address != expected_address:
                raise RefreshConfigurationError(
                    "refresh sensor address does not match its ROM"
                )
            if rom not in inventory_by_rom:
                raise RefreshConfigurationError(
                    "refresh sensor identity is not present in discovery inventory"
                )
            if rom in seen_roms or address in seen_addresses:
                raise RefreshConfigurationError(
                    "refresh configuration contains a duplicate sensor"
                )
            seen_roms.add(rom)
            seen_addresses.add(address)
            interval = _interval(row.get("interval_seconds"))
            legacy = _optional_name(row.get("legacy_name_override"))
            effective = _name(row.get("effective_name"), "effective_name")
            configured.append(
                SensorRefreshConfiguration(
                    rom, address, interval, legacy, effective
                )
            )
        return cls(
            master,
            tuple(sorted(configured, key=lambda item: item.address)),
            armed,
        )

    @staticmethod
    def requires_migration(raw: object) -> bool:
        return (
            isinstance(raw, Mapping)
            and raw.get("schema_version") == LEGACY_REFRESH_CONFIGURATION_SCHEMA
        )

    def sensor(self, rom: bytes) -> SensorRefreshConfiguration | None:
        return next((item for item in self.sensors if item.rom == rom), None)

    def with_sensor(self, sensor: SensorRefreshConfiguration) -> "RefreshConfiguration":
        rows = {item.rom: item for item in self.sensors}
        rows[sensor.rom] = sensor
        return replace(self, sensors=tuple(sorted(rows.values(), key=lambda item: item.address)))

    def with_interval(self, rom: bytes, interval: int) -> "RefreshConfiguration":
        value = _interval(interval)
        current = self.sensor(rom)
        if current is None:
            raise RefreshConfigurationError(
                "refresh interval target is not a validated discovered sensor"
            )
        return self.with_sensor(replace(current, interval_seconds=value))

    def with_master(self, enabled: bool) -> "RefreshConfiguration":
        """Apply one explicit controller command and arm only when enabling."""

        if not isinstance(enabled, bool):
            raise RefreshConfigurationError("master enabled state must be Boolean")
        return replace(self, master_enabled=enabled, scheduler_armed=enabled)


def resolved_sensor_configuration(
    rom: bytes,
    *,
    legacy_name_override: str | None,
    default_name: str,
    previous: SensorRefreshConfiguration | None,
) -> SensorRefreshConfiguration:
    """Track plugin-owned creation names and deliberate legacy overrides.

    Once a node has been registered, its displayed name belongs to the IoX
    operator.  PG3's database name is not an authoritative current-name
    source, so ordinary reconciliation never derives a rename from it.
    ``legacy_name_override`` records only the last override observed by this
    plugin; removing an override deliberately retains the last plugin-selected
    name so that no compensating rename is attempted.
    """

    parsed = _parse_supported_rom(rom.hex())
    address = sensor_address(parsed)
    legacy = _optional_name(legacy_name_override)
    fallback = _name(default_name, "default_name")
    if previous is None:
        effective = legacy or fallback
        interval = 0
    elif legacy is not None and legacy != previous.legacy_name_override:
        # A newly added or deliberately changed legacy override is the only
        # configuration action that authorizes an explicit rename.
        effective = legacy
        interval = previous.interval_seconds
    else:
        # An unchanged override, its removal, stale PG3 names, restarts, and
        # unrelated configuration saves must never rename an existing node.
        effective = previous.effective_name
        interval = previous.interval_seconds
    return SensorRefreshConfiguration(parsed, address, interval, legacy, effective)


def _interval(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in ALLOWED_INTERVALS:
        allowed = ", ".join(str(item) for item in ALLOWED_INTERVALS)
        raise RefreshConfigurationError(
            f"refresh interval must be one of: {allowed} seconds"
        )
    return value


def _parse_supported_rom(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError("sensor ROM must be hexadecimal text")
    try:
        family = bytes.fromhex(value)[0]
    except (ValueError, IndexError) as exc:
        raise ValueError("sensor ROM must be hexadecimal text") from exc
    if family == DS18B20_FAMILY:
        return parse_ds18b20_rom(value)
    if family == DS2438_FAMILY:
        return parse_ds2438_rom(value)
    raise ValueError("refresh sensor family is unsupported")


def _optional_name(value: object) -> str | None:
    if value is None:
        return None
    return _name(value, "friendly name")


def _name(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RefreshConfigurationError(f"{field} must be non-empty text")
    name = value.strip()
    if len(name) > MAX_FRIENDLY_NAME_LENGTH:
        raise RefreshConfigurationError(
            f"{field} exceeds {MAX_FRIENDLY_NAME_LENGTH} characters"
        )
    return name
