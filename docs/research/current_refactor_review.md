# Current refactor review

Review date: 2026-10-09. This is a repository-evidence-only review of the
current implementation against the steering correction: one whole bus with 12
devices, tab-driven mode, no global protocol/mode selectors, no `SafetyArm`
gate, passive dual recognizers, continuous master poll-all, and 12 slave
profiles. No implementation files were changed for this review.

## Current behavior

- `models.py` defines three runtime modes (`PASSIVE`, `MASTER`, `SLAVE`) and
  two protocol IDs (`PARTLOW_P1160`, `MKS_275`) (lines 17-26). A
  `SessionSpec` carries exactly one mode, one protocol, one serial profile, and
  at most one `slave_profile` (339-355). `SafetyArm` is a first-class enum
  with PLC- and sensor-disconnected values (44-47).
- `AppConfig` still has a persisted `selected_protocol` plus
  `protocol_profiles` (one profile per protocol), while also carrying a shared
  Partlow serial profile and `partlow_addresses` (358-376). Defaults create 11
  Partlow PID entries (PID 1..11, addresses 2..12) and two named slave
  profiles, one Partlow and one MKS (`config.py`, 43-71). The model validates
  the PID range as 1..11 (models.py, 403-423), so this is not a 12-device
  collection.
- Configuration persistence serializes `selected_protocol`, both protocol
  profiles, the 11-address Partlow map, passive capture settings, and named
  slave profiles (`config.py`, 122-159). The atomic write/load path and legacy
  Partlow migration are useful, but the persisted shape remains protocol-first
  rather than a unified bus/device inventory.
- The toolbar exposes both a Mode combobox and a Protocol combobox
  (`ui/app.py`, 452-500). Mode selection also selects the corresponding
  notebook page (`ui/app.py`, 576-601); protocol selection globally changes
  the master address/profile and slave form (`ui/app.py`, 603-653). The
  notebook nevertheless contains separate Passive Sniffer, Active Master,
  Active Slave, Settings, Raw Traffic, and Bus Timeline tabs (532-549).
- Opening an active session is explicitly gated twice: the UI requires the
  acknowledgement checkbox (`ui/app.py`, 778-787) and maps it to a
  `SafetyArm`; `SerialWorker._validate_arm` rejects active opens without the
  matching arm (serial_worker.py, 1148-1163). The worker's public `open` also
  accepts `arm` (404-407). `README.md` and `docs/research/architecture.md`
  describe this same gate.
- Passive setup uses a separate `PassiveCaptureSettings` with a descriptive
  `mixed_raw` strategy and pairing-window field (models.py, 304-335), but
  `ui/app.py` builds passive `SerialSettings` as one 8N1 stream (687-711) and
  `SessionSpec` still includes one selected protocol (713-726). Worker open
  selects one adapter and one framer (`serial_worker.py`, 557-580); frame
  decode then calls that adapter only (`serial_worker.py`, 794-827). The
  pairing window is persisted/UI-visible but has no worker or protocol use
  (`rg` finds it only in config, models, and views/app).
- Master UI exposes one manual operation (`read`, `write`, or `raw`), one
  address, one parameter/value, and a Send button (`ui/views.py`, 166-231).
  `SerialWorker` allows one pending transaction and rejects another while it
  is outstanding (`serial_worker.py`, 605-627). There is no poll list,
  scheduler, poll-all command, continuous-run state, or device-health result;
  the only repeated loop is the transport/UI polling loop.
- Slave UI exposes one address and one editable profile form
  (`ui/views.py`, 234-322). `SessionSpec.slave_profile`, worker
  `_slave_state`, `replace_slave_profile`, and adapter `respond` are all
  singular (`models.py`, 339-355; `serial_worker.py`, 419-422, 629-648,
  877-915). `AppConfig.slave_profiles` can store arbitrary named entries, but
  the defaults contain only two and the UI loads/edits the first profile for
  the globally selected protocol (`ui/app.py`, 416-450).

## Key interfaces and reuse seams

- Keep the single-owner transport architecture. `SerialWorker` owns the
  serial handle, command queue, lifecycle notifications, bounded raw/frame
  rings, deadlines, echo marking, and close-on-fault behavior
  (`serial_worker.py`, 314-500 and 930-1099). `TrafficRecord` and `FrameEvent`
  are immutable cross-thread records (`models.py`, 532-559).
- Reuse `TransactionPlan`/`TransactionStep`, `DecodedFrame`,
  `ResponseExpectation`, and `MatchResult` as the low-level transaction
  vocabulary (`models.py`, 479-524; `protocols/base.py`, 49-76). The adapter
  contract already isolates `new_framer`, `compile`, `decode`, `match`,
  `respond`, and timing (`protocols/base.py`, 130-145).
- Reuse both protocol implementations as codecs/recognizers. Partlow has an
  incremental `L...*` framer and documented Type 1-4 compile/decode/respond
  behavior (`protocols/partlow.py`, 43-84, 318-475). MKS has an incremental
  `#`/`*`/`?` through CR framer and command/response handling
  (`protocols/mks275.py`, 37-76, 224-346). Their parsers can be composed in a
  passive dual-recognizer layer without changing the documented grammar.
- Reuse the raw/timeline views and worker drain APIs. The UI already drains
  bounded traffic and frame batches with explicit gap accounting
  (`ui/app.py`, 899-932; `serial_worker.py`, 429-465), and the views render
  bytes plus decoded summaries without owning I/O (`ui/views.py`, 764-932).
- Reuse config's validated immutable values, defaulting, legacy migration, and
  atomic save (`config.py`, 162-299). The data shape needs a bus/device
  migration, but its error handling and file safety are directly applicable.

## Prioritized gaps for the correction

### P0 — change the runtime/config contract

1. Make the bus the session boundary. Replace the one-protocol
   `SessionSpec`/`AppConfig.selected_protocol` assumption with one bus serial
   configuration and a 12-device inventory whose entries carry the protocol,
   address, enabled/role/profile data needed by that device. The existing
   Partlow 11-entry map plus separate MKS profile is not that inventory.
2. Make mode tab-driven. Remove the global Mode and Protocol comboboxes and
   stop using a global selected protocol to drive all views. The mode tab should
   select the active workflow; protocol should be a property of each device or
   recognizer/profile where needed. The notebook, raw view, timeline, and
   settings components are reusable.
3. Remove `SafetyArm` from the open contract and active-open validation,
   including the toolbar checkbox and the corresponding tests/docs. Keep any
   required warning copy as UI messaging, but do not make a user acknowledgement
   a worker gate.

### P1 — implement the three workflows on the bus

4. Passive mode needs two independent recognizers on the same captured byte
   stream: Partlow framing/decoding and MKS framing/decoding, with raw traffic
   retained regardless of recognition. The current singular `_framer`, selected
   adapter, and unused pairing-window setting are the main seams to replace.
5. Master mode needs a worker-owned poll-all/continuous scheduler over the 12
   configured devices, with serialized bus transactions, per-device protocol
   compilation/matching, and visible progress/results. The current manual
   `submit` plus one `_pending` transaction can remain as a primitive but does
   not supply scheduling or continuous operation.
6. Slave mode needs 12 profile/state slots and address/protocol dispatch. The
   current singular `slave_profile`, `_slave_state`, `replace_slave_profile`,
   and one-form UI must become a collection while preserving each adapter's
   `SlaveState` and reply timing rules.

### P2 — update verification and cleanup

7. Add tests for the unified 12-device config/migration, tab-driven selection,
   no-arm open, dual passive recognition (including mixed/coalesced input),
   poll-all ordering/continuous stop, and 12-profile slave dispatch. Existing
   tests cover native serial settings, passive no-write, safety-arm rejection,
   one outstanding transaction, echo handling, timeouts, and protocol grammar;
   they therefore encode the superseded contract (`tests/test_serial_worker.py`,
   156-257; `tests/test_config.py`, 9-58).
8. Reconcile `README.md` and `docs/research/architecture.md`, which currently
   present active safety arms, a selected protocol, and one outstanding manual
   master operation as requirements. Remove or revise compatibility aliases only
   after callers/tests have moved to the bus/device model.

## Verification note

The checkout contains no UI tests. The available virtual environment does not
have `pytest` installed, so this review records test-file evidence rather than
claiming a test run.
