# Development history

eisy-onewire was developed in controlled phases, separating offline protocol
work, mocked tests, deployment, and explicitly approved hardware operations.
The private development repository retains detailed phase records and hardware
acceptance evidence. They are intentionally omitted from the public source
snapshot because they include private test-network identifiers. Ordinary users
should start with the [README](../README.md) and [user guide](user-guide.md).

## Milestones

- **0.2.0--0.2.4:** isolated plugin packaging, native FreeBSD libusb open/close,
  platform-managed USB permissions, USB notify-rule correction, status
  diagnostic, and deterministic-bytecode update handling.
- **0.2.5--0.2.9:** bounded Search ROM development, flexible timing, ROM-only
  reporting, FIFO/idle sequencing corrections, and successful five-device live
  discovery.
- **0.3.0:** explicit externally powered DS18B20 temperature diagnostic with
  scratchpad CRC validation.
- **0.3.1--0.3.4:** first IoX temperature node, runtime JSON profile
  synchronization/schema correction, and generic controller/child hierarchy.
- **0.3.5--0.3.8:** manual discovery, DS2409 topology inventory, atomic
  inventory persistence, status diagnostics, and explicit event-latch policy.
- **0.3.9--0.3.12:** verified manual branch temperature, user refresh controls,
  serialized root scheduler, and opt-in unattended branch access.
- **0.3.13:** standalone DS2438 native measurement diagnostic with topology
  evidence and cross-process exclusion.
- **0.3.14:** global Fahrenheit/Celsius and precision presentation with a
  serialized profile-update worker.
- **0.3.15:** explicitly identified DS2438/Honeywell HIH-4000 IoX integration,
  manual live acceptance, cached/stale state, and scheduler integration.

## Engineering records

The private records capture protocol reasoning, acceptance evidence, failure
corrections, and safety boundaries. They are not installation instructions.
The public documentation in this repository is the supported operational
reference. Maintainers can reproduce public releases with the
[distribution workflow](public-distribution-workflow.md).

## Current safety model

The project retains these principles from development:

- no global USB rules or manual permission changes;
- one serialized adapter transaction path;
- explicit manual discovery and zero automatic retries;
- CRC/data validation before publication;
- verified DS2409 restoration before branch values become current;
- three independent DS2409 permissions with safe defaults; and
- no DS2438 EEPROM writes.
