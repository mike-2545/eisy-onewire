# v0.3.19 public beta release kit

## GitHub repository description

Native DS9490 1-Wire plugin for Universal Devices eisy with identity-bound
adapter access and DS18B20, DS2409, and explicitly configured DS2438 support.

## Suggested GitHub topics

`universal-devices`, `eisy`, `iox`, `pg3x`, `onewire`, `1-wire`, `ds9490`,
`ds2490`, `ds18b20`, `ds2409`, `ds2438`, `hih4000`, `pyranometer`, `freebsd`,
`libusb`, `home-automation`

## Release notes

Proposed release title: **eisy-onewire v0.3.19 — Unattended Adapter Identity Recovery**

### eisy-onewire 0.3.19 public beta

Version 0.3.19 can restore an existing, accepted DS9490 binding after an
ordinary plugin restart or eisy reboot without weakening the identity safety
introduced in 0.3.18:

- Every ordinary transaction remains bound to the same retained native handle
  whose CRC-valid family-`0x81` ROM exactly matches accepted inventory.
- A different adapter is never accepted automatically, even if it is the only
  matching USB device or appears at the previous USB address or port.
- Multiple matching adapters remain ambiguous and block automatic and ordinary
  access. Simultaneous multi-adapter sensor-network operation is not supported.
- **Automatic Adapter Recovery** is persistent, operator controlled, and
  disabled by default. Enabling it authorizes one bounded identity Search ROM
  per supported recovery event, with zero automatic retries.
- The user's persistent **Automatic Refresh** preference is independent of the
  current adapter binding. Scheduling resumes only after identity verification
  and only when that preference was already enabled.
- The readiness window is eight seconds. If it expires, use manual **Verify
  Adapter Identity**; the plugin does not maintain a permanent USB hotplug
  monitor.
- Automatic recovery never performs topology discovery, branch switching,
  sensor acquisition, EEPROM writes, or identity replacement.

Controlled live acceptance demonstrated one automatic verification and normal
staggered scheduler recovery after both a plugin restart and a full eisy
reboot. The precise retained-handle invalidation followed by reconnection
within the eight-second window is covered by automated regression tests, not
by the final live acceptance sequence.

Accessibility and recovery Notices are historical records. An older probe,
refusal, or recovery result can remain visible after current state changes;
operators should use the current controller fields and newest Notice together.

Beta limitations remain: one identity-verified DS9490, one DS2409 level, no
parasite-powered DS18B20 conversion, no automatic discovery or measurement
retry, and operator-supplied calibration for solar irradiance.

## UD Plugin Store listing

**Short description**

Identity-bound DS9490 1-Wire temperature, humidity, voltage, and calibrated
solar integration for eisy.

**Full description**

eisy-onewire operates one Dallas/Maxim DS9490/DS9490R directly from Universal
Devices eisy using native FreeBSD libusb. It creates stable IoX nodes for
externally powered DS18B20 sensors, supports conservative one-level DS2409
branches, and supports explicitly identified DS2438 humidity, generic voltage,
and calibrated solar applications. Version 0.3.19 adds a default-disabled
operator authorization for unattended verification of an already accepted
adapter identity after startup. Unknown, substituted, or ambiguous adapters
fail closed. Automatic Refresh intent persists independently and resumes only
after a successful identity match. This beta does not manage multiple adapter
networks simultaneously.

## Suggested URLs

- Documentation: `https://github.com/mike-2545/eisy-onewire/blob/main/README.md`
- Installation: `https://github.com/mike-2545/eisy-onewire/blob/main/docs/installation.md`
- User guide: `https://github.com/mike-2545/eisy-onewire/blob/main/docs/user-guide.md`
- Troubleshooting: `https://github.com/mike-2545/eisy-onewire/blob/main/docs/troubleshooting.md`
- Bugs and sensor requests: `https://github.com/mike-2545/eisy-onewire/issues`
- Community support: `https://forum.universal-devices.com/`

## Plugin Store metadata

- Version: `0.3.19`
- Edition: `Beta`
- Archive: `https://github.com/mike-2545/eisy-onewire/releases/download/v0.3.19/eisy-onewire-0.3.19.tar.gz`
- SHA-256: `8DF6B15FB8AF024F5CBDCFEEFF8F741E3958EAB66D2B43DFF2602333F78FDF7B`
- License: MIT; retain `THIRD_PARTY_NOTICES.md`

No USB ownership rule, plugin slot, purchase option, or existing configuration
metadata changes are required solely for this update.

## Artifact

The live-accepted v0.3.19 archive contains 53 allowlisted files and has
SHA-256 `8DF6B15FB8AF024F5CBDCFEEFF8F741E3958EAB66D2B43DFF2602333F78FDF7B`.
Publish that exact archive; do not rebuild it from the public documentation
commit.

## Repository model

The public repository uses independent Git history and receives reviewed,
sanitized source snapshots. Development commits, credentials, private
infrastructure, adapter identities, and installation-specific acceptance
evidence are not included.
