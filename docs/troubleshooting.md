# Troubleshooting

## Plugin remains Starting or Disconnected

1. Confirm the installation was made through the supported Plugin Store/Local-
   store lifecycle, not a direct development path.
2. Check the plugin log for Python import or runtime-profile errors.
3. Confirm `udi_interface==3.4.9` is present in the isolated plugin runtime.
4. Do not install packages globally or create a manual runtime account.

## Adapter Accessible is false

Inspect the `ds9490_adapter_identity` and `adapter_identity_recovery` Notices
first. If Automatic Adapter Recovery is disabled, or a reconnect occurred
outside its bounded readiness event, use **Verify Adapter Identity**. Zero or
multiple adapters, a different family-`0x81` ROM, missing accepted evidence,
or a disconnected retained handle all fail closed; the plugin never falls back
to the sole remaining adapter.

These Notices form a history and an older accessibility probe, refusal, or
recovery result can remain visible after state changes. Treat the controller's
current **Adapter Accessible**, **Automatic Adapter Recovery**, and
**Automatic Refresh** values plus the newest Notice as authoritative. The
automatic reconnect readiness window is eight seconds; once it expires,
verification is manual.

- Confirm the USB device identifies as VID `04fa`, PID `2490`.
- Confirm the rendered platform rule targets `/dev/$cdev`, the dedicated plugin
  account, and mode `0600`.
- On first install, reconnect the adapter once after the rule is installed.
- Do not use manual `chmod`, `chown`, or a global devd rule.
- A busy adapter can indicate another process or standalone diagnostic holds it.
  Packaged hardware diagnostics require the accepted CRC-valid family-`0x81`
  ROM with `--adapter-rom`, validate installed inventory evidence, and use the
  same cross-process lock as the plugin. Stop scheduled activity before an
  operator-approved diagnostic; never bypass the lock or select by USB address.

## Profile commands or nodes are missing

Wait for the runtime profile acknowledgment, then refresh eisy-ui or reopen the
IoX Admin Console. A cached UI can temporarily show old commands even when the
profile is installed. Reinstall only after checking profile acknowledgments and
the persisted node inventory.

## Discovery finds only family `0x81`

The `0x81` ROM is the DS9490 identification device. Check cable power and the
external bus. If a DS2409 is present, inspect the discovery Notice for unscanned
branches and read DS2409 status before considering an acknowledgment policy.
Do not start with timing sweeps or retries.

## A branch operation is refused

The safe `preserve` policy may refuse an operation because returning the DS2409
to Off requires All-Lines-Off, which can irreversibly clear event latches. This
is expected fail-closed behavior. Review the status/evidence and the
[advanced permissions](user-guide.md#advanced-ds2409-permissions). Do not enable
`acknowledge` merely to silence the error.

## Temperature or humidity is visible but GV0 is false

The displayed value is the last validated cached reading and is stale. GV0 is
reset after restart and after a failed read. Run one authorized manual refresh
and resolve any Notice; do not assume the cached value is current.

## Automatic refresh does not run

Check all of the following:

- the sensor interval is not Off;
- controller Automatic Refresh is enabled;
- the adapter is accessible;
- the sensor has validated persisted topology;
- branch access is allowed by the independent unattended policy; and
- no other operation holds the shared adapter lock.

The persisted master may be enabled while the scheduler is disarmed because
adapter identity is unverified. This is intentional. Verify the adapter or
review the recovery Notice; do not toggle intervals to work around identity
failure. Automatic recovery never changes the saved master preference.

The scheduler performs no immediate retries and never performs discovery.

An invalid optional ROM-keyed friendly name has its own Notice and does not
invalidate otherwise safe scheduler targets. Correct or remove that entry.
Invalid application, topology, or DS2409 policy settings still fail closed.

## DS2438 remains inventory-only

That is the safe default. The plugin creates
`_ds2438_application_<ROM>=unassigned` from validated inventory. Physically
identify the analog application, then select `hih4000`, `analog_voltage`, or
`solar_irradiance`. A family code alone is
insufficient. Unsupported or misspelled models fail closed.

## Solar node stays stale or is not scheduled

Verify its ROM-specific multiplier is a finite positive W/m²/V value. The
generated `unassigned` placeholder deliberately authorizes no acquisition.
Invalid calibration affects only that sensor. An application-transition Notice
means an existing node definition would need replacement; the plugin refuses
to delete/recreate it automatically.

## Reporting a bug

Open a [GitHub issue](https://github.com/mike-2545/eisy-onewire/issues) with:

- plugin version and installation/update path;
- eisy, eisy-ui/PG3x, and IoX versions;
- adapter model and USB VID/PID;
- sensor family and complete ROM;
- root/DS2409 topology and relevant policy values;
- exact command, timestamp, Notice, and a short redacted log excerpt; and
- whether the problem reproduces once without retrying.

Remove credentials, tokens, private hostnames/IP addresses, unrelated node data,
and full configuration exports before posting.

For a new sensor request, include the manufacturer datasheet, family code,
power mode, topology, and desired IoX measurements. Do not attach proprietary
firmware or copyrighted manuals.
