# Whole-bus, twelve-device architecture

## Scope and fixed decisions

This is the smallest redesign that makes the application represent one physical
RS-485 bus rather than one selected protocol/device. It keeps the existing
process model: Tk on the main thread and exactly one `SerialWorker` thread that
owns exactly one pyserial handle. It adds no threads, timer framework, database,
service, or protocol abstraction. The existing Partlow and MKS adapters,
framers, compilers, matchers, and responders remain the protocol boundary.

The canonical logical inventory is ordered and exact:

```python
class DeviceId(str, Enum):
    GP = "gp"
    PID_1 = "pid_1"
    PID_2 = "pid_2"
    PID_3 = "pid_3"
    PID_4 = "pid_4"
    PID_5 = "pid_5"
    PID_6 = "pid_6"
    PID_7 = "pid_7"
    PID_8 = "pid_8"
    PID_9 = "pid_9"
    PID_10 = "pid_10"
    PID_11 = "pid_11"

CANONICAL_DEVICE_IDS: tuple[DeviceId, ...] = tuple(DeviceId)
```

`GP` is always MKS 275 and defaults to address 1. `PID_1` through `PID_11`
are always Partlow P1160 and default to addresses 2 through 12 respectively.
An address is editable independently for every device. A device's ID and
protocol are not editable; this prevents a saved configuration from silently
ceasing to describe the required machine.

There is no global protocol selection. Protocol is a property of a device.
There is no `SafetyArm` type, checkbox, persisted value, or `open(..., arm=)`
path. The passive/master/slave warning banners remain visible on their tabs.

## Public model

Keep `BusMode`, `ProtocolId`, `Direction`, `LifecycleState`, `SerialFormat`,
`SerialSettings`, `PortIdentity`, `UiPreferences`, `MasterOperation`,
`TransactionStep`, `TransactionPlan`, `DecodedFrame`, and `ParseError`. Remove
`SafetyArm`, `ProtocolProfile`, `SlaveProfile`, `selected_protocol`,
`protocol_profiles`, `partlow_addresses`, and named `slave_profiles` after the
schema-1 migration is implemented.

The new configuration/session values are:

```python
@dataclass(frozen=True)
class DeviceConfig:
    device_id: DeviceId
    protocol: ProtocolId
    address: int
    serial: SerialFormat
    fake_values: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class PassiveCaptureSettings:
    serial: SerialFormat = SerialFormat(9600, 7, "E", 1)
    response_pairing_window_s: float = 0.250

@dataclass(frozen=True)
class AppConfig:
    schema_version: int = 2
    preferred_port: PortIdentity | None = None
    devices: tuple[DeviceConfig, ...] = field(default_factory=default_devices)
    passive_capture: PassiveCaptureSettings = field(
        default_factory=PassiveCaptureSettings
    )
    response_timeout_s: float = 1.0
    raw_capacity: int = 10_000
    continuous_cycle_delay_s: float = 0.250
    ui: UiPreferences = field(default_factory=UiPreferences)

@dataclass(frozen=True)
class SessionSpec:
    mode: BusMode
    port: str
    devices: tuple[DeviceConfig, ...]
    listen_serial: SerialFormat
    response_timeout_s: float = 1.0
    raw_capacity: int = 10_000
    continuous_cycle_delay_s: float = 0.250

@dataclass(frozen=True)
class DeviceRuntimeState:
    device_id: DeviceId
    address: int
    values: Mapping[str, Any]
    pending_parameter: str | None = None
    pending_data: str | None = None
```

All mappings are copied to `MappingProxyType` in `__post_init__`. `AppConfig`
and `SessionSpec` validate that `devices` is in `CANONICAL_DEVICE_IDS` order,
contains each ID exactly once, and contains no extra ID. They also validate the
fixed ID/protocol relation, Partlow addresses 1..99, MKS addresses 0..15,
positive timeouts/capacities, and unique `(protocol, address)` keys. The same
numeric address may exist in different protocols because slave dispatch is by
both protocol and address.

`default_devices()` returns GP/MKS address 1 with
`{"pressure": "0.00E+00"}` and PID 1..11/Partlow addresses 2..12 with separate
`{"M": 0, "S": 0}` mappings. The GP default is 9600/8N1 and the shared
Partlow default is 9600/7E1. Each device retains its own serial field and
master mode honors it.

`SessionSpec.listen_serial` is the one format used to open/listen on the
handle. In passive and slave modes it comes from the selected capture/listen
profile; the worker does not require every device's saved profile to match it.
In master mode it is only the initial handle format; the worker changes the
same handle to the target device's format at transaction boundaries. A single
UART cannot listen to different baud/data/parity/stop formats simultaneously,
so passive/slave capture can only use the selected listen format.

The runtime state is a worker-owned
`dict[DeviceId, DeviceRuntimeState]`, initialized by copying all twelve
persisted `fake_values` on open. Master result extraction and slave response
decisions update this same map. A close/fault discards it; persisted fake values
change only when the UI explicitly saves a received `DeviceStateChanged`
snapshot or applies edited fake values. No state object or values mapping is
shared between devices.

Keep the runtime reducer deliberately small. On a successful Partlow read or
write-completion response, store the decoded value under its one-character
`parameter` (prefer the adapter's decoded numeric metadata value, otherwise
store `data`). On a successful MKS `RD` response, store its payload under
`"pressure"`. Other successful master commands update no fake value unless the
adapter already returns an explicit state/value field; their decoded result is
still reported. Slave mode continues to take the adapter's complete
`state_after`. Every applied reduction emits `DeviceStateChanged`; no generic
worker code interprets protocol wire syntax.

## Configuration migration

Schema 2 serializes only `preferred_port`, the ordered twelve `devices`,
`passive_capture`, timeout/capacity/cycle delay, and UI preferences. Loading a
schema-1 file performs one deterministic migration:

1. Build GP from the old MKS protocol profile. Use the values from the first
   MKS slave profile if present, otherwise the GP defaults.
2. Build each PID from the shared Partlow serial and the corresponding old
   `partlow_addresses[1..11]`. Copy the first Partlow slave profile's values to
   each PID if present, otherwise use the PID defaults.
3. Ignore `selected_protocol` and any safety-arm-like keys. Never write them
   back.
4. Validate the resulting exact inventory normally. Invalid IDs, duplicate
   `(protocol, address)` pairs, or out-of-range addresses cause the existing
   visible invalid-config fallback; they are never silently collapsed.

The migrated value mappings must be twelve independent copies. Save remains
the existing temporary-file, flush, atomic-replace path.

## Worker public interface

```python
class SerialWorker:
    def start(self) -> None: ...
    def open(self, spec: SessionSpec) -> UUID: ...
    def set_mode(self, mode: BusMode) -> UUID: ...
    def close(self) -> UUID: ...

    def submit(self, device_id: DeviceId,
               operation: MasterOperation) -> UUID: ...
    def poll(self, device_id: DeviceId) -> UUID: ...
    def poll_all_once(self) -> UUID: ...
    def start_continuous_poll(self) -> UUID: ...
    def stop_continuous_poll(self) -> UUID: ...

    def replace_fake_values(self, device_id: DeviceId,
                            values: Mapping[str, Any]) -> UUID: ...
    def cancel(self, transaction_id: UUID) -> UUID: ...
    def drain_notifications(self, limit: int = 200) -> list[Notification]: ...
    def drain_traffic(self, after_seq: int = 0,
                      limit: int = 1000) -> TrafficBatch: ...
    def drain_frames(self, after_frame_id: int = 0,
                     limit: int = 1000) -> FrameBatch: ...
    def stop(self, join_timeout_s: float = 2.0) -> None: ...
```

Every mutating method except `start` and `stop` enqueues one immutable command
and returns its command UUID immediately. Transaction/cycle UUIDs are created
by the owner thread and reported in notifications. Keep the current bounded
command queue, reliable `SimpleQueue` for notifications, bounded traffic/frame
rings, injected clock, and wake event.

`poll(device_id)` means the canonical health/value read for that device:
MKS `MasterOperation(kind="RD")`; Partlow
`MasterOperation(kind="read", parameter="M")`. `submit` covers all other
protocol-specific operations. Before compilation, the worker copies the
target address into the operation with `dataclasses.replace`; the UI never
supplies or edits a raw address in the master form.

`replace_fake_values` is legal only in running slave mode. It atomically
replaces one device's `values` snapshot between complete input frames, retaining
that device's address and adapter pending fields. It cannot change ID,
protocol, serial, or address; those require close, config save, and reopen.

## Lifecycle and tab-owned mode

Lifecycle remains `NEW -> IDLE -> OPENING -> RUNNING -> CLOSING -> IDLE`, with
`FAULTED` transiently closing the handle before `IDLE`, and terminal `STOPPED`.
`open(spec)` is the only open path and never requires a second acknowledgement.
The port, twelve-device snapshot, timeout, and capacities are immutable until
close. Mode alone may change while running through `set_mode`.

The notebook is the only source of mode intent:

- Selecting Passive Sniffer, Active Master, or Active Slave selects that
  authoritative mode. While closed it only changes the mode used in the next
  `SessionSpec`. While running it immediately enqueues `set_mode`.
- Selecting Settings, Raw Traffic, or Bus Timeline retains the last operational
  mode and sends no worker command.
- Remove the global Mode and Protocol comboboxes. Master has a device picker;
  settings/slave use twelve device cards/rows. Port and Open/Close remain
  session controls.

A running mode switch is an owner-thread boundary operation:

1. Stop continuous polling and clear all queued scheduled operations.
2. Fail the one in-flight master transaction once with `mode_changed`; discard
   a not-yet-written delayed slave reply.
3. Reset both incremental framers and recent-TX echo correlation. Do not clear
   raw/frame history or the twelve runtime states.
4. For passive and slave, reconfigure the same handle to `listen_serial`; for
   master, leave the handle idle until a target chooses its serial. Passive and
   slave listen framing is one explicit capture policy, not a requirement that
   all saved device profiles be equal.
5. Set the new mode and emit `ModeChanged`. No mode switch itself may transmit.

If reconfiguration/validation fails, close the handle, transition to `IDLE`,
and emit `ModeChangeFailed`; do not continue in the previous mode while a new
operational tab is selected. Re-selecting the active mode is an accepted
no-op. Close/fault/stop use the same cancellation cleanup.

## Master transaction queue and scheduler

There is still at most one on-wire `TransactionPlan`. The scheduler is just an
owner-thread `deque` plus the current `_Pending`; it is not another thread or
timer.

```python
@dataclass(frozen=True)
class _ScheduledOperation:
    device_id: DeviceId
    operation: MasterOperation
    cycle_id: UUID | None
    ordinal: int | None

@dataclass(frozen=True)
class _PollCycle:
    cycle_id: UUID
    continuous: bool
    total: int = 12
```

Rules:

- `submit` and `poll` are accepted only in running master mode when there is no
  pending transaction, queued cycle, or continuous schedule. The simplest
  manual-interaction policy is busy rejection; there is no unbounded manual
  queue.
- `poll_all_once` queues exactly twelve canonical polls in
  `CANONICAL_DEVICE_IDS` order. A repeated poll-all or continuous start while
  a schedule exists is rejected as busy.
- `start_continuous_poll` creates one twelve-device cycle. After its final
  result, the worker waits `continuous_cycle_delay_s`, then creates the next
  cycle. Calling it again is idempotent: accept the command but create no
  second loop.
- `stop_continuous_poll` clears the continuous flag and all not-yet-started
  scheduled operations. It does not retract bytes already written; the current
  transaction may finish/fail, but its completion/deadline must not enqueue or
  transmit a next item.
- For each scheduled item, reconfigure properties on the existing serial
  handle to the target device's `SerialFormat`, reset both framers if the
  format changed, compile with the adapter for that device's protocol/address,
  and run the existing step/deadline/recovery loop.
- Success, negative reply, protocol/decode error, compile error, and timeout all
  emit a per-device result and advance a poll cycle. Such a device failure never
  aborts the rest of a poll-all cycle. An actual serial read/write/reconfigure
  fault is the exception: it closes the session and cancels the cycle because
  further I/O is unsafe or impossible.
- A cycle completes after twelve terminal per-device results and reports
  success/failure counts. There are never two outstanding transactions and no
  bytes from different plans interleave.

Reconfiguring one open pyserial object is still one handle. Do not close/reopen
per device. Wait for the previous transaction/recovery to be terminal before
changing UART properties. Failure to assign/read back the requested format is
a serial fault and no request bytes are written for that item.

## Dual recognizers and raw evidence

Instantiate one Partlow framer and one MKS framer on open. Every RX byte chunk
is first appended exactly once to the raw traffic ring, then fed independently
to both framers in passive and slave modes. Master mode may do the same so
unrelated bus traffic remains visible; only the pending target protocol may
satisfy its matcher.

Maintain a separate `first_traffic_seq` anchor per framer. For every candidate
frame, call only that framer's adapter `decode`. A successful decode emits one
`FrameEvent` with that protocol. A delimited candidate that fails decoding may
emit a protocol-attributed `FrameEvent(error=ParseError(...))`; bytes that
produce no candidate remain visible in `TrafficRecord` and need no invented
"unknown protocol" frame. Resetting/overflow in one recognizer never resets the
other.

Do not feed output from one framer to the other, do not consume bytes globally
when one recognizes a frame, and do not merge the framer buffers. Deduplicate
only an exact repeated emission from the same protocol/span/raw tuple. If the
same raw span validly decodes as both protocols, retain both events and add
`AMBIGUOUS_PROTOCOL`; passive mode reports both and still writes nothing.

Passive mode has a hard owner-thread guard: `submit`, every polling command,
`replace_fake_values`, and slave reply code are rejected/disabled. No startup
probe exists. Opening never sets `rs485_mode` or deliberately toggles RTS. Thus
valid requests observed in passive mode cannot reach any TX path.

## Slave routing for all twelve devices

Slave mode uses the same two recognizers and adapters. Build an immutable
routing snapshot from `(protocol, address)` to `DeviceId` whenever slave mode
is entered; config validation guarantees uniqueness.

For each successfully decoded RX request:

1. Find `(decoded.protocol, decoded.address)` in the route map. Missing address,
   unknown address, parse failure, and protocol mismatch are recorded but do
   not call `respond`, transmit, or mutate state.
2. Convert that device's `DeviceRuntimeState` to the existing adapter
   `SlaveState`, call that protocol's existing `respond`, and apply only its
   returned `state_after` to the same device ID.
3. `Ignore` and `NoReply` follow adapter semantics. For a reply decision,
   preserve the existing turnaround/recovery deadline, record TX before write,
   and use the decoded protocol to decode the TX timeline event.
4. Permit at most one delayed slave reply. A second complete request received
   while one is pending remains visible but is ignored with
   `SlaveRequestIgnored(reason="reply_pending")`; it cannot replace or duplicate
   the first reply.

If one raw span validly routes to two protocol/device pairs, mark it ambiguous
and send no reply. This conservative rule prevents two responses to one
physical request. A frame's responder and state snapshot are captured before
response calculation, so `replace_fake_values` processed later affects the
next frame, never an in-flight decision.

## Events and records

Use these public immutable notifications (the `kind` constant may remain an
`init=False` field as it is today):

```python
StateChanged(command_id, old_state, new_state, session_summary)
ModeChanged(command_id, old_mode, new_mode)
ModeChangeFailed(command_id, requested_mode, reason)
CommandAccepted(command_id, action, related_id=None)
CommandRejected(command_id, reason)
ContinuousPollChanged(command_id, active)

TransactionStarted(transaction_id, device_id, operation_summary,
                   cycle_id=None, ordinal=None)
TransactionCompleted(transaction_id, device_id, result,
                     cycle_id=None, ordinal=None)
TransactionFailed(transaction_id, device_id, failure_kind, detail="",
                  cycle_id=None, ordinal=None)
PollCycleStarted(cycle_id, continuous, total=12)
PollCycleCompleted(cycle_id, continuous, succeeded, failed)

DeviceStateChanged(device_id, request_frame_id, state_snapshot)
SlaveRequestIgnored(frame_id, protocol, address, reason)
RawRecordsDropped(total_count)
FrameRecordsDropped(total_count)
Faulted(command_id_or_none, exception_summary)
```

The records become protocol-neutral at the raw-read level and device-aware at
the decoded level:

```python
@dataclass(frozen=True)
class TrafficRecord:
    seq: int
    monotonic_ns: int
    wall_time_utc: datetime
    direction: Direction
    data: bytes
    mode: BusMode
    protocol: ProtocolId | None = None
    device_id: DeviceId | None = None
    transaction_id: UUID | None = None
    flags: frozenset[str] = frozenset()

@dataclass(frozen=True)
class FrameEvent:
    frame_id: int
    monotonic_ns: int
    direction: Direction
    raw_frame: bytes
    first_traffic_seq: int
    last_traffic_seq: int
    protocol: ProtocolId
    device_id: DeviceId | None = None
    transaction_id: UUID | None = None
    decoded: DecodedFrame | None = None
    error: ParseError | None = None
    flags: frozenset[str] = frozenset()
```

RX `TrafficRecord.protocol/device_id` are always `None`: raw bytes have not yet
been recognized. TX records carry the target/reply identity. `FrameEvent`
gets a device ID when `(protocol, address)` resolves or when it belongs to the
pending master transaction; unknown addressed frames retain `None`.

## Non-negotiable invariants

1. Exactly one worker thread and one live serial handle exist; only that thread
   touches the handle.
2. The runtime/config inventory contains exactly the twelve canonical IDs in
   canonical order, with independent state mappings.
3. Device ID determines protocol. All master addressing and slave routing use
   the selected/resolved device, never a global protocol/address control.
4. Passive mode performs zero application writes under every input and command
   sequence.
5. There is at most one master transaction or one delayed slave reply on the
   bus. Master and slave TX paths cannot be active in the same mode.
6. Poll failures are attributed to one device and continue the cycle unless the
   serial handle itself faults.
7. Stop polling, mode change, close, fault, and stop cannot leave a future
   scheduled TX.
8. Slave state changes affect only the routed device. Unknown/ambiguous frames
   cannot mutate state or transmit.
9. Both recognizers see the same RX bytes independently; raw evidence is never
   discarded because framing/decoding failed.
10. Mode tabs are authoritative. Auxiliary tabs never change mode, and a failed
    running mode switch cannot leave the old worker mode active behind the new
    tab.

## Focused implementation tests

Keep all existing adapter vector/codec tests. Replace old single-protocol and
arm assertions with these focused worker/model/UI tests:

1. `default_devices()` has exactly canonical IDs, GP/MKS address 1, PID
   1..11/Partlow addresses 2..12, independent values mappings, and common
   default 9600/7E1. Missing/extra/reordered IDs, wrong fixed protocols,
   duplicate same-protocol addresses, and range errors fail validation.
2. Schema-2 round trip preserves every device address, serial profile, and fake
   value. Schema-1 migration creates exactly twelve independent records and
   ignores/removes selected protocol and safety-arm data.
3. Public imports contain no `SafetyArm`; `SerialWorker.open(spec)` accepts all
   three modes and an `arm=` keyword raises `TypeError`.
4. A fake endpoint proves one handle is constructed/opened once. Master polls
   two devices with different formats by reconfiguring that handle between
   terminal transactions, never by constructing another.
5. Passive split/coalesced Partlow and MKS bytes create correctly attributed
   frames from both recognizers, preserve exact raw records/spans, tolerate
   unknown bytes, and produce zero writes. Every TX-capable command is rejected.
6. `poll(device)` compiles the correct canonical operation/address for GP and a
   PID. The UI cannot override the address through `MasterOperation`.
7. Poll-all emits exactly twelve transactions in canonical order with one
   outstanding at a time. Success, NACK, protocol error, and timeout are
   attributed per device; each failure continues to the next device and the
   final counts are correct.
8. Continuous polling repeats only after a complete cycle and delay. Repeated
   start creates no duplicate scheduler. Stop during a pending response allows
   that result but prevents every later TX. Close/fault/mode switch do likewise.
9. Manual submit/poll and a second poll-all are visibly rejected while a cycle
   is active; the bounded command queue never becomes an operation backlog.
10. Slave open and running switch reject differing device UART formats and
    leave the handle closed. With a common format, Partlow one/two-digit and MKS
    requests route by `(protocol,address)` to the correct one of twelve states.
11. Wrong/unknown/malformed/ambiguous requests remain recorded and produce no
    TX/state change. A delayed-reply collision cannot replace the first reply.
12. Stateful Partlow write/commit and MKS state changes update only the routed
    device; a following read observes it. `replace_fake_values` is atomic
    between frames and rejected outside slave mode.
13. Operational notebook selections change desired/running mode; Settings,
    Raw Traffic, and Bus Timeline retain it. The toolbar has neither mode nor
    protocol selector nor arm checkbox. A failed running mode switch closes the
    session rather than leaving worker/UI mode disagreement.
14. Entrypoint/headless composition and raw/timeline draining still work with a
    fake worker; a worker exception does not kill Tk's periodic callback.

The release gate remains the complete existing protocol/config/worker suite
plus these tests and a real-device bench check. Software cannot prove converter
direction control, line release timing, electrical termination, or reliable
mixed-protocol capture on the actual hardware.
