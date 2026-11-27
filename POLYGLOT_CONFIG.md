# eisy-onewire configuration reference

Version 0.3.16 uses the supported eisy-ui plugin configuration and IoX command
interfaces. Most users can discover sensors, rename nodes, select intervals,
and change temperature presentation without typing a ROM.

## Safe defaults

New installations begin with:

```text
_config_version=1
_ds2409_event_flag_policy=preserve
_ds2409_branch_read_event_flag_policy=preserve
_ds2409_unattended_branch_access_policy=disabled
```

Automatic Refresh and every sensor interval also start Off. No discovery or
sensor measurement is initiated by saving configuration.

## Friendly names

Prefer the standard IoX rename action after discovery. Stable node addresses
remain derived from the full 64-bit ROM.

An optional ROM-keyed override is supported for compatibility for DS18B20
(`0x28`) and DS2438 (`0x26`) devices:

```text
<complete-CRC-valid-16-character-ROM>=Friendly Name
```

The key must contain all eight ROM bytes as uppercase or lowercase hexadecimal,
without OWFS punctuation. Sensors without an override receive deterministic
ROM-based names. A malformed optional name raises an actionable Notice but
does not disable unrelated sensors or scheduler targets. Correct or remove the
entry to clear the Notice. To migrate, rename through IoX, allow reconciliation
to complete, then remove the legacy override. Removal never restores a default
or previously cached name. A newly added or changed valid override deliberately
renames the node once; an unchanged override and unrelated configuration saves
do not rename it.

## DS2438/HIH-4000 application

A discovered family-`0x26` device automatically receives a safe selector:

```text
_ds2438_application_<complete-CRC-valid-ROM>=unassigned
```

It remains inventory-only and cannot be measured. After identifying a
Honeywell HIH-4000-series circuit, change the value to `hih4000`:

```text
_ds2438_application_<complete-CRC-valid-ROM>=hih4000
```

Do not select `hih4000` based only on the family code or voltage values.
The plugin does not support arbitrary DS2438 analog circuits and never writes
DS2438 EEPROM. A configured node starts stale with interval Off.
Changing an existing selector back to `unassigned` immediately revokes manual
and scheduled acquisition. The stable node may remain visible with cached
values marked stale; this avoids destructive IoX node deletion.

## Sensor interval commands

Each supported sensor node provides fixed commands for:

- Off
- 30 seconds
- 60 seconds
- 120 seconds
- 300 seconds

The controller's Automatic Refresh Enable/Disable command is the independent
master state. The scheduler runs only when both master and a sensor interval
are enabled. It never performs discovery or automatic retry.

## Temperature presentation commands

The controller provides:

- Use Fahrenheit / Use Celsius
- 0 / 1 / 2 / 3 Decimal Places

These settings persist in plugin customdata. They re-render validated cached
temperatures without USB activity, preserve GV0, and never modify humidity.

## Advanced DS2409 Permissions

These three settings are intentionally separate. They are not ordinary IoX
buttons and must be changed only by an operator who understands DS2409 event
latches. Their defaults remain `preserve`, `preserve`,
and `disabled`.

### Manual discovery

`_ds2409_event_flag_policy` — default `preserve`

- `preserve`: manual discovery stops before a required event-latch clear.
- `acknowledge`: the explicitly invoked discovery may clear latches after
  recording available evidence.

### Manual branch sensor reads

`_ds2409_branch_read_event_flag_policy` — default `preserve`

- `preserve`: a manual branch read stops if safe restoration would require an
  acknowledged All-Lines-Off.
- `acknowledge`: one explicitly invoked branch read may perform that restoration
  after capturing original and pre-restoration evidence.

### Scheduled branch reads

`_ds2409_unattended_branch_access_policy` — default `disabled`

- `disabled`: branch sensors are manual-only.
- `preserve`: scheduling may use a branch only when restoration does not require
  acknowledged latch clearing.
- `acknowledge`: scheduling may clear latches during verified restoration after
  capturing evidence.

**All-Lines-Off can irreversibly clear event latches. Cleared evidence cannot
be restored.** Installations using DS2409 event flags for alarms or Conditional
Search must not enable acknowledgment indiscriminately and should keep
unattended branch access disabled.

Changing any policy performs no USB operation by itself. The manual policies
do not authorize scheduled access, and scheduled permission does not cause
discovery. Saving configuration does not run discovery.

## Persisted plugin data

The plugin atomically maintains discovery inventory, refresh settings,
temperature presentation, and validated cached measurements in its own
customdata. Users must not edit these records directly. Malformed data fails
closed and raises a Notice.

After restart, cached ST/humidity values may be restored, but GV0 is false until
a new read succeeds. Existing node identities, user names, intervals, and
configuration are reconciled without an automatic bus scan.

## Notices

Notices report discovery inventory, scheduler eligibility/state, per-sensor
refresh failures, profile consistency, DS2409 event evidence, and restoration
status. Review them before changing advanced policies or enabling automation.

See [docs/user-guide.md](docs/user-guide.md) for normal operation and
[docs/troubleshooting.md](docs/troubleshooting.md) for failure guidance.
