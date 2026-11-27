# User guide

## Controller and sensor nodes

The **1-Wire Controller** represents one DS9490 adapter. Supported sensors are
children with stable addresses derived from their complete CRC-valid ROMs.
Changing a friendly name never changes that identity.

Sensor operations remain blocked until the connected adapter's CRC-valid
family-`0x81` ROM matches accepted inventory. Run **Verify Adapter Identity**
manually, or explicitly enable **Automatic Adapter Recovery**. Automatic
recovery is disabled by default and authorizes at most one bounded root-bus
identity Search ROM per recovery event. It cannot accept a new identity.

The controller shows:

- **Adapter Accessible**;
- **Automatic Refresh Enabled**;
- **Automatic Adapter Recovery**;
- **Temperature Unit**; and
- **Temperature Decimal Places**.

DS18B20 nodes show temperature, Current Reading Valid, and Desired Refresh
Interval. Configured DS2438 nodes show application-specific humidity, VAD/VDD,
or solar irradiance properties.

## Discover Devices

Select **Discover Devices** on the controller to run one bounded Search ROM
operation. There are no automatic retries. The result inventories every valid
ROM it can safely reach:

- `0x28`: registered as DS18B20 temperature nodes;
- `0x1F`: inventoried as DS2409 couplers;
- `0x26`: inventory-only until explicitly identified as an application; and
- `0x81`: the DS9490 identification device, not a temperature sensor.

Discovery is not run at startup or on a schedule. With a DS2409, the result may
list branches that were not scanned because the safe policy refused an
irreversible event-latch clear.

## Friendly names

Use the standard IoX rename action on a discovered sensor. Once registered, the
displayed name is owned by IoX and the operator. The plugin uses
`addNode(..., rename=False)` during ordinary reconciliation and restart, so it
does not need to observe the current name in order to preserve it. A ROM-keyed
plugin parameter remains an optional compatibility override:

```text
<complete-16-character-ROM>=Friendly Name
```

Do not omit the CRC byte or use an OWFS identifier with punctuation.
Both supported sensor families (`0x28` and `0x26`) accept legacy overrides.
Standard IoX Rename is preferred. Adding or changing a valid legacy override
is an explicit request for the plugin to rename that node once. Leaving the
override unchanged does not rename it again, and removing it leaves the current
IoX name untouched. The stable node address never changes.

## Manual refresh

- DS18B20: **Refresh Temperature**
- DS2438/HIH-4000: **Refresh Temperature and Humidity**
- DS2438 analog voltage: **Refresh Analog Voltages**
- DS2438 solar: **Refresh Solar Irradiance**

A refresh obtains the shared adapter lock, performs one bounded transaction,
validates CRC/data, and releases all USB resources. Branch reads additionally
select the validated path, restore the original coupler state, and verify that
restoration before publishing. There is no automatic retry.

## Automatic refresh

Each supported sensor has commands for Off, 30, 60, 120, and 300 seconds.
Intervals are independent. The controller's **Automatic Refresh Enable** is a
master switch; both a non-Off interval and the master are required.

The scheduler serializes all work through the same adapter lock, staggers
targets, uses monotonic deadlines, and performs no discovery. Failed reads set
GV0 false and retain the last valid ST/GV2 as stale.

Branch sensors also require `_ds2409_unattended_branch_access_policy`. This is
independent of both manual policies. Root-bus DS18B20 scheduling was live-tested.
Unattended DS2438 branch measurement passed controlled v0.3.16 acceptance.

## Automatic adapter recovery

Automatic Refresh expresses the operator's scheduling intent. Adapter
Accessible reports whether this process currently owns a live retained handle
whose family-`0x81` ROM exactly matches accepted inventory. Automatic Adapter
Recovery is a third, independent authorization.

When recovery is enabled, startup waits for bounded descriptor and permission
readiness and makes one identity attempt only if exactly one DS9490 is present.
A successful match retains that same native handle. Scheduling resumes only if
Automatic Refresh was already enabled; staggered due times and per-sensor
intervals remain unchanged. If Automatic Refresh was disabled, verification
does not enable it.

The same bounded recovery can follow invalidation of an already verified
handle when the adapter returns within that event's eight-second readiness
window. Multiple
adapters, a mismatch, missing or invalid family-`0x81` evidence, lock or
permission failure, and native errors fail closed with zero retry. A later
reconnect that was not observed during the bounded window requires manual
verification. This release intentionally does not maintain a permanent libusb
hotplug event loop.

The precise disconnect/invalidation/eight-second reconnect sequence is covered
by automated regression tests. Controlled live acceptance demonstrated
automatic recovery after plugin restart and a full eisy reboot; it did not
repeat that timing-sensitive reconnect sequence.

Enabling recovery authorizes root-bus signaling for identity only. It never
runs sensor discovery, switches branches, reads a sensor, writes EEPROM, or
replaces the accepted identity.

## Temperature presentation

Use controller commands to select Fahrenheit or Celsius and 0, 1, 2, or 3
decimal places. The choices apply globally to all managed temperature nodes.
The plugin preserves canonical Celsius internally and rounds published values
with decimal ROUND_HALF_EVEN. Presentation changes:

- use cached data only;
- do not access USB or change humidity;
- do not alter GV0; and
- persist across restart.

Some eisy-ui displays suppress trailing zeros; numeric profile precision and
the underlying value remain correct.

## Current versus stale

GV0 (**Current Reading Valid**) is true only after a fresh validated operation.
It becomes false after a failed read and after restart. ST and humidity may
remain visible as the last known valid values; GV0 distinguishes those cached
values from a current measurement. Never treat GV0=false as live data.

## DS2438/HIH-4000 configuration

A DS2438 measures temperature and voltages; it does not identify the external
analog humidity element. Discovery or restored inventory therefore creates an
application parameter set to `unassigned`; the device remains inventory-only
and performs no measurement until its circuit is physically identified.

For a Honeywell HIH-4000-series application, change the generated selector:

```text
_ds2438_application_<complete-CRC-valid-ROM>=hih4000
```

The plugin uses VAD as humidity output and VDD as sensor supply, applies the
Honeywell nominal transfer function and temperature compensation, and rejects
invalid/nonfinite voltages or results. It does not write DS2438 EEPROM.
Nominal sensor accuracy is a hardware specification, not the same as one-decimal
display precision.

New DS2438 nodes start with interval Off. The v0.3.15 live acceptance verified
one manual branch measurement with validated restoration. Exact VAD/VDD and
uncompensated RH were not retained in the original v0.3.15 acceptance evidence.
Unattended measurement subsequently passed the v0.3.16 controlled acceptance.

Changing the application back to `unassigned` revokes manual and scheduled
acquisition. An existing node is retained with its last validated values stale
rather than being destructively removed. Selecting `hih4000` again reconciles
the same stable address.

### Analog voltage and solar irradiance

Select `analog_voltage` only after identifying a DS2438 circuit whose VAD and
VDD values should be exposed directly. Select `solar_irradiance` only for a
verified linear analog irradiance sensor. The plugin then creates multiplier
and offset parameters for that ROM and uses:

```text
irradiance_w_m2 = VAD * multiplier_w_m2_per_volt + offset_w_m2
```

There is no default multiplier. Small negative results are retained as useful
zero-offset evidence, not silently clamped; nonfinite and out-of-range results
fail closed. Changing calibration marks cached irradiance stale and performs no
measurement. Application changes that would replace an existing IoX node
definition are blocked rather than deleting the node or breaking programs.

See the [DS2438 analog interface guide](ds2438-analog-interface.md) for the
manufacturer pinout and electrical limits, signal-conditioning guidance, and
the difference between live-tested applications and conceptual examples.

## Advanced DS2409 Permissions

These settings are for knowledgeable operators and intentionally remain plugin
configuration parameters rather than everyday IoX buttons.

### `_ds2409_event_flag_policy`

Controls manual discovery branch traversal. Default: `preserve`.

- `preserve`: stop before an operation that would clear asserted event latches.
- `acknowledge`: permit the explicitly invoked discovery to clear them after
  recording available evidence.

### `_ds2409_branch_read_event_flag_policy`

Controls manual branch sensor reads. Default: `preserve`. It is independent of
the discovery policy.

### `_ds2409_unattended_branch_access_policy`

Controls scheduled branch reads. Default: `disabled`.

- `disabled`: branch sensors remain manual-only.
- `preserve`: scheduled access may proceed only when restoration does not need
  an acknowledged event-latch clear.
- `acknowledge`: allows scheduled restoration that clears event latches after
  recording evidence.

**All-Lines-Off can irreversibly clear DS2409 event latches. They cannot be
restored.** Installations using event flags for alarms or Conditional Search
must not enable acknowledgment indiscriminately and should leave unattended
branch access disabled.
