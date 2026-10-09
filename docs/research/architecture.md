# YES RS-485 utility architecture handoff

## Scope and decisions

This is the minimal architecture for a Windows/Tkinter engineering utility that
supports Partlow P1160 ASCII and the documented MKS Series 275 RS-485 variant.
It is deliberately a one-process, two-thread design: Tk owns the main thread and
one `SerialWorker` thread owns the serial port. There is no asyncio loop,
database, service, plugin framework, or second background thread.

The selected `BusMode` and `ProtocolId` are immutable for the lifetime of an
open serial session. Changing either requires closing and reopening the port.
That is a safety boundary, not just a UI convention.

Partlow must initially use the operating system's native 7E1 configuration.
Do not implement a 7E1-over-8N1 transformation without a separate, hardware-
tested requirement. The existing adapter research explicitly says to configure
7E1 rather than assume user-space parity emulation is needed.

## 1. Thread ownership

- The Tk main thread owns every widget, view model, persisted configuration,
  dialogs, and user intent. It never opens, reads, writes, flushes, resets, or
  closes a serial object.
- Exactly one `SerialWorker` thread owns the pyserial `Serial` object from
  construction through close. It also owns the incremental framer, outstanding
  master transaction, slave-emulator live state, deadlines, and sequence
  counters.
- Communication is messages only. The UI posts immutable commands. The worker
  emits immutable notifications and traffic/frame records. No worker callback
  may invoke Tk code.
- The command queue is bounded so a runaway UI cannot consume unbounded memory.
  `submit()` reports `QueueFull` synchronously.
- Low-volume lifecycle/transaction notifications use a reliable queue. Raw
  traffic and decoded frames use bounded ring buffers; overwrites increment
  visible dropped-record counters. Serial I/O must never block waiting for the
  UI to render.
- The worker uses nonblocking serial reads (`timeout=0`) and a wake event. Its
  loop polls incoming bytes and commands and waits no longer than the nearest
  protocol deadline or a small cap (target 1 ms in active-slave mode, 5 ms in
  other modes). These are scheduler targets, not hard real-time guarantees.

## 2. Public interface and lifecycle

Suggested exact public surface (names may be imported directly by UI code):

```python
class BusMode(Enum):
    PASSIVE = "passive"
    MASTER = "master"
    SLAVE = "slave"

class ProtocolId(Enum):
    PARTLOW_P1160 = "partlow_p1160"
    MKS_275 = "mks_275"

@dataclass(frozen=True)
class SerialSettings:
    port: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: float
    xonxoff: bool = False
    rtscts: bool = False
    dsrdtr: bool = False

@dataclass(frozen=True)
class SessionSpec:
    mode: BusMode
    protocol: ProtocolId
    serial: SerialSettings
    response_timeout_s: float
    raw_capacity: int
    slave_profile: "SlaveProfile | None" = None

class SafetyArm(Enum):
    PLC_DISCONNECTED = "plc_disconnected"       # master
    SENSORS_DISCONNECTED = "sensors_disconnected" # slave

class SerialWorker:
    def start(self) -> None: ...
    def open(self, spec: SessionSpec,
             arm: SafetyArm | None = None) -> UUID: ...
    def close(self) -> UUID: ...
    def submit(self, operation: "MasterOperation") -> UUID: ...
    def replace_slave_profile(self, profile: "SlaveProfile") -> UUID: ...
    def cancel(self, transaction_id: UUID) -> UUID: ...
    def drain_notifications(self, limit: int = 200) -> list["Notification"]: ...
    def drain_traffic(self, after_seq: int, limit: int = 1000) -> "TrafficBatch": ...
    def drain_frames(self, after_frame_id: int,
                     limit: int = 1000) -> "FrameBatch": ...
    def stop(self, join_timeout_s: float = 2.0) -> None: ...
```

All mutating methods except `start`/`stop` enqueue a command and return its
command or transaction UUID immediately. Results arrive as notifications.
`open`, `close`, and `submit` never block the Tk thread.

Lifecycle is `NEW -> IDLE -> OPENING -> RUNNING -> CLOSING -> IDLE`, with
`FAULTED` reachable from an open state and `STOPPED` terminal. `start()` is
allowed once. `stop()` always asks the owner thread to cancel the transaction,
close the handle in `finally`, and exit; it is safe to call more than once.
Unexpected worker exceptions produce a `Faulted` notification and close the
handle before the thread returns to `IDLE` (or `STOPPED` during shutdown).

`replace_slave_profile` is legal only in a running slave session, and the
worker installs a validated immutable snapshot between frames. `submit` is
legal only in a running master session. `cancel` prevents later stages of a
compound operation; it cannot promise to retract bytes already written.

Representative notifications:

```python
StateChanged(command_id, old_state, new_state, session_summary)
CommandRejected(command_id, reason)
TransactionStarted(transaction_id, operation_summary)
TransactionCompleted(transaction_id, result)
TransactionFailed(transaction_id, failure_kind, detail)
SlaveStateChanged(request_frame_id, state_snapshot)
RawRecordsDropped(total_count)
FrameRecordsDropped(total_count)
Faulted(command_id_or_none, exception_summary)
```

Do not pass exception objects, serial handles, mutable dictionaries, or Tk
objects across the boundary.

## 3. Modes and safety invariants

The three modes describe a running session. `IDLE` is lifecycle state, not a
fourth bus mode.

### Passive Sniffer

- Opening requires no safety arm.
- Every application write route is disabled in both validation and the worker.
- `submit`, slave replies, startup probes, test frames, and automatic polling
  are rejected. Opening the port itself must not toggle RTS deliberately or
  enable `rs485_mode`; normal driver behavior remains a hardware caveat.
- It only reads, frames, decodes, records, and reports.

### Active Master

- `open` requires the non-persisted `SafetyArm.PLC_DISCONNECTED` value.
- The UI must display the active warning continuously, not only in a dialog.
- Only one master operation is outstanding. A second may be rejected as busy
  (simplest first version) rather than building an implicit unbounded queue.
- Every write originates from an accepted `MasterOperation`; there are no
  hidden probes. State-changing MKS commands and Partlow writes are labelled
  writes by the protocol layer, allowing the UI to require confirmation.
- Slave emulation is disabled.

### Active Slave

- `open` requires the non-persisted `SafetyArm.SENSORS_DISCONNECTED` value and
  a validated slave profile.
- `submit` is always rejected. The only legal transmission is a protocol reply
  generated from a complete, valid, addressed request received in this
  session. There are no unsolicited frames.
- Unknown address, invalid syntax, and protocol-defined ignored cases follow
  the device protocol. They must not fall through to a generic response.
- Live emulated state is worker-owned and resets from `SlaveProfile` on open.

Global invariants:

1. A live session's mode, protocol, and serial framing never mutate.
2. No code outside `SerialWorker` accesses the serial handle.
3. PASSIVE executes zero application writes.
4. MASTER and SLAVE cannot be active together.
5. An active safety arm is required on every active-mode open and is never
   restored from disk or carried across close/reopen.
6. All TX bytes are recorded before/at the write attempt; completion records
   distinguish accepted-by-OS from response success.
7. Closing or a serial fault terminates the pending transaction exactly once.

## 4. Request/response correlation

The worker serializes master operations. A protocol adapter compiles an
operation to one or more ordered steps:

```python
@dataclass(frozen=True)
class TransactionStep:
    tx: bytes
    expectation: "ResponseExpectation | None"
    timeout_s: float
    recovery_delay_s: float = 0.0

@dataclass(frozen=True)
class TransactionPlan:
    protocol: ProtocolId
    steps: tuple[TransactionStep, ...]
    is_write: bool
```

Focused algorithm:

```text
accept operation only when RUNNING/MASTER and no pending transaction
adapter.compile(operation) -> validated TransactionPlan
for each step:
    wait until protocol quiet/turnaround deadline
    record TX; serial.write(step.tx); flush as required
    if no response expected:
        wait recovery_delay (RST uses about 2 seconds), then advance
    else:
        set response deadline
        feed all RX bytes to raw log and incremental framer
        for each complete decoded frame:
            classification = adapter.match(expectation, frame)
            if MATCH_SUCCESS: advance
            if MATCH_NEGATIVE: fail with protocol-negative result
            if MATCH_MALFORMED_FOR_EXPECTED_PEER: fail or continue according
                to adapter policy, while retaining the frame
            if UNRELATED: retain/report and continue waiting
        on deadline: fail TIMEOUT
complete or fail parent transaction exactly once
```

Partlow matching checks start/type grammar, expected address, parameter, and
the expected terminal status (`A`, `I`, or `N`). An absolute write is a single
parent plan: Type 3 must receive the matching `I` response before Type 4 is
sent; Type 4 must receive the matching `A` response. Failure or cancellation
between stages means Type 4 is not sent.

MKS matching checks the `*`/`?` prefix and address where the documented reply
contains one. Because `RSA` and some error examples do not use a uniform
address layout, the expectation is command-specific rather than a global
fixed-offset parser. `RST` explicitly has no response; a timeout is not an
error for that step, and the worker enforces the approximately 2-second reset
recovery before another operation.

Received bytes identical to recently transmitted bytes are flagged
`POSSIBLE_ECHO` but are never silently deleted: SH-U22C echo behavior is not
verified. A request-shaped echo cannot satisfy a response expectation. This
preserves evidence while preventing the common false correlation.

## 5. Traffic and decoded-frame event model

Record byte traffic separately from decoded frames so split/coalesced OS reads
do not erase what actually happened:

```python
class Direction(Enum):
    RX = "rx"
    TX = "tx"

@dataclass(frozen=True)
class TrafficRecord:
    seq: int
    monotonic_ns: int
    wall_time_utc: datetime
    direction: Direction
    data: bytes
    mode: BusMode
    protocol: ProtocolId
    transaction_id: UUID | None
    flags: frozenset[str]   # e.g. WRITE_ATTEMPT, POSSIBLE_ECHO

@dataclass(frozen=True)
class FrameEvent:
    frame_id: int
    monotonic_ns: int
    direction: Direction
    raw_frame: bytes
    first_traffic_seq: int
    last_traffic_seq: int
    protocol: ProtocolId
    transaction_id: UUID | None
    decoded: "DecodedFrame | None"
    error: "ParseError | None"
```

`TrafficBatch` returns records plus `oldest_seq`, `newest_seq`, and
`dropped_before_seq`, so the raw view can visibly mark gaps. Use monotonic time
for ordering/durations and UTC wall time only for display/export. The simple
timeline consumes `FrameEvent`s and transaction/state notifications; the raw
table consumes `TrafficRecord`s. Raw bytes are always retained as bytes and
rendered as both escaped ASCII and hex.

## 6. Protocol boundaries

Keep four narrow concepts; do not make a protocol inheritance hierarchy larger
than this interface requires:

```python
class ProtocolAdapter(Protocol):
    id: ProtocolId
    def default_serial_settings(self) -> "SerialDefaults": ...
    def new_framer(self, role: BusMode) -> "IncrementalFramer": ...
    def compile(self, operation: MasterOperation,
                timeout_s: float) -> TransactionPlan: ...
    def decode(self, frame: bytes, direction: Direction,
               role: BusMode) -> DecodedFrame: ...
    def match(self, expected: ResponseExpectation,
              actual: DecodedFrame) -> "MatchResult": ...
    def respond(self, request: DecodedFrame,
                state: SlaveState) -> "SlaveDecision": ...
    def quiet_time_s(self, baudrate: int) -> float: ...
```

- Transport knows bytes, serial settings, timestamps, and deadlines only.
- Incremental framers know start/delimiter/resynchronization/max frame length,
  but not device values: Partlow ends at `*`; MKS ends at CR. Max length must be
  bounded to recover from noise.
- Codecs build and decode validated protocol frames and numeric fields. Partlow
  owns the five-character sign/decimal encoding. MKS owns command-specific
  spacing/templates and variable reply parsing.
- Adapter policy compiles compound operations, correlates replies, determines
  ignored/negative responses, schedules turnaround, and implements slave
  behavior against a typed state snapshot.

`SlaveDecision` is one of `Ignore`, `NoReply(state_after, recovery_s)`,
`Reply(data, earliest_send_ns, state_after)`, or
`ProtocolErrorReply(...)`. Partlow replies are not scheduled
until at least three character times after the complete request. MKS slave
timing uses the manual's baud-dependent `T` plus command processing delay (0 ms
for RD, 105 ms for FAC, 17 ms for other responding commands); RST returns
`NoReply` with reset state and an approximately two-second recovery. These
timings are best-effort on Windows and
must be surfaced as such.

The Partlow adapter must resolve one- versus two-digit addresses using the
configured/expected address and whole-frame grammar, never by assuming a fixed
address width. The MKS adapter must use two ASCII hexadecimal address digits.

## 7. Configuration model and persistence

Use a versioned JSON file in the normal per-user application configuration
directory (resolved with `platformdirs` only if that dependency is accepted;
otherwise `%APPDATA%/YES_RS485/config.json`). Write a temporary sibling, flush,
then atomically replace the destination. On invalid JSON, preserve the bad file
and load defaults with a visible warning.

```python
@dataclass(frozen=True)
class AppConfig:
    schema_version: int
    selected_protocol: ProtocolId
    preferred_port: "PortIdentity | None"
    protocol_profiles: dict[ProtocolId, "ProtocolProfile"]
    slave_profiles: dict[str, "SlaveProfile"]
    response_timeout_s: float
    raw_capacity: int
    ui: "UiPreferences"

@dataclass(frozen=True)
class PortIdentity:
    device: str             # e.g. COM7, a preference not a guarantee
    vid: int | None
    pid: int | None
    serial_number: str | None
    description: str | None

@dataclass(frozen=True)
class ProtocolProfile:
    serial: "SerialFormat"  # baud/data/parity/stop + false flow controls
    device_address: int
```

Validate all values on load and before open. Defaults are Partlow 4800/7E1 and
MKS 19200/8N1. Port enumeration is runtime data, not configuration truth; show
device, description, VID/PID, serial number, and location when available.
Never persist a safety arm, an “active enabled” checkbox, an open-port state,
or an accepted destructive-write confirmation. Adapter direction, isolation,
termination, polarity, and echo behavior remain user-visible “unverified”
facts rather than invented config defaults.

## 8. Minimal module layout

```text
pyproject.toml
src/yes_rs485/
    __main__.py             # composition root; launches Tk
    models.py               # enums, immutable commands/events/results
    config.py               # validation and atomic JSON persistence
    serial_worker.py        # sole serial owner and generic transaction loop
    protocols/
        base.py             # small ProtocolAdapter contract/common results
        partlow.py          # framer, codec, matcher, operations, slave policy
        mks275.py           # framer, codec, matcher, operations, slave policy
    ui/
        app.py              # root, polling, session controls/safety banner
        views.py            # passive/master/slave/settings/raw/timeline frames
tests/
    test_partlow.py
    test_mks275.py
    test_serial_worker.py   # fake serial; ownership/mode/correlation/timeouts
    test_config.py
```

Keep `views.py` together until its size demonstrably impairs maintenance.
Likewise, do not add repositories, controllers, event buses, dependency
injection containers, or separate threads for logging/polling.

Tests should prove at minimum: native serial settings; passive zero-write;
required active arms; close-on-fault; one outstanding request; Partlow numeric
vectors and Type 3/4 gating; MKS spacing/hex address/CR framing and RST no-
response behavior; negative ACK distinct from timeout; echo cannot correlate;
split/coalesced frames; bounded raw-buffer gap reporting; wrong-address frames
remain visible but do not satisfy a transaction; and no safety arm persistence.

## Explicit hard ambiguities

1. SH-U22C direction control, echo behavior, pinout, termination, isolation,
   and actual 7E1 support are unknown. Do not toggle RTS or enable pyserial
   `rs485_mode` until verified on the exact unit.
2. Neither a pyserial write nor Windows scheduling proves physical RS-485 line
   release timing. Partlow's three-character release requirement and precise
   slave-response timing require bench validation with the actual adapter.
3. The Partlow manual does not specify a host response timeout. Keep it a
   validated, visible setting and distinguish timeout from negative ACK.
4. MKS documentation is variant-specific, some reply layouts are irregular,
   and the PCS example is text-extraction damaged. Implement command-specific
   expectations only where verified and mark unsupported emulation explicitly.
5. A single converter is not guaranteed to observe both directions faithfully
   in passive mode. The application can report what Windows delivers, not
   promise analyzer-grade capture.
