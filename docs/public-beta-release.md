# v0.3.16 public beta release kit

This file prepares public-facing text. It does not authorize publication,
repository visibility changes, a GitHub Release, Plugin Store publication, or
forum posting.

## GitHub repository description

Native DS9490 1-Wire plugin for Universal Devices eisy with DS18B20,
DS2409, and explicitly identified DS2438/HIH-4000 support.

## Suggested GitHub topics

`universal-devices`, `eisy`, `iox`, `pg3x`, `onewire`, `1-wire`, `ds9490`,
`ds2490`, `ds18b20`, `ds2409`, `ds2438`, `hih4000`, `freebsd`, `libusb`,
`home-automation`

## Release notes

Proposed release title: **eisy-onewire v0.3.16 — Public Beta**

### eisy-onewire 0.3.16 public beta

eisy-onewire connects a DS9490/DS9490R directly to Universal Devices eisy—no
Raspberry Pi or OWFS server required.

Highlights:

- Manual CRC-validated discovery and stable ROM-derived IoX nodes.
- Externally powered DS18B20 temperature sensors with manual and scheduled
  refresh.
- Fahrenheit/Celsius and 0--3 decimal-place presentation.
- One-level DS2409 branch discovery/read support with explicit safeguards for
  irreversible event-latch clearing.
- Explicit DS2438/Honeywell HIH-4000 temperature and relative-humidity support.
- Safer configuration reconciliation: invalid optional names no longer disable
  valid scheduler targets, and standard IoX renames remain operator-owned.
- Serialized USB operations, no automatic retries, cached/stale validity, and
  platform-managed restrictive USB permissions.

Beta limitations:

- One DS9490 adapter and one DS2409 level.
- No parasite-powered DS18B20 support.
- Unknown DS2438 applications remain inventory-only.
- Live scheduled acceptance covered one root/branch network and one physically
  identified HIH-4000 application; other topologies require local validation.
- Advanced DS2409 acknowledgment can irreversibly clear event latches.

Read the installation guide and leave Automatic Refresh disabled until manual
reads pass.

## UD Plugin Store listing

**Short description**

Native DS9490 1-Wire temperature and humidity integration for eisy.

**Full description**

eisy-onewire operates a Dallas/Maxim DS9490/DS9490R directly from Universal
Devices eisy using native FreeBSD libusb. It discovers CRC-valid 1-Wire ROMs,
creates stable IoX nodes for externally powered DS18B20 sensors, supports
manual and scheduled temperature refresh, and provides Fahrenheit/Celsius and
precision controls. One-level DS2409 branches are supported with conservative
event-latch safeguards. A discovered DS2438 can expose temperature and humidity
only when its analog application is explicitly identified as Honeywell
HIH-4000. Version 0.3.16 also isolates optional naming errors and preserves
operator-owned IoX names. It remains a public beta: one adapter, no parasite
power, no automatic discovery/retries, and advanced DS2409 branch access
requires careful local validation.

## Suggested URLs

- Documentation: `https://github.com/mike-2545/eisy-onewire/blob/main/README.md`
- Installation: `https://github.com/mike-2545/eisy-onewire/blob/main/docs/installation.md`
- User guide: `https://github.com/mike-2545/eisy-onewire/blob/main/docs/user-guide.md`
- Troubleshooting: `https://github.com/mike-2545/eisy-onewire/blob/main/docs/troubleshooting.md`
- Bugs/requests: `https://github.com/mike-2545/eisy-onewire/issues`
- Community support: `https://forum.universal-devices.com/`

These links will work for general users only after the repository is public.

## Friendly UD forum announcement

**Subject: Public beta: native DS9490 1-Wire plugin for eisy**

I’m inviting beta testers for eisy-onewire 0.3.16. The plugin connects a
DS9490/DS9490R directly to eisy through native FreeBSD libusb, without a
Raspberry Pi or OWFS host.

The beta supports externally powered DS18B20 temperature sensors, manual
discovery, manual/scheduled refresh, Fahrenheit/Celsius, display precision,
and one-level DS2409 branches. It also supports a DS2438 plus a physically
identified Honeywell HIH-4000 analog humidity sensor.

Please read the limitations before installing. DS2409 event-latch
acknowledgment is advanced and can irreversibly clear latch evidence; the safe
defaults remain enabled. Start with Automatic Refresh off and validate one
manual read at a time.

If you test it, please report your eisy/IoX versions, DS9490 model, sensor
families, topology, and redacted Notices/log excerpts in GitHub Issues. Please
do not post credentials or full home-automation exports. Requests for new
sensors are welcome when accompanied by a manufacturer datasheet and power/
topology details.

## Bug and sensor request template

Include:

- eisy-onewire, eisy-ui/PG3x, IoX, and eisy versions;
- install versus upgrade and plugin state;
- adapter model and USB VID/PID;
- sensor family and full ROM;
- root or DS2409 main/aux topology;
- relevant DS2409 policy values;
- exact command, timestamp, Notice, and redacted log excerpt;
- expected versus actual behavior; and
- whether it reproduced once without a retry.

For new hardware, also include the official datasheet, power mode, required
measurements, and whether a live unit is available for controlled testing.

## Artifact

The live-accepted v0.3.16 archive contains 47 allowlisted files and has SHA-256
`766E7FB4EC0ECD1289FE5F4CBE87ADAABECFE4519BBF231261AFC09230CA5F54`.
Public acceptance notes are stored outside the plugin package, so the public
archive remains byte-identical to the accepted artifact.

## Repository model

The authoritative private development repository is
`mike-2545/eisy-onewire-dev`. Public source is exported with independent
history to `mike-2545/eisy-onewire`. See the
[distribution workflow](public-distribution-workflow.md) and
[repository transition plan](repository-transition-plan.md). This kit does not
publish a GitHub Release, Plugin Store entry, or forum post.
