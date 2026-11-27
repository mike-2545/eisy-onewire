# Supported hardware

Support status is deliberately divided into live-tested, automated-test-only,
and unsupported/untested categories.

## Live-tested on eisy

| Hardware | Status and scope |
| --- | --- |
| DS9490/DS9490R USB adapter (`04fa:2490`) | Native FreeBSD libusb open/close, status, Search ROM, and serialized transactions tested. |
| Externally powered DS18B20-compatible sensors (family `0x28`) | Multiple-device discovery, manual temperature, root scheduling, CRC, cached/stale behavior, and presentation tested. |
| DS2409 MicroLAN coupler (family `0x1F`) | One-level main-branch discovery, manual branch temperature, event evidence, restoration, and readback tested. |
| DS2438 (family `0x26`) with physically identified Honeywell HIH-4000-series application | One manual branch temperature/humidity acquisition tested with state restoration and zero EEPROM writes. |
| DS2438 analog environmental interface | VAD light response and VDD were characterized through OWFS; native eisy analog/solar publication awaits v0.3.17 acceptance. |

The accepted network included five CRC-valid ROMs and both root/branch devices,
but those personal ROM values are not defaults and users need not reproduce the
same topology.

## Supported by implementation and automated tests

- DS2409 main and aux branch selection for one coupler level.
- Scheduled DS18B20 branch access when the separate unattended policy permits.
- DS2438 temperature, VAD/VDD acquisition, selector restoration, CRC handling,
  Honeywell nominal humidity conversion, and root/branch services.
- DS2438 scheduling through the shared serialized scheduler.
- DS2438 generic VAD/VDD and calibrated linear solar applications using IoX
  volt (UOM 72) and watts/m² (UOM 74) units.
- Failure paths for timeouts, malformed responses, CRC errors, disconnects,
  lock contention, unsafe event-latch states, and restoration failure.

Automated-test support does not mean every physical combination has been tested.
Unattended DS2438 humidity and DS2409 branch measurements passed controlled
v0.3.16 acceptance. Analog/solar IoX integration is automated-test-only until
v0.3.17 hardware acceptance.

## Requirements and electrical assumptions

- DS18B20 sensors must be externally powered. Parasite-power conversion and
  strong pull-up are not supported.
- The 1-Wire network must be electrically sound and within normal cable/load
  limits for the adapter and sensors.
- HIH-4000 support assumes the DS2438 VAD input measures humidity output and
  VDD measures its supply. The analog element must be identified physically.
- DS2438 conversion codes span 0--10.23 V at 10 mV resolution; the data-sheet
  VAD accuracy specification is guaranteed only from 1.5 V. Low-output solar
  readings remain useful raw evidence but may have lower accuracy.
- One DS9490 is selected by the plugin. Multiple-adapter selection is not yet
  implemented.

## Inventory-only devices

- DS9490 family-`0x81` identification ROMs are reported but never registered as
  temperature sensors.
- Unknown DS2438 applications and unidentified humidity elements stay inventory-only.
- Other valid 1-Wire families may appear in inventory but receive no IoX node.

## Not currently supported

- Nested DS2409 couplers or arbitrary-depth topology traversal.
- Two DS9490 adapters in one plugin instance.
- Parasite-powered DS18B20 conversion/strong pull-up.
- DS2438 EEPROM calibration writes.
- Automatic discovery, automatic retry, or timing sweeps.
- HIH3600, HIH5030, HTM1735, or unidentified analog humidity circuits.
- Arbitrary humidity models or automatic solar calibration.
