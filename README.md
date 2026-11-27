# eisy-onewire

`eisy-onewire` is a native 1-Wire plugin for Universal Devices eisy. It uses
the DS9490R USB adapter directly through FreeBSD libusb, so a separate
Raspberry Pi, OWFS server, or network bridge is not required.

Version **0.3.17** is a **public beta**. Controlled live acceptance covered a
DS9490R, multiple DS18B20 temperature sensors, a DS2409 coupler, an explicitly
identified DS2438/Honeywell HIH-4000 humidity application, generic DS2438
analog voltage, and a provisionally calibrated solar-voltage application.
Review the [known limitations](#known-limitations) before enabling branch
access or automatic refresh.

## What it does

- Discovers CRC-valid 1-Wire ROMs on one DS9490 adapter.
- Creates IoX nodes for supported DS18B20 temperature sensors.
- Reads externally powered DS18B20 sensors manually or on a configured
  schedule.
- Discovers devices behind one level of DS2409 main/aux branches with explicit
  event-latch safeguards.
- Supports an explicitly configured DS2438 with a Honeywell HIH-4000-series
  analog humidity sensor.
- Supports explicitly configured DS2438 generic analog-voltage and calibrated
  solar-irradiance applications.
- Displays temperature in Fahrenheit or Celsius with 0--3 decimal places.
- Preserves the last valid reading as stale when a later read fails or after a
  restart.

## Requirements

- Universal Devices **eisy** with current eisy-ui/PG3x and IoX software.
- Dallas/Maxim DS9490/DS9490R USB 1-Wire adapter (VID `04fa`, PID `2490`).
- Supported 1-Wire hardware listed in
  [docs/supported-hardware.md](docs/supported-hardware.md).
- The adapter must be reconnected after first installation when directed so
  the platform-managed USB permission rule can apply.

The plugin declares `udi_interface==3.4.9`; eisy-ui installs it in the
plugin-managed runtime. Do not install it globally or manually change USB
permissions.

## Installation

Install the beta through the supported eisy-ui Plugin Store/Local-store
lifecycle. Keep the existing slot when upgrading.

1. Back up your eisy and record existing plugin configuration.
2. Install or update **eisy-onewire** in eisy-ui.
3. Confirm the plugin reaches **Connected** and its runtime profile installs.
4. Confirm the DS9490 rule is limited to VID `04fa`, PID `2490`, the dedicated
   plugin account, and mode `0600`.
5. If this is the first install, physically reconnect the DS9490 once after
   installation, then verify **Adapter Accessible** is true.
6. Leave **Automatic Refresh** disabled until manual reads have succeeded.

See [docs/installation.md](docs/installation.md) for installation, upgrade,
verification, and rollback details.

## First use

### Discover devices

Open the **1-Wire Controller** node and select **Discover Devices**. Discovery
is manual; startup never scans the bus. Supported family `0x28` devices are
registered as temperature nodes. DS2409 (`0x1F`), DS2438 (`0x26`), and the
DS9490 identification device (`0x81`) are inventoried without being mistaken
for temperature sensors.

DS2409 branches may remain unscanned if safely restoring the coupler could
clear event latches. Read the
[advanced DS2409 guidance](docs/user-guide.md#advanced-ds2409-permissions)
before changing any policy.

### Rename sensors

Rename a discovered node with the standard IoX node rename control. The stable
node address is derived from the ROM and does not change. Optional ROM-keyed
name overrides for families `0x28` and `0x26` remain available for
compatibility; users normally do not need to type a ROM address. After initial
registration the displayed name is user-owned: ordinary reconciliation uses
`rename=False` and does not replace an external IoX rename. A newly added or
changed legacy override is an explicit one-time rename request; removing it
leaves the current IoX name untouched.

### Refresh a sensor

Select **Refresh Temperature** on a DS18B20 node, or **Refresh Temperature and
Humidity** on a configured DS2438/HIH-4000 node. Reads are serialized and do
not retry automatically. A branch sensor may refuse the operation under the
safe `preserve` policy if restoration would clear DS2409 event latches.

### Configure automatic refresh

Each sensor offers **Off**, **30**, **60**, **120**, or **300 seconds**. Set the
sensor interval first, then use **Automatic Refresh Enable** on the controller.
New sensors default to Off and the controller defaults to disabled.

Root-bus DS18B20, scheduled branch access, and DS2438 polling have passed
controlled live acceptance. Branch polling still requires the separate
unattended DS2409 policy, and acknowledgment can irreversibly clear event
latches. Keep unfamiliar sensors Off until their circuit, topology, and safety
requirements have been reviewed.

### Temperature units and precision

The controller provides Fahrenheit/Celsius and 0--3 decimal-place commands.
Changes re-render cached temperatures without touching the USB bus. Humidity
is not changed. Some eisy-ui views suppress trailing zeros even though the
profile and numeric driver precision are correct.

### DS2438 and HIH-4000 humidity

Family `0x26` devices remain inventory-only by default because a DS2438 does
not identify the analog sensor attached to it. Discovery automatically adds a
safe selector with value `unassigned`. After physically confirming a Honeywell
HIH-4000-series application, change that selector to `hih4000`:

```text
_ds2438_application_<16-character-ROM>=hih4000
```

Use the complete CRC-valid ROM reported by discovery. Do not assume every
DS2438 is a humidity sensor. Details and acceptance qualifications are in the
[user guide](docs/user-guide.md#ds2438hih-4000-configuration).

Other explicit choices are `analog_voltage` and `solar_irradiance`. Solar
selection creates ROM-specific multiplier and offset parameters. A positive
multiplier is mandatory; there is intentionally no universal default.
See [Using the DS2438 as an analog-to-1-Wire interface](docs/ds2438-analog-interface.md)
for pinout, electrical limits, conditioning guidance, and application examples.

## Current and stale readings

**Current Reading Valid** (`GV0`) is true only after a fresh read completes,
passes CRC and value validation, and (for branch devices) restores and verifies
the original coupler state. The last valid temperature/humidity remains visible
when a later attempt fails, but GV0 becomes false so the value is clearly
stale. Cached values also return as stale after plugin restart until refreshed.

## Known limitations

- One DS9490 adapter per plugin instance; no adapter selector yet.
- One DS2409 coupler level; nested couplers are not supported.
- DS18B20 parasite-powered conversion is not supported. Sensors must be
  externally powered.
- No automatic discovery and no automatic retries.
- Only explicitly identified DS2438 applications are measured. Solar accuracy
  depends on operator-supplied calibration; unidentified analog humidity
  circuits remain `unassigned`.
- DS2409 event latches can be irreversibly cleared by All-Lines-Off. The safe
  defaults refuse operations that require acknowledgment.
- Unattended DS2438/branch humidity polling passed controlled live acceptance
  in v0.3.16 under explicit DS2409 authorization.
- This is beta software; preserve backups and validate manually before enabling
  automation.

## Documentation

- [Installation and rollback](docs/installation.md)
- [User guide](docs/user-guide.md)
- [Supported and tested hardware](docs/supported-hardware.md)
- [DS2438 analog interface guide](docs/ds2438-analog-interface.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Configuration reference](POLYGLOT_CONFIG.md)
- [Development history](docs/development-history.md)
- [Changelog](CHANGELOG.md)

## Support and contributions

Use [GitHub Issues](https://github.com/mike-2545/eisy-onewire/issues) for bugs
and sensor-support requests. Include the plugin version, eisy/IoX versions,
adapter model, complete 1-Wire family/ROM identifiers, topology, relevant
Notices, and a short redacted log excerpt. Never post credentials, tokens,
private keys, full configuration exports, or unrelated home-automation data.

For general eisy and Plugin Store questions, use the
[Universal Devices forum](https://forum.universal-devices.com/).

## License

eisy-onewire is released under the [MIT License](LICENSE). Attribution and
license information for third-party dependencies and test material is recorded
in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
