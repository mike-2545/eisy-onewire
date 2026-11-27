# v0.3.17 public beta release kit

This file prepares public-facing text. It does not authorize a GitHub Release,
Plugin Store publication, or forum posting.

## GitHub repository description

Native DS9490 1-Wire plugin for Universal Devices eisy with DS18B20, DS2409,
and explicitly configured DS2438 temperature, humidity, voltage, and solar
irradiance support.

## Suggested GitHub topics

`universal-devices`, `eisy`, `iox`, `pg3x`, `onewire`, `1-wire`, `ds9490`,
`ds2490`, `ds18b20`, `ds2409`, `ds2438`, `hih4000`, `pyranometer`, `freebsd`,
`libusb`, `home-automation`

## Release notes

Proposed release title: **eisy-onewire v0.3.17 — DS2438 Analog Voltage & Solar Irradiance**

### eisy-onewire 0.3.17 public beta

Version 0.3.17 adds two explicit DS2438 applications while preserving the
existing DS18B20, DS2409, and Honeywell HIH-4000 behavior:

- `analog_voltage` publishes CRC-validated VAD and VDD measurements.
- `solar_irradiance` publishes raw VAD and a configured linear conversion in
  W/m²: `VAD × multiplier + offset`.
- Solar calibration is per device, must be supplied by the operator, and is
  never inferred from ROM family or observed voltage.
- Missing or invalid solar calibration is isolated to that device and cannot
  disable unrelated scheduler targets.
- Application changes and calibration edits perform no USB operation.
- Stable ROM-derived addresses, operator-owned IoX names, cached/stale state,
  serialized access, zero automatic retries, and zero EEPROM writes remain in
  force.

Controlled live acceptance verified generic analog VAD/VDD acquisition and a
solar sensor's dark/illuminated voltage response plus the configured linear
math. It did not establish absolute irradiance accuracy or current instrument
calibration.

Beta limitations:

- One DS9490 adapter and one DS2409 level.
- No parasite-powered DS18B20 support.
- Unknown DS2438 applications remain inventory-only.
- Solar multiplier and offset require application-specific calibration data.
- Advanced DS2409 acknowledgment can irreversibly clear event latches.

Read the installation guide and leave Automatic Refresh disabled until manual
reads pass for the local topology.

## UD Plugin Store listing

**Short description**

Native DS9490 1-Wire temperature, humidity, voltage, and calibrated solar
integration for eisy.

**Full description**

eisy-onewire operates a Dallas/Maxim DS9490/DS9490R directly from Universal
Devices eisy using native FreeBSD libusb. It discovers CRC-valid 1-Wire ROMs,
creates stable IoX nodes for externally powered DS18B20 sensors, supports
manual and scheduled refresh, and provides Fahrenheit/Celsius presentation.
One-level DS2409 branches use conservative event-latch safeguards. Explicitly
identified DS2438 applications support Honeywell HIH-4000 humidity, generic
VAD/VDD monitoring, and operator-calibrated solar irradiance. Version 0.3.17
remains a public beta: one adapter, no parasite power, no automatic discovery
or retries, and advanced branch access requires careful local validation.

## Suggested URLs

- Documentation: `https://github.com/mike-2545/eisy-onewire/blob/main/README.md`
- Installation: `https://github.com/mike-2545/eisy-onewire/blob/main/docs/installation.md`
- User guide: `https://github.com/mike-2545/eisy-onewire/blob/main/docs/user-guide.md`
- Troubleshooting: `https://github.com/mike-2545/eisy-onewire/blob/main/docs/troubleshooting.md`
- Bugs and sensor requests: `https://github.com/mike-2545/eisy-onewire/issues`
- Community support: `https://forum.universal-devices.com/`

## Friendly UD forum announcement

**Subject: eisy-onewire 0.3.17 beta: DS2438 analog voltage and solar support**

eisy-onewire connects a DS9490/DS9490R directly to eisy through native
FreeBSD libusb, without a Raspberry Pi or OWFS host. Version 0.3.17 adds
explicit DS2438 generic-voltage and calibrated-solar applications alongside
the existing DS18B20, DS2409, and HIH-4000 support.

Solar support deliberately has no universal default calibration. Operators
must identify their attached analog hardware and supply its multiplier and
optional offset. The live acceptance test verified voltage response and the
configured calculation, not absolute irradiance calibration.

Please read the limitations before installing. DS2409 event-latch
acknowledgment is advanced and can irreversibly clear latch evidence. Start
with Automatic Refresh off and validate one manual read at a time.

When reporting a bug, include plugin/eisy/IoX versions, adapter model, sensor
family, redacted topology and Notices, the exact command and timestamp, and
expected versus actual behavior. Do not post credentials or full automation
exports. New-sensor requests should include an authoritative datasheet and
power/topology details.

## Artifact

The live-accepted v0.3.17 archive contains 49 allowlisted files and has
SHA-256 `6A162DC8ECCC3C3AADC8C0BC88F420D84E9C9FB89B47ECC766514152301A86FB`.
The release kit and private acceptance record are not packaged, so the public
archive must remain byte-identical to the accepted artifact.

## Repository model

The private `mike-2545/eisy-onewire-dev` repository remains the authoritative
development source. The public `mike-2545/eisy-onewire` repository receives
reviewed, independently rooted sanitized snapshots. Private commits and
acceptance-system identifiers are never pushed to the public repository.
