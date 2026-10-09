# YES RS-485 utility architecture handoff

## Scope and decisions

This is the minimal architecture for a Windows/Tkinter engineering utility that
operates one physical RS-485 bus containing the documented Partlow P1160 ASCII
and MKS Series 275 RS-485 variants.  The canonical inventory is always twelve
devices: `GP` (MKS) plus `PID 1` through `PID 11` (Partlow).  Device identity
selects the protocol; there is no selected global protocol and no one-device
or one-profile application model.

It is deliberately a one-process, two-thread design: Tk owns the main thread
and one `SerialWorker` thread owns one serial handle.  There is no asyncio loop,
database, service, plugin framework, or second background thread.

The operational notebook tabs are the mode authority.  `Passive`, `Master`,
and `Slave` are `BusMode` values and may be switched on a running session.
The worker cancels in-flight work, resets framers, and (for listening modes)
re-applies the one selected listen UART format at the mode boundary.  Master
retunes that same handle to the selected device's serial profile for each
transaction.

The application defaults are MKS/GP 9600/8N1 and the shared Partlow PID-bus
profile 9600/7E1.  Passive and Slave use one explicit listen format (default
9600/7E1), so one UART cannot hear 7E1 and 8N1 simultaneously.  Partlow must
use the operating system's native 7E1 configuration; do not implement a
7E1-over-8N1 transformation without a separate, hardware-tested requirement.
The SH-U22C adapter's actual 7E1 support remains unverified.

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

Suggested public surface (names may be imported directly by UI code):

```python
class BusMode(Enum):
    PASSIVE = "passive"
    MASTER = "master"
    SLAVE = "slave"

class ProtocolId(Enum):
    PARTLOW_P1160 = "partlow_p1160"
    MKS_275 = "mks_275"

class DeviceId(Enum):
    GP = "gp"
    PID_1 = "pid_1"
    # ... through PID_11

@dataclass(frozen=True)
class SerialFormat:
    baudrate: int
    bytesize: int
    parity: str
    stopbits: float
    xonxoff: bool = False
    rtscts: bool = False
    dsrdtr: bool = False

@dataclass(frozen=True)
class DeviceConfig:
    device_id: DeviceId
    protocol: ProtocolId       # fixed by device ID: GP=MKS, PID 1..11=Partlow
    address: int
    serial: SerialFormat
    fake_values: Mapping[str, object]

@dataclass(frozen=True)
class SessionSpec:
    mode: BusMode
    port: str
    devices: tuple[DeviceConfig, ...]  # complete GP + PID 1..11 inventory
    listen_serial: SerialFormat         # one UART format for Passive/Slave
    response_timeout_s: float
    raw_capacity: int
    continuous_cycle_delay_s: float = 0.250

class SerialWorker:
    def start(self) -> None: ...
    def open(self, spec: SessionSpec) -> UUID: ...
    def set_mode(self, mode: BusMode) -> UUID: ...
    def close(self) -> UUID: ...
    def submit(self, device_id: DeviceId, operation: "MasterOperation") -> UUID: ...
    def poll(self, device_id: DeviceId) -> UUID: ...
    def poll_all_once(self) -> UUID: ...
    def start_continuous_poll(self) -> UUID: ...
    def stop_continuous_poll(self) -> UUID: ...
    def replace_fake_values(self, device_id: DeviceId,
                            values: Mapping[str, object]) -> UUID: ...
    def cancel(self, transaction_id: UUID) -> UUID: ...
    def drain_notifications(self, limit: int = 200) -> list["Notification"]: ...
    def drain_traffic(self, after_seq: int, limit: int = 1000) -> "TrafficBatch": ...
    def drain_frames(self, after_frame_id: int,
                     limit: int = 1000) -> "FrameBatch": ...
    def stop(self, join_timeout_s: float = 2.0) -> None: ...
```

All mutating methods except `start`/`stop` enqueue a command and return its
command or transaction UUID immediately. Results arrive as notifications.
`open`, `close`, mode changes, and poll requests never block the Tk thread.

Lifecycle is `NEW -> IDLE -> OPENING -> RUNNING -> CLOSING -> IDLE`, with
`FAULTED` reachable from an open state and `STOPPED` terminal. `start()` is
allowed once. `stop()` always asks the owner thread to cancel the transaction,
close the handle in `finally`, and exit; it is safe to call more than once.
Unexpected worker exceptions produce a `Faulted` notification and close the
handle before the thread returns to `IDLE` (or `STOPPED` during shutdown).

`replace_fake_values` is legal only in a running slave session, and the worker
installs the selected device's validated values between frames. `submit` and
the poll commands are legal only in a running master session. `cancel`
prevents later stages of a compound operation; it cannot promise to retract
bytes already written.

Representative notifications:

```python
StateChanged(command_id, old_state, new_state, session_summary)
ModeChanged(command_id, old_mode, new_mode)
ModeChangeFailed(command_id, requested_mode, reason)
CommandRejected(command_id, reason)
TransactionStarted(transaction_id, operation_summary)
TransactionCompleted(transaction_id, result)
TransactionFailed(transaction_id, failure_kind, detail)
PollCycleStarted(cycle_id, continuous, total=12)
PollCycleCompleted(cycle_id, continuous, succeeded, failed)
DeviceStateChanged(device_id, request_frame_id, state_snapshot)
SlaveRequestIgnored(frame_id, protocol, address, reason)
RawRecordsDropped(total_count)
FrameRecordsDropped(total_count)
Faulted(command_id_or_none, exception_summary)
```

Do not pass exception objects, serial handles, mutable dictionaries, or Tk
objects across the boundary.

## 3. Modes and bus invariants

The three modes describe the current operation of one running bus session.
`IDLE` is lifecycle state, not a fourth bus mode.  The active notebook tab is
the mode authority and can request a mode change without opening another port.

### Passive Sniffer

- Both protocol adapters are active for recognition: received bytes are fed to
  Partlow and MKS framers/decoders and matching frames are reported with their
  device when the address is known.
- Every application write route is disabled in validation and in the worker.
  Polls, startup probes, test frames, slave replies, and other TX commands are
  rejected.  Opening the port itself must not deliberately toggle RTS or enable
  `rs485_mode`; normal driver behavior remains a hardware caveat.
- It only reads, frames, decodes, records, and reports.  The one selected
  listen UART format is used for the entire capture; a UART cannot hear 7E1 and
  8N1 at the same time.

### Active Master

- The Master tab selects a device for **Poll**, or starts **Poll All Once** or
  **Continuous Scan** over all twelve devices in canonical order.
- Before each device transaction the worker applies that device's native
  serial profile and protocol adapter to the shared handle.  A poll cycle is
  therefore a sequence of twelve per-device transactions, not one global
  protocol request.
- Every TX originates from an accepted poll or explicit `MasterOperation`; the
  worker does not send hidden probes.  State-changing MKS commands and
  Partlow writes remain labelled writes in the protocol layer.
- Only one wire transaction is in flight at a time, while Poll All and
  Continuous Scan queue the remaining devices and report cycle progress.
  Slave emulation is disabled in this mode.

### Active Slave

- The Slave tab exposes twelve independent fake-value profiles, one for every
  canonical device.  The worker owns live runtime values and can replace one
  device's values between frames.
- `submit` and poll commands are rejected.  The only legal transmission is a
  protocol reply generated from a complete, valid, addressed request received
  in this session; there are no unsolicited frames.
- Unknown address, invalid syntax, and protocol-defined ignored cases follow
  the device protocol and never fall through to a generic response.
- Slave uses the one explicit listen UART format selected for Passive capture.
  The single UART limitation means it cannot listen for GP/MKS 8N1 and
  Partlow 7E1 traffic simultaneously.

Global invariants:

1. The session contains exactly twelve devices in canonical order: GP plus
   PID 1 through PID 11.  Device identity fixes its protocol (GP=MKS,
   PID=Partlow); there is no selected global protocol.
2. No code outside `SerialWorker` accesses the one serial handle.
3. PASSIVE executes zero application writes.
4. MASTER and SLAVE behavior is selected by the current operational mode; a
   mode switch cancels pending work and resets framing before continuing.
5. All TX bytes are recorded before/at the write attempt; completion records
   distinguish accepted-by-OS from response success.
6. Closing, a mode change, or a serial fault terminates pending work exactly
   once.

## 4. Request/response correlation

The worker serializes master operations.  A manual Poll targets one device;
Poll All and Continuous Scan enqueue the same per-device operation for each of
the twelve devices.  Before compiling a queued item, the worker applies that
device's serial format to the shared handle.  A protocol adapter then compiles
the operation to one or more ordered steps:

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
accept operation only when RUNNING/MASTER and the requested device is known
queue Poll All/Continuous items in canonical GP, PID 1..11 order
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
    protocol: ProtocolId | None
    device_id: DeviceId | None
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
    device_id: DeviceId | None
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
directory (`%APPDATA%/YES_RS485/config.json`). Write a temporary sibling,
flush, then atomically replace the destination. On invalid JSON, preserve the
bad file and load defaults with a visible warning. The current persisted format
is schema 2.

```python
@dataclass(frozen=True)
class AppConfig:
    schema_version: int = 2
    preferred_port: "PortIdentity | None"
    devices: tuple[DeviceConfig, ...]       # exactly GP + PID 1..11
    passive_capture: "PassiveCaptureSettings"  # one listen UART
    response_timeout_s: float
    raw_capacity: int
    continuous_cycle_delay_s: float
    ui: "UiPreferences"

@dataclass(frozen=True)
class PortIdentity:
    device: str             # e.g. COM7, a preference not a guarantee
    vid: int | None
    pid: int | None
    serial_number: str | None
    description: str | None

@dataclass(frozen=True)
class PassiveCaptureSettings:
    serial: "SerialFormat"                  # default 9600/7E1
    response_pairing_window_s: float
```

Each `DeviceConfig` stores `device_id`, its fixed `protocol`, address, native
serial profile, and `fake_values`.  The defaults are MKS/GP 9600/8N1 and a
shared Partlow PID-bus profile 9600/7E1 (PID addresses initially 2 through 12).
The passive/listen profile defaults to 9600/7E1.  Master uses the selected
device profile at each transaction boundary; Passive and Slave use the one
listen profile.

Validate all values on load and before open.  Port enumeration is runtime data,
not configuration truth; show device, description, VID/PID, serial number, and
location when available.  Schema 2 deliberately has no selected global
protocol, one shared protocol profile, one slave profile, separate mode arm,
active checkbox, or open-port state.  Adapter direction, isolation, termination,
polarity, and echo behavior remain user-visible “unverified” facts rather than
invented config defaults.

## 8. Minimal module layout

```text
pyproject.toml
src/yes_rs485/
    __main__.py             # composition root; launches Tk
    models.py               # enums, immutable commands/events/results
    config.py               # validation and atomic JSON persistence
    serial_worker.py        # sole serial owner, modes, polls, transaction loop
    protocols/
        base.py             # small ProtocolAdapter contract/common results
        partlow.py          # framer, codec, matcher, operations, slave policy
        mks275.py           # framer, codec, matcher, operations, slave policy
    ui/
        app.py              # root, mode tabs, polling, session controls
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

Tests should prove at minimum: native serial settings; passive dual-protocol
recognition and zero-write behavior; mode switching on one handle; per-device
Master Poll, Poll All, and Continuous scheduling; twelve independent Slave
fake profiles; close-on-fault; one wire transaction at a time; Partlow numeric
vectors and Type 3/4 gating; MKS spacing/hex address/CR framing and RST no-
response behavior; negative ACK distinct from timeout; echo cannot correlate;
split/coalesced frames; bounded raw-buffer gap reporting; wrong-address frames
remain visible but do not satisfy a transaction; and schema-2 persistence.

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
