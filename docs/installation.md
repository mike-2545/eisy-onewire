# Installation and rollback

## Before installing

eisy-onewire 0.3.17 is a public beta for Universal Devices eisy. Confirm that:

- eisy-ui/PG3x and IoX are current;
- a DS9490/DS9490R (USB VID `04fa`, PID `2490`) is attached;
- the 1-Wire sensors are externally powered where required;
- you have an eisy backup and have recorded existing plugin configuration; and
- you understand the [DS2409 event-latch warning](user-guide.md#advanced-ds2409-permissions)
  before using a coupler.

Do not install FreeBSD packages, create accounts, add global devd rules, or run
manual `chmod`/`chown` commands. The supported Plugin Store lifecycle creates
the isolated plugin account and asks UDX to render the packaged hardware rule.

The project is distributed under the MIT License. The archive includes both
`LICENSE` and `THIRD_PARTY_NOTICES.md`.

## Install from eisy-ui

1. Open the Plugin Store in eisy-ui and locate **eisy-onewire**.
2. Install it into an unused slot. For an update, use the existing plugin's
   supported Update/Reinstall action so its slot and NSID are retained.
3. Wait for the plugin to report **Connected**.
4. Confirm the 1-Wire Controller appears in IoX. If the profile was just
   installed, reopen the IoX Admin Console or refresh eisy-ui rather than
   reinstalling immediately.
5. Verify the installed device configuration is restricted to USB VID `04fa`,
   PID `2490`, `/dev/$cdev`, the dedicated plugin account, and mode `0600`.
6. On a first installation, disconnect and reconnect the DS9490 once after the
   device rule is installed. This lets FreeBSD create a new device node with
   the intended ownership.
7. Confirm **Adapter Accessible** is true. The normal startup detector opens and
   immediately closes the adapter; it does not claim an interface or send a
   1-Wire command.

## Safe first-use sequence

1. Leave Automatic Refresh disabled and all sensor intervals Off.
2. Select **Discover Devices** once.
3. Review the discovery Notice, including CRC results and any unscanned DS2409
   branches.
4. Rename registered DS18B20 nodes as desired.
5. Manually refresh one externally powered root-bus DS18B20 and confirm GV0 is
   true.
6. Review advanced DS2409 permissions before any branch operation.
7. Enable automatic refresh only after manual acceptance.

Discovery, configuration changes, and presentation commands do not run merely
because the plugin starts.

## Updating

Use the Plugin Store Update/Reinstall action for the existing slot. Before an
update, record:

- node addresses and names;
- parent relationships;
- sensor intervals and controller master state;
- temperature unit and precision;
- all three DS2409 policies; and
- the last valid readings and GV0 states.

After the update, verify the same state before enabling any sensor operation.
Cached readings are intentionally restored with GV0 false until a fresh read.

## Verification checklist

- Plugin is Connected under a dedicated plugin account.
- The DS9490 node is owned by that account with mode `0600` after reconnect.
- Runtime profile synchronization was acknowledged.
- Existing node addresses, names, and intervals are unchanged.
- Automatic Refresh remains at the recorded state.
- No unexpected discovery or branch operation appears in Notices/logs.
- Other installed plugins remain healthy.

## Rollback

Keep the previously accepted archive and its checksum before updating.

1. Disable Automatic Refresh.
2. Use the supported Local-store/Plugin Store lifecycle to reinstall the
   previous version in the same slot.
3. Do not edit PG3/eisy-ui databases or remove devd files manually.
4. Confirm the plugin reconnects, the previous profile is acknowledged, and
   node identities/configuration remain intact.
5. Treat cached readings as stale until manually validated.

If rollback cannot preserve the slot or profile, stop and collect logs before
deleting or reinstalling anything else.
