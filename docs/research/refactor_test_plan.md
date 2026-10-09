# Steering-correction test plan

This plan records the acceptance coverage needed while the UI/worker boundary is
steered toward tab-owned modes, protocol-aware passive capture, multi-device
master polling, and address-routed slave emulation.  It is intentionally a test
plan, not a production design prescription.

## Current coverage and entrypoints

The current tests are useful protocol/transport regression tests:

| Area | Existing coverage | Important gap |
| --- | --- | --- |
| `tests/test_serial_worker.py` | Fake serial ownership, native UART kwargs, passive zero-write, active `SafetyArm` checks, one pending transaction, split response, NACK vs timeout, echo visibility, bounded raw-ring gap, and close-on-read-fault | No tab routing, dual-protocol passive decoding, scheduler/poll-all/continuous operation, 12-device state, multi-address slave dispatch, or post-`SafetyArm` API |
| `tests/test_partlow.py` | Numeric vectors, split/coalesced framing, Type 3/4 write sequence, wrong-address matching, PV sentinels, probe NACK isolation, syntax validation, scan-table data, and direct slave decisions | Direct adapter tests do not prove worker-level routing or no-write behavior for unknown addresses |
| `tests/test_mks275.py` | Command spacing, hex addresses, CR framing, irregular/negative replies, RSA addressless handling, PCS, slave reply shapes, and reset no-reply recovery | No mixed Partlow+MKS passive stream or worker-level protocol selection |
| `tests/test_config.py` | Defaults, atomic round trip, malformed JSON fallback, and legacy single-Partlow-profile migration | Current model persists 11 Partlow PID entries; no canonical 12-device state or migration/validation for that state |
| UI/runtime | None | No tests for `YesRs485App`, notebook tabs, mode selection, worker construction, headless import, or `python -m yes_rs485`/`main()` behavior |

The runtime entrypoint is `src/yes_rs485/__main__.py`, which calls
`yes_rs485.ui.app.main()`.  `main()` constructs `YesRs485App`; the app lazily
creates a `SerialWorker`, builds `SessionSpec`, and periodically drains worker
notifications/traffic/frame batches.  A smoke test should exercise this
composition boundary with a fake worker and fake serial factory rather than
opening a real COM port or requiring a display.

## Focused acceptance tests

Use deterministic fake serial endpoints and an injectable clock.  Keep protocol
codec tests separate from worker/UI tests so failures identify the layer at
fault.

### Tab-driven modes

1. **Tab inventory and default.**  Construct the app with a fake worker and
   assert that Passive, Master, Slave, Raw Traffic, Timeline, and Settings tabs
   exist; the default selected tab/session is passive.  This can be a Tk smoke
   test under a virtual display, or a view-model test if the refactor exposes
   one.
2. **Mode isolation.**  Selecting each mode builds a `SessionSpec` with the
   tab's mode/protocol and only enables that tab's controls.  Switching tabs
   while closed must not open a port, submit a command, or mutate an existing
   open session.  Mode/protocol/serial identity stays fixed until close.
3. **Open/close lifecycle.**  A fake worker emits opening/running/closing/idle
   events; assert button/control state and that close clears the session handle,
   pending poll state, and any transient active-mode state.  Repeat open after
   close to catch stale mode/device selection.
4. **Entrypoint smoke.**  Import `yes_rs485.__main__` without side effects;
   invoke `main()` with a patched app class/mainloop and assert it returns 0.
   In a headless test, verify the Tk-unavailable diagnostic returns 2 instead of
   raising during import.

### Dual-protocol passive capture

5. **RX-only mixed stream.**  Open passive capture with a fake endpoint and feed
   a coalesced/split stream containing one valid Partlow frame and one valid MKS
   frame (plus unknown bytes).  Assert raw records preserve exact bytes and
   timestamps, both protocol decoders produce frame events, and no TX write is
   attempted.
6. **Protocol attribution.**  Passive frames carry the detected protocol (or a
   clearly documented `unknown`/raw result); a Partlow frame is never decoded by
   the MKS parser and vice versa.  Malformed/ambiguous bytes remain visible as
   raw evidence and do not fault the session.
7. **Passive hard guard.**  `submit`, automatic probes, continuous polling, and
   slave-reply paths are unavailable/rejected in passive mode.  Feed valid
   addressed requests for both protocols and assert the endpoint remains
   write-free.  Also assert no `rs485_mode`/RTS direction option is injected.

### 12-device state and persistence

8. **Canonical cardinality.**  Define and test the exact canonical device IDs
   (the correction must make clear whether these are 1..12 or another explicit
   set).  A default config has exactly 12 independent device records/addresses,
   each with a stable ID and initial address; no aliasing means changing one
   address/state cannot change another.
9. **Round trip and migration.**  Save/load preserves all 12 entries, custom
   addresses, protocol/profile values, and ordering.  A legacy 11-entry config
   migrates deterministically (or is rejected with a useful validation error),
   and missing/duplicate/out-of-range IDs cannot silently collapse to a single
   profile.
10. **Runtime reset/isolation.**  Opening a new master/slave session starts from
    the persisted 12-device snapshot; close/fault resets transient state.  A
    response from device N updates only N's state and cannot overwrite device
    N+1.

### Master poll-all and continuous operation

11. **Poll-all ordering.**  Start a poll-all request for the canonical device
    set and assert exactly one request per device, deterministic order, correct
    protocol address on every TX, and one response correlation per device.
    There must never be two outstanding transactions on the same bus.
12. **Poll-all completion/failure.**  A full successful cycle emits a per-device
    result and cycle-complete notification.  A timeout/NACK/protocol error is
    attributed to the affected device; the worker either continues according to
    the documented policy or stops with a visible cycle-failed result, without
    emitting a request for the wrong device.
13. **Continuous start/stop.**  Starting continuous polling schedules repeated
    cycles; stopping it prevents the next TX even if a response/deadline event
    arrives later.  Close, fault, mode switch, and protocol switch all cancel
    scheduling and leave no background timer/thread.  Repeated start is
    idempotent (no duplicate poll loops).
14. **Manual interaction.**  A manual master operation while poll-all is active
    follows the explicit policy (reject/busy or queue at a defined boundary).
    Test that policy and assert no starvation, unbounded queue, or interleaved
    bytes.  Polling is never available from passive or slave tabs.
15. **UI status.**  Fake worker notifications update the master tab with device,
    cycle, and failure status while Raw Traffic/Timeline continue to receive
    the same evidence.  A worker polling exception does not kill Tk's periodic
    callback.

### Slave address dispatch

16. **Matching address only.**  In a worker-backed slave session, feed valid
    requests for the configured address and for one or more other addresses.
    Only the matching request produces a reply; wrong-address frames are still
    recorded/decoded and produce no TX or state mutation.
17. **Both protocols and forms.**  Cover Partlow one- and two-digit address
    forms and MKS two-hex-digit addresses, including protocol-mismatched frames.
    Unknown address, malformed syntax, unsupported command, and protocol-defined
    no-reply cases are ignored or negatively replied exactly as the adapter
    contract specifies—never by a generic fallback.
18. **Stateful replies and timing.**  A valid write/commit sequence updates only
    the addressed device state; a subsequent read observes that value.  Reply
    bytes are recorded before write, honor the adapter's turnaround/recovery
    delay, and a second request cannot cause duplicate/late replies after close.
19. **Live profile update.**  Replacing a slave profile is atomic between frames:
    in-flight request/reply uses the old snapshot, the next request uses the new
    address/values, and a profile update in master/passive mode is rejected.

### Removal of `SafetyArm`

20. **Public API.**  Remove the `SafetyArm` import/type and the `arm` argument
    from the worker/app session-open path.  `open(spec)` is the sole lifecycle
    call for passive, master, and slave.  Tests should fail loudly if production
    modules still import or reference `SafetyArm` rather than retaining a silent
    compatibility shim.
21. **UI behavior.**  There is no arm checkbox, BooleanVar, arm-specific button
    gate, or arm persisted in config.  Mode-specific warning banners remain
    continuously visible, and opening a mode does not require a second hidden
    acknowledgement.  Close/fault/reopen must not carry any removed arm state.
22. **Persistence compatibility.**  Loading old JSON containing a safety-arm key
    ignores that transient key (or reports a documented migration warning) and
    never writes it back.  Existing serial/profile/address preferences remain
    intact.  Update old tests that assert active-arm rejection; replace them with
    no-arm open and warning-banner assertions.

## Compatibility hazards and release gates

* The current worker and UI require `SafetyArm`; removing it is an intentional
  API break.  Search all call sites, third-party adapters, examples, and tests
  for both `SafetyArm` and `arm=` before release.
* The current configuration describes **11** Partlow PID entries (PID 1..11,
  default addresses 2..12), while the corrected requirement says 12-device
  state.  Do not infer the twelfth ID from an address range.  Freeze the exact
  canonical IDs in a model-level test and provide an explicit migration.
* The current passive worker accepts one selected protocol.  Dual-protocol
  capture must not accidentally reuse the selected device profile's UART or
  make a passive capture write through a fallback adapter.
* The current UI uses a global mode combobox plus notebook views; tab-driven
  behavior must not leave the combobox as a second source of truth.  Test both
  keyboard/tab selection and persisted selected protocol/mode behavior.
* The current worker supports one outstanding transaction only and has no
  poll-all/continuous scheduler.  Polling must preserve that invariant and must
  cancel timers on close/fault/mode changes.
* Existing adapter direct tests do not prove worker routing.  Keep them, but add
  endpoint-level tests that inspect exact `writes`, traffic flags, frame IDs,
  and notifications.
* No UI tests are present today, and the checked-in virtual environment does
  not include pytest.  Add/use the project test runner (including Tk/headless
  support) before treating the acceptance suite as green.

## Minimum merge gate

Run the existing protocol/config/worker suite plus tests 1, 5, 7, 8, 11, 13,
16, 20, and 21.  For a release, require all tests above, a headless runtime
smoke, and a real-device bench check for serial turnaround/electrical behavior;
software tests cannot validate converter direction control, termination, or
physical RS-485 visibility.
