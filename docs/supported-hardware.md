# Supported hardware

Support status is deliberately divided into live-tested, automated-test-only,
and unsupported/untested categories.

## Live-tested on eisy

| Hardware | Status and scope |
| --- | --- |
| DS9490/DS9490R USB adapter (`04fa:2490`) | Native FreeBSD libusb open/close, status, Search ROM, and serialized transactions tested. |
| Externally powered DS18B20-compatible sensors (family `0x28`) | Multiple-device discovery, manual temperature, root scheduling, CRC, cached/stale behavior, and presentation tested. |
| DS2409 MicroLAN coupler (family `0x1F`) | One-level main-branch discovery, manual and scheduled branch temperature, event evidence, restoration, and readback tested. |
| DS2438 (family `0x26`) with physically identified Honeywell HIH-4000-series application | Manual and repeated scheduled branch temperature/humidity acquisition tested with state restoration, zero EEPROM writes, and zero automatic retries. |

The accepted network included five CRC-valid ROMs and both root/branch devices,
but those personal ROM values are not defaults and users need not reproduce the
same topology.

## Supported by implementation and automated tests

- DS2409 main and aux branch selection for one coupler level.
- Scheduled DS18B20 branch access when the separate unattended policy permits.
- DS2438 temperature, VAD/VDD acquisition, selector restoration, CRC handling,
  Honeywell nominal humidity conversion, and root/branch services.
- DS2438 scheduling through the shared serialized scheduler.
- Failure paths for timeouts, malformed responses, CRC errors, disconnects,
  lock contention, unsafe event-latch states, and restoration failure.

Automated-test support does not mean every physical combination has been tested.
The live scheduled test covered one adapter, one DS2409 level, and one
physically identified HIH-4000 application. Aux paths, other analog circuits,
and other physical networks retain automated-test-only status until separately
validated.

## Requirements and electrical assumptions

- DS18B20 sensors must be externally powered. Parasite-power conversion and
  strong pull-up are not supported.
- The 1-Wire network must be electrically sound and within normal cable/load
  limits for the adapter and sensors.
- HIH-4000 support assumes the DS2438 VAD input measures humidity output and
  VDD measures its supply. The analog element must be identified physically.
- One DS9490 is selected by the plugin. Multiple-adapter selection is not yet
  implemented.

## Inventory-only devices

- DS9490 family-`0x81` identification ROMs are reported but never registered as
  temperature sensors.
- Unknown DS2438 applications stay inventory-only.
- Other valid 1-Wire families may appear in inventory but receive no IoX node.

## Not currently supported

- Nested DS2409 couplers or arbitrary-depth topology traversal.
- Two DS9490 adapters in one plugin instance.
- Parasite-powered DS18B20 conversion/strong pull-up.
- DS2438 EEPROM calibration writes.
- Automatic discovery, automatic retry, or timing sweeps.
- HIH3600, HIH5030, HTM1735, or unidentified analog humidity circuits.
- DS2438 humidity calibration controls.
