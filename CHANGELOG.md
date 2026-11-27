# Changelog

All notable user-facing changes are summarized here. Detailed engineering
records are indexed in [docs/development-history.md](docs/development-history.md).

## 0.3.17 — DS2438 analog voltage and solar irradiance

- Added explicit `analog_voltage` and `solar_irradiance` DS2438 applications.
- Added IoX VAD/VDD and calibrated W/m² nodes using official UOMs 72 and 74.
- Added isolated, auto-populated solar multiplier/offset configuration with no
  universal calibration default and no configuration-triggered USB activity.
- Preserved stable identities and operator names; unsafe node-definition
  transitions fail closed without deleting or recreating nodes.
- Corrected VAD conversion validation to accept the documented 0--10 V ADC
  code range while documenting reduced guaranteed accuracy below 1.5 V.

## 0.3.16 — Configuration usability and reliability

- Isolated optional friendly-name errors so they cannot disable unrelated
  sensors or automatic refresh targets.
- Added backward-compatible family-`0x26` naming overrides while making
  standard IoX node Rename the preferred mechanism.
- Made registered node names operator-owned: stale PG3 database names and
  unrelated configuration saves cannot reverse an external IoX rename; only a
  newly added or changed legacy override issues an explicit rename.
- Added idempotent, inventory-derived DS2438 application selectors whose safe
  default is `unassigned`; application identification remains explicit.
- Preserved DS2409 authorization defaults and all hardware transaction paths.

## 0.3.15 — Public beta candidate

- Added an IoX temperature/humidity node for a discovered DS2438 whose attached
  analog application is explicitly identified as Honeywell HIH-4000.
- Added validated temperature, VAD/VDD, nominal humidity conversion,
  temperature compensation, persistence, and stale-state handling.
- Integrated DS2438 nodes with the existing manual refresh controls, global
  temperature presentation, serialized scheduler, adapter lock, and DS2409
  restoration safeguards.
- Preserved inventory-only behavior for unidentified family-`0x26` devices.
- Added public installation, user, hardware, troubleshooting, and beta-release
  documentation.
- Removed the personal test sensor from new-install default configuration.
- Added the MIT project license, retained third-party notices, and added a
  reproducible exporter for an independently historied public source snapshot.

## 0.3.14

- Added global Fahrenheit/Celsius selection and 0--3 decimal-place controls.
- Added a serialized worker for acknowledged runtime profile updates.
- Preserved canonical Celsius values, humidity independence, and GV0 semantics.

## 0.3.12

- Added separately authorized scheduled DS2409 branch temperature access.
- Preserved event evidence and verified branch restoration before publication.

## 0.3.11

- Added the first serialized automatic refresh scheduler for root DS18B20
  sensors with independent Off/30/60/120/300-second intervals.

## 0.3.10

- Added fixed per-sensor interval commands and the controller master enable.

## 0.3.9

- Added safe manual DS18B20 temperature refresh behind a DS2409 branch.

## 0.3.8

- Added read-only DS2409 status and explicit event-latch acknowledgment policy.

## 0.3.6--0.3.7

- Added one-level DS2409 topology discovery and atomic inventory persistence.

## 0.3.5

- Added manual ROM-based discovery and automatic DS18B20 node registration.

## 0.3.1--0.3.4

- Added IoX JSON profiles, the controller node, reusable DS18B20 child nodes,
  stable addresses, friendly names, and manual refresh.

## 0.2.0--0.3.0

- Established deterministic packaging, platform-managed USB permissions,
  native FreeBSD libusb transport, DS2490 status/Search ROM support, and the
  validated externally powered DS18B20 diagnostic.
