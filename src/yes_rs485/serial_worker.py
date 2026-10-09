"""Thread-owned serial transport for the YES RS-485 utility.

The transport deliberately knows very little about either application protocol.
Protocol adapters provide framers, transaction plans, decoding/matching, and
slave replies.  This module owns the serial handle, deadlines, and the bounded
event buffers.  In particular, it never enables pyserial's ``rs485_mode`` or
guesses how a USB adapter controls direction.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import importlib
import queue
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from uuid import UUID, uuid4


try:  # The model module is intentionally small and is developed separately.
    from .models import (  # type: ignore
        BusMode,
        Direction,
        FrameEvent,
        LifecycleState,
        ParseError,
        ProtocolId,
        SafetyArm,
        SessionSpec,
        TrafficRecord,
    )
except (ImportError, AttributeError):  # pragma: no cover - bootstrap fallback
    class BusMode(Enum):
        PASSIVE = "passive"
        MASTER = "master"
        SLAVE = "slave"

    class ProtocolId(Enum):
        PARTLOW_P1160 = "partlow_p1160"
        MKS_275 = "mks_275"

    class SafetyArm(Enum):
        PLC_DISCONNECTED = "plc_disconnected"
        SENSORS_DISCONNECTED = "sensors_disconnected"

    LifecycleState = None

    class Direction(Enum):
        RX = "rx"
        TX = "tx"

    @dataclass(frozen=True)
    class ParseError:
        message: str
        raw: bytes = b""

    @dataclass(frozen=True)
    class TrafficRecord:
        seq: int
        monotonic_ns: int
        wall_time_utc: datetime
        direction: Direction
        data: bytes
        mode: BusMode
        protocol: ProtocolId
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
        transaction_id: UUID | None = None
        decoded: Any = None
        error: Any = None

    @dataclass(frozen=True)
    class TrafficBatch:
        records: tuple[TrafficRecord, ...]
        oldest_seq: int | None
        newest_seq: int | None
        dropped_before_seq: int | None

    @dataclass(frozen=True)
    class FrameBatch:
        frames: tuple[FrameEvent, ...]
        oldest_frame_id: int | None
        newest_frame_id: int | None
        dropped_before_frame_id: int | None

    Notification = Any


# Batches are transport return values rather than protocol values.  Keep them
# here until/if the shared model module chooses to expose them too.
try:
    FrameBatch
except NameError:
    @dataclass(frozen=True)
    class FrameBatch:
        frames: tuple[FrameEvent, ...]
        oldest_frame_id: int | None
        newest_frame_id: int | None
        dropped_before_frame_id: int | None

try:
    TrafficBatch
except NameError:
    @dataclass(frozen=True)
    class TrafficBatch:
        records: tuple[TrafficRecord, ...]
        oldest_seq: int | None
        newest_seq: int | None
        dropped_before_seq: int | None


try:
    from .models import TransactionPlan, TransactionStep  # type: ignore
except (ImportError, AttributeError):  # pragma: no cover - bootstrap fallback
    @dataclass(frozen=True)
    class TransactionStep:
        tx: bytes
        expectation: Any = None
        timeout_s: float = 0.0
        recovery_delay_s: float = 0.0

    @dataclass(frozen=True)
    class TransactionPlan:
        protocol: ProtocolId
        steps: tuple[TransactionStep, ...]
        is_write: bool = False


class SerialFactory(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class _Command:
    kind: str
    command_id: UUID
    value: Any = None


@dataclass(frozen=True)
class _Pending:
    transaction_id: UUID
    operation: Any
    plan: Any
    step_index: int
    deadline: float | None
    recovery_until: float | None
    waiting_for_response: bool
    sent_at: float | None = None


@dataclass(frozen=True)
class _Session:
    spec: Any
    adapter: Any
    arm: Any


@dataclass(frozen=True)
class StateChanged:
    command_id: UUID | None
    old_state: Any
    new_state: Any
    session_summary: Any = None
    kind: str = field(default="state_changed", init=False)


@dataclass(frozen=True)
class CommandRejected:
    command_id: UUID
    reason: str
    kind: str = field(default="command_rejected", init=False)


@dataclass(frozen=True)
class TransactionStarted:
    transaction_id: UUID
    operation_summary: Any = None
    kind: str = field(default="transaction_started", init=False)


@dataclass(frozen=True)
class TransactionCompleted:
    transaction_id: UUID
    result: Any = None
    kind: str = field(default="transaction_completed", init=False)


@dataclass(frozen=True)
class TransactionFailed:
    transaction_id: UUID
    failure_kind: str
    detail: str = ""
    kind: str = field(default="transaction_failed", init=False)


@dataclass(frozen=True)
class SlaveStateChanged:
    request_frame_id: int | None
    state_snapshot: Any
    kind: str = field(default="slave_state_changed", init=False)


@dataclass(frozen=True)
class RawRecordsDropped:
    total_count: int
    kind: str = field(default="raw_records_dropped", init=False)


@dataclass(frozen=True)
class FrameRecordsDropped:
    total_count: int
    kind: str = field(default="frame_records_dropped", init=False)


@dataclass(frozen=True)
class Faulted:
    command_id_or_none: UUID | None
    exception_summary: str
    kind: str = field(default="faulted", init=False)


class WorkerState(str, Enum):
    NEW = "new"
    IDLE = "idle"
    OPENING = "opening"
    RUNNING = "running"
    CLOSING = "closing"
    FAULTED = "faulted"
    STOPPED = "stopped"


# Use the shared lifecycle enum when available so UI/tests can compare state
# values directly.  The local enum remains useful for importing this module in
# isolation during bootstrap.
if LifecycleState is not None:
    WorkerState = LifecycleState  # type: ignore[misc,assignment]


class _DelimiterFramer:
    """Small fallback framer used only when no protocol adapter is supplied."""

    def __init__(self, delimiter: bytes) -> None:
        self._delimiter = delimiter
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        result: list[bytes] = []
        while self._delimiter in self._buf:
            end = self._buf.index(self._delimiter) + len(self._delimiter)
            result.append(bytes(self._buf[:end]))
            del self._buf[:end]
        return result

    def reset(self) -> None:
        self._buf.clear()


class _NullAdapter:
    """Transport-only adapter for passive capture and simple fake tests."""

    def __init__(self, protocol: Any) -> None:
        self.id = protocol

    def new_framer(self, role: Any) -> _DelimiterFramer:
        value = getattr(self.id, "value", self.id)
        return _DelimiterFramer(b"*" if "partlow" in str(value).lower() else b"\r")

    def decode(self, frame: bytes, direction: Any, role: Any) -> Any:
        return frame

    def compile(self, operation: Any, timeout_s: float) -> Any:
        if isinstance(operation, (bytes, bytearray, memoryview)):
            return TransactionPlan(
                protocol=self.id,
                steps=(TransactionStep(bytes(operation), None, timeout_s),),
                is_write=True,
            )
        if hasattr(operation, "tx"):
            tx = bytes(operation.tx)
            return TransactionPlan(
                protocol=self.id,
                steps=(TransactionStep(tx, getattr(operation, "expectation", None), timeout_s),),
                is_write=bool(getattr(operation, "is_write", True)),
            )
        raise ValueError("no protocol adapter can compile this operation")

    def match(self, expected: Any, actual: Any) -> Any:
        return "success" if expected is None else "unrelated"

    def respond(self, request: Any, state: Any) -> Any:
        return None

    def quiet_time_s(self, baudrate: int) -> float:
        return 0.0


class SerialWorker:
    """Single-owner serial worker with bounded command and event buffers.

    ``serial_factory`` is intentionally injectable.  Its preferred signature is
    the same keyword form used by :class:`serial.Serial`; a factory accepting a
    ``SerialSettings`` value or no arguments is also supported for test doubles.
    """

    def __init__(
        self,
        serial_factory: SerialFactory | None = None,
        adapters: Mapping[Any, Any] | None = None,
        *,
        command_capacity: int = 256,
        notification_capacity: int = 1024,
        traffic_capacity: int = 4000,
        frame_capacity: int = 1000,
        poll_interval_s: float = 0.005,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        if command_capacity < 1 or traffic_capacity < 1 or frame_capacity < 1:
            raise ValueError("queue capacities must be positive")
        self._serial_factory = serial_factory or self._default_serial_factory
        self._adapters = dict(adapters or {})
        self._commands: queue.Queue[_Command] = queue.Queue(maxsize=command_capacity)
        # Lifecycle and transaction notifications are reliable events.  They
        # must not be evicted when Tk is briefly busy, and the owner thread
        # must never block waiting for the UI to render.  The raw/frame rings
        # and command queue are the bounded structures; this queue is a
        # non-blocking FIFO with no silent loss.
        self._notifications: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._traffic_capacity = traffic_capacity
        self._frame_capacity = frame_capacity
        self._traffic: deque[Any] = deque(maxlen=traffic_capacity)
        self._frames: deque[Any] = deque(maxlen=frame_capacity)
        self._traffic_dropped = 0
        self._frame_dropped = 0
        self._next_seq = 1
        self._next_frame_id = 1
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._poll_interval_s = max(0.0005, float(poll_interval_s))
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_ident: int | None = None
        self._start_lock = threading.Lock()
        self._state: WorkerState = WorkerState.NEW
        self._session: _Session | None = None
        self._serial: Any = None
        self._framer: Any = None
        self._pending: _Pending | None = None
        self._slave_state: Any = None
        self._stop_requested = False
        self._last_command_id: UUID | None = None
        self._tx_history: deque[bytes] = deque(maxlen=32)
        self._slave_reply: tuple[float, bytes] | None = None
        self._last_rx_at: float | None = None
        # Sequence of the traffic record where the current incremental RX
        # frame began.  A frame may span several OS reads.
        self._rx_frame_first_seq: int | None = None
        self._lock = threading.RLock()

    @property
    def state(self) -> WorkerState:
        with self._lock:
            return self._state

    @property
    def pending_transaction_id(self) -> UUID | None:
        with self._lock:
            return self._pending.transaction_id if self._pending else None

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        with self._start_lock:
            if self._state == WorkerState.STOPPED:
                raise RuntimeError("worker is stopped")
            if self._thread and self._thread.is_alive():
                return
            if self._state != WorkerState.NEW:
                return
            self._stop_requested = False
            self._state = WorkerState.IDLE
            self._thread = threading.Thread(target=self._run, name="yes-rs485-serial", daemon=True)
            self._thread.start()

    def open(self, spec: Any, arm: Any = None) -> UUID:
        command_id = uuid4()
        self._enqueue(_Command("open", command_id, (spec, arm)))
        return command_id

    def close(self) -> UUID:
        command_id = uuid4()
        self._enqueue(_Command("close", command_id))
        return command_id

    def submit(self, operation: Any) -> UUID:
        transaction_id = uuid4()
        self._enqueue(_Command("submit", transaction_id, (transaction_id, operation)))
        return transaction_id

    def replace_slave_profile(self, profile: Any) -> UUID:
        command_id = uuid4()
        self._enqueue(_Command("replace_slave_profile", command_id, profile))
        return command_id

    def cancel(self, transaction_id: UUID) -> UUID:
        command_id = uuid4()
        self._enqueue(_Command("cancel", command_id, transaction_id))
        return command_id

    def drain_notifications(self, limit: int = 200) -> list[Any]:
        if limit <= 0:
            return []
        result: list[Any] = []
        for _ in range(limit):
            try:
                result.append(self._notifications.get_nowait())
            except queue.Empty:
                break
        return result

    def drain_traffic(self, after_seq: int = 0, limit: int = 1000) -> Any:
        # ``deque`` iteration is not safe while the owner thread appends a
        # record.  Snapshot under the same lock used by the public state
        # properties; the UI must never observe a partially-mutated ring.
        with self._lock:
            records = [item for item in self._traffic if item.seq > after_seq]
            if limit >= 0:
                records = records[:limit]
            oldest = self._traffic[0].seq if self._traffic else None
            newest = self._traffic[-1].seq if self._traffic else None
        dropped = None
        if oldest is not None and after_seq < oldest - 1:
            dropped = oldest - 1
        return self._make_batch(TrafficBatch, records, oldest, newest, dropped, "records")

    def drain_frames(self, after_frame_id: int = 0, limit: int = 1000) -> Any:
        with self._lock:
            records = [item for item in self._frames if item.frame_id > after_frame_id]
            if limit >= 0:
                records = records[:limit]
            oldest = self._frames[0].frame_id if self._frames else None
            newest = self._frames[-1].frame_id if self._frames else None
        dropped = None
        if oldest is not None and after_frame_id < oldest - 1:
            dropped = oldest - 1
        return self._make_batch(FrameBatch, records, oldest, newest, dropped, "frames")

    def stop(self, join_timeout_s: float = 2.0) -> None:
        with self._start_lock:
            if self._state == WorkerState.STOPPED:
                return
            if not self._thread or not self._thread.is_alive():
                self._state = WorkerState.STOPPED
                self._close_handle_direct()
                return
            self._stop_requested = True
            self._wake.set()
            thread = self._thread
        thread.join(max(0.0, join_timeout_s))
        if thread.is_alive():
            raise TimeoutError("serial worker did not stop before join timeout")

    def _enqueue(self, command: _Command) -> None:
        if self._state == WorkerState.NEW:
            raise RuntimeError("call start() before mutating worker state")
        if self._state == WorkerState.STOPPED:
            raise RuntimeError("worker is stopped")
        try:
            self._commands.put_nowait(command)
        except queue.Full as exc:
            raise queue.Full("serial worker command queue is full") from exc
        self._wake.set()

    @staticmethod
    def _default_serial_factory(*args: Any, **kwargs: Any) -> Any:
        try:
            import serial  # type: ignore
        except ImportError as exc:  # pragma: no cover - requires pyserial
            raise RuntimeError("pyserial is required to open a serial session") from exc
        return serial.Serial(*args, **kwargs)

    def _run(self) -> None:
        self._thread_ident = threading.get_ident()
        try:
            while not self._stop_requested:
                self._drain_commands()
                if self._serial is not None:
                    self._poll_serial()
                    self._advance_slave_reply()
                    self._advance_transaction()
                wait_s = self._next_wait()
                self._wake.wait(wait_s)
                self._wake.clear()
        except BaseException as exc:  # owner thread must close before returning
            self._fault(None, exc)
        finally:
            try:
                self._finish_pending("closed", "worker stopped")
                self._close_handle_direct()
            finally:
                with self._lock:
                    self._state = WorkerState.STOPPED
                    self._thread_ident = None

    def _drain_commands(self) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            self._last_command_id = command.command_id
            try:
                if command.kind == "open":
                    self._handle_open(command)
                elif command.kind == "close":
                    self._handle_close(command)
                elif command.kind == "submit":
                    self._handle_submit(command)
                elif command.kind == "replace_slave_profile":
                    self._handle_replace_profile(command)
                elif command.kind == "cancel":
                    self._handle_cancel(command)
                else:
                    self._reject(command, "unknown command")
            except BaseException as exc:
                self._fault(command.command_id, exc)

    def _handle_open(self, command: _Command) -> None:
        spec, arm = command.value
        mode = getattr(spec, "mode", None)
        if self._serial is not None or self._session is not None:
            self._reject(command, "session already open")
            return
        reason = self._validate_arm(spec, arm)
        if reason:
            self._reject(command, reason)
            return
        self._set_state(WorkerState.OPENING, command.command_id, None)
        adapter = self._adapter_for(getattr(spec, "protocol", None))
        serial_handle = self._open_serial(spec)
        self._serial = serial_handle
        self._session = _Session(spec, adapter, arm)
        # SessionSpec is the persisted/user-visible capacity policy.  Apply it
        # only after a successful open so records from a previous session can
        # never leak across a lifecycle boundary.
        capacity = int(getattr(spec, "raw_capacity", self._traffic_capacity))
        if capacity < 1:
            self._close_handle_direct()
            self._session = None
            raise ValueError("raw_capacity must be positive")
        self._traffic_capacity = capacity
        self._traffic = deque(maxlen=capacity)
        # Frames belong to the same live session as traffic.  Retaining the
        # previous session's frame ring makes a fresh open appear to contain
        # stale decoded traffic and defeats the UI's gap accounting.
        self._frames = deque(maxlen=self._frame_capacity)
        self._traffic_dropped = 0
        self._frame_dropped = 0
        try:
            self._framer = adapter.new_framer(mode) if hasattr(adapter, "new_framer") else None
        except BaseException:
            self._close_handle_direct()
            self._session = None
            self._framer = None
            raise
        self._pending = None
        self._slave_state = self._profile_state(getattr(spec, "slave_profile", None))
        self._tx_history.clear()
        self._slave_reply = None
        self._last_rx_at = None
        self._rx_frame_first_seq = None
        self._set_state(WorkerState.RUNNING, command.command_id, self._summary())

    def _handle_close(self, command: _Command) -> None:
        if self._serial is None:
            self._reject(command, "session is not open")
            return
        self._set_state(WorkerState.CLOSING, command.command_id, self._summary())
        self._finish_pending("closed", "session closed")
        self._close_handle_direct()
        self._session = None
        self._slave_state = None
        self._framer = None
        self._set_state(WorkerState.IDLE, command.command_id, None)

    def _handle_submit(self, command: _Command) -> None:
        transaction_id, operation = command.value
        if self._session is None or self._serial is None:
            self._reject(CommandLike(command.command_id), "session is not open")
            return
        if getattr(self._session.spec, "mode", None) != BusMode.MASTER:
            self._reject(CommandLike(command.command_id), "submit is only legal in master mode")
            return
        if self._pending is not None:
            self._reject(CommandLike(command.command_id), "master transaction already outstanding")
            return
        adapter = self._session.adapter
        try:
            plan = adapter.compile(operation, float(getattr(self._session.spec, "response_timeout_s", 1.0)))
            self._validate_plan(plan)
        except BaseException as exc:
            self._reject(CommandLike(command.command_id), f"operation rejected: {exc}")
            return
        if not getattr(plan, "steps", None):
            self._notify(TransactionCompleted(transaction_id, None))
            return
        self._notify(TransactionStarted(transaction_id, self._operation_summary(operation)))
        self._pending = _Pending(transaction_id, operation, plan, 0, None, None, False)

    def _handle_replace_profile(self, command: _Command) -> None:
        if self._session is None or self._serial is None:
            self._reject(command, "session is not open")
            return
        if getattr(self._session.spec, "mode", None) != BusMode.SLAVE:
            self._reject(command, "slave profile only applies in slave mode")
            return
        profile = command.value
        validator = getattr(self._session.adapter, "validate_slave_profile", None)
        if validator:
            validator(profile)
        old_spec = self._session.spec
        try:
            from dataclasses import replace
            new_spec = replace(old_spec, slave_profile=profile)
        except (TypeError, ValueError):
            new_spec = SimpleNamespace(**vars(old_spec), slave_profile=profile)
        self._session = _Session(new_spec, self._session.adapter, self._session.arm)
        self._slave_state = self._profile_state(profile)
        self._notify(SlaveStateChanged(None, self._slave_state))

    def _handle_cancel(self, command: _Command) -> None:
        target = command.value
        if self._pending is None or self._pending.transaction_id != target:
            self._reject(command, "transaction is not outstanding")
            return
        self._finish_pending("cancelled", "transaction cancelled")
        self._notify(CommandAccepted(command.command_id, target))

    def _poll_serial(self) -> None:
        serial_handle = self._serial
        if serial_handle is None:
            return
        try:
            waiting = getattr(serial_handle, "in_waiting", None)
            if waiting is not None and int(waiting) <= 0:
                return
            data = serial_handle.read(max(1, min(4096, int(waiting or 4096))))
        except BaseException as exc:
            self._fault(self._last_command_id, exc)
            return
        if not data:
            return
        data = bytes(data)
        self._last_rx_at = self._clock()
        rx_flags = frozenset({"POSSIBLE_ECHO"}) if self._is_possible_echo(data) else frozenset()
        record = self._record_traffic(Direction.RX, data, rx_flags)
        self._feed_frames(data, Direction.RX, getattr(record, "seq", None))

    def _advance_slave_reply(self) -> None:
        delayed = self._slave_reply
        if delayed is None or self._clock() < delayed[0]:
            return
        self._slave_reply = None
        self._write_slave_reply(delayed[1])

    def _advance_transaction(self) -> None:
        pending = self._pending
        session = self._session
        if pending is None or session is None or self._serial is None:
            return
        now = self._clock()
        if pending.recovery_until is not None:
            if now < pending.recovery_until:
                return
            self._pending = _Pending(
                pending.transaction_id,
                pending.operation,
                pending.plan,
                pending.step_index + 1,
                None,
                None,
                False,
                pending.sent_at,
            )
            return
        if pending.waiting_for_response:
            if pending.deadline is not None and now >= pending.deadline:
                self._finish_pending("timeout", "response deadline expired")
            return
        steps = tuple(getattr(pending.plan, "steps", ()))
        if pending.step_index >= len(steps):
            self._finish_pending("success", None)
            return
        step = steps[pending.step_index]
        quiet_s = self._quiet_time(session.adapter, getattr(session.spec, "serial", None))
        quiet_anchor = self._last_rx_at if self._last_rx_at is not None else pending.sent_at
        if quiet_anchor is not None and now < quiet_anchor + quiet_s:
            return
        tx = bytes(getattr(step, "tx", b""))
        if tx:
            traffic = self._record_traffic(Direction.TX, tx, frozenset({"WRITE_ATTEMPT"}))
            self._record_tx_frame(tx, traffic)
            self._tx_history.append(tx)
            try:
                self._serial.write(tx)
                flush = getattr(self._serial, "flush", None)
                if flush:
                    flush()
            except BaseException as exc:
                self._finish_pending("write_error", str(exc))
                self._fault(pending.transaction_id, exc)
                return
        sent_at = now
        expectation = getattr(step, "expectation", None)
        timeout_s = float(getattr(step, "timeout_s", 0.0) or 0.0)
        recovery_s = float(getattr(step, "recovery_delay_s", 0.0) or 0.0)
        if expectation is None:
            if recovery_s > 0:
                recovery_until = now + recovery_s
                self._pending = _Pending(
                    pending.transaction_id, pending.operation, pending.plan,
                    pending.step_index, None, recovery_until, False, sent_at,
                )
            else:
                self._pending = _Pending(
                    pending.transaction_id, pending.operation, pending.plan,
                    pending.step_index + 1, None, None, False, sent_at,
                )
            return
        self._pending = _Pending(
            pending.transaction_id,
            pending.operation,
            pending.plan,
            pending.step_index,
            now + max(0.0, timeout_s),
            None,
            True,
            sent_at,
        )

    def _feed_frames(self, data: bytes, direction: Any, traffic_seq: int | None = None) -> None:
        framer = self._framer
        if framer is None:
            return
        try:
            feed = getattr(framer, "feed", None) or getattr(framer, "feed_bytes", None)
            if feed is None:
                return
            first_seq = self._rx_frame_first_seq if direction == Direction.RX else traffic_seq
            if first_seq is None:
                first_seq = traffic_seq
            frames = feed(data)
            if frames is None:
                return
            if isinstance(frames, (bytes, bytearray, memoryview)):
                frames = [bytes(frames)]
            for raw_frame in frames:
                self._handle_frame(
                    bytes(raw_frame),
                    direction,
                    first_traffic_seq=first_seq,
                    last_traffic_seq=traffic_seq,
                )
                # Coalesced frames in one OS read each begin in this same
                # traffic record.  A partial tail also begins in this record.
                first_seq = traffic_seq
            if direction == Direction.RX:
                buffered = self._framer_has_pending_bytes(framer)
                self._rx_frame_first_seq = first_seq if buffered else None
        except BaseException as exc:
            # A malformed input should be visible to the UI, but an adapter
            # crash is a serial-session fault and must close the port.
            self._fault(self._last_command_id, exc)

    def _handle_frame(
        self,
        raw_frame: bytes,
        direction: Any,
        *,
        first_traffic_seq: int | None = None,
        last_traffic_seq: int | None = None,
    ) -> None:
        session = self._session
        if session is None:
            return
        if first_traffic_seq is None and last_traffic_seq is None:
            first, last = self._traffic_span_for_latest(direction)
        else:
            first, last = first_traffic_seq, last_traffic_seq
        decoded = None
        error = None
        try:
            decoder = getattr(session.adapter, "decode", None)
            decoded = decoder(raw_frame, direction, getattr(session.spec, "mode", None)) if decoder else raw_frame
        except BaseException as exc:
            error = ParseError(str(exc), raw_frame)
        event = self._make_frame_event(raw_frame, direction, first, last, decoded, error)
        self._append_frame(event)
        if direction == Direction.RX and getattr(session.spec, "mode", None) == BusMode.MASTER:
            self._match_master_frame(decoded, event)
        elif direction == Direction.RX and getattr(session.spec, "mode", None) == BusMode.SLAVE:
            # Invalid syntax is evidence on the timeline, not a transport
            # failure.  Slave adapters must ignore malformed/unaddressable
            # requests rather than being called with ``None`` and faulting the
            # entire session.
            if decoded is None:
                return
            self._handle_slave_frame(decoded, event)

    @staticmethod
    def _framer_has_pending_bytes(framer: Any) -> bool:
        """Best-effort check for incremental bytes retained by a framer."""

        for name in ("_buffer", "_buf", "buffer"):
            try:
                value = getattr(framer, name)
            except AttributeError:
                continue
            return bool(value)
        # A third-party framer may not expose its buffer.  Without a complete
        # frame there is no safe sequence anchor to carry forward.
        return False

    def _match_master_frame(self, decoded: Any, event: Any) -> None:
        pending = self._pending
        session = self._session
        if pending is None or session is None or not pending.waiting_for_response:
            return
        steps = tuple(getattr(pending.plan, "steps", ()))
        if pending.step_index >= len(steps):
            return
        expected = getattr(steps[pending.step_index], "expectation", None)
        # A request-shaped RX echo must be retained but never correlate.
        if self._is_possible_echo(event.raw_frame):
            self._mark_frame_flag(event, "POSSIBLE_ECHO")
            return
        matcher = getattr(session.adapter, "match", None)
        try:
            result = matcher(expected, decoded) if matcher else ("success" if expected is None else "unrelated")
        except BaseException as exc:
            self._finish_pending("protocol_error", str(exc))
            return
        classification = str(getattr(result, "value", result)).lower()
        if classification in {"match_success", "success", "matched", "ok"}:
            next_step = pending.step_index + 1
            if next_step >= len(steps):
                self._finish_pending("success", decoded)
            else:
                self._pending = _Pending(
                    pending.transaction_id, pending.operation, pending.plan,
                    next_step, None, None, False, pending.sent_at,
                )
        elif classification in {"match_negative", "negative", "nack", "negative_ack"}:
            self._finish_pending("negative_ack", self._result_detail(decoded))
        elif classification in {"match_malformed_for_expected_peer", "malformed", "protocol_error"}:
            self._finish_pending("protocol_error", self._result_detail(decoded))

    def _handle_slave_frame(self, decoded: Any, event: Any) -> None:
        session = self._session
        if session is None:
            return
        responder = getattr(session.adapter, "respond", None)
        if responder is None:
            return
        profile = getattr(session.spec, "slave_profile", None)
        state = self._slave_state or self._profile_state(profile)
        try:
            decision = responder(decoded, state)
        except BaseException as exc:
            self._fault(self._last_command_id, exc)
            return
        if decision is None:
            return
        state_after = getattr(decision, "state_after", None) or state
        self._slave_state = state_after
        self._notify(SlaveStateChanged(event.frame_id, state_after))
        data = getattr(decision, "data", None)
        if data is None:
            return
        earliest = getattr(decision, "earliest_send_ns", None)
        # Adapters may express the documented receive-to-reply interval as a
        # relative recovery_s (the common case) or provide an absolute
        # monotonic deadline.  Honour either form without sleeping in the
        # owner thread.
        recovery_s = float(getattr(decision, "recovery_s", 0.0) or 0.0)
        if (earliest is None or earliest <= 0) and recovery_s > 0:
            earliest = time.monotonic_ns() + int(recovery_s * 1_000_000_000)
        if earliest is not None:
            delay = max(0.0, (earliest - time.monotonic_ns()) / 1_000_000_000)
            if delay:
                # Do not block the owner thread; retain a one-shot slave reply
                # until its deadline.  This is intentionally simple and bounded.
                self._slave_reply = (self._clock() + delay, bytes(data))
                return
        self._write_slave_reply(bytes(data))

    def _write_slave_reply(self, data: bytes) -> None:
        if self._serial is None or self._session is None:
            return
        traffic = self._record_traffic(Direction.TX, data, frozenset({"WRITE_ATTEMPT", "SLAVE_REPLY"}))
        self._record_tx_frame(data, traffic)
        self._tx_history.append(data)
        try:
            self._serial.write(data)
            flush = getattr(self._serial, "flush", None)
            if flush:
                flush()
        except BaseException as exc:
            self._fault(self._last_command_id, exc)

    def _record_traffic(self, direction: Any, data: bytes, flags: frozenset[str]) -> Any:
        session = self._session
        if session is None:
            return None
        record = self._make_traffic_record(direction, data, flags)
        dropped_count: int | None = None
        with self._lock:
            if len(self._traffic) >= self._traffic_capacity:
                self._traffic_dropped += 1
                dropped_count = self._traffic_dropped
            self._traffic.append(record)
        if dropped_count is not None:
            self._notify(RawRecordsDropped(dropped_count))
        return record

    def _append_frame(self, event: Any) -> None:
        dropped_count: int | None = None
        with self._lock:
            if len(self._frames) >= self._frame_capacity:
                self._frame_dropped += 1
                dropped_count = self._frame_dropped
            self._frames.append(event)
        if dropped_count is not None:
            self._notify(FrameRecordsDropped(dropped_count))

    def _record_tx_frame(self, raw: bytes, traffic: Any) -> None:
        """Decode a transmitted frame for the timeline without feeding RX state."""

        session = self._session
        if session is None:
            return
        decoded = None
        error = None
        try:
            decoder = getattr(session.adapter, "decode", None)
            decoded = decoder(raw, Direction.TX, getattr(session.spec, "mode", None)) if decoder else raw
        except BaseException as exc:
            error = ParseError(str(exc), raw)
        seq = getattr(traffic, "seq", None)
        self._append_frame(self._make_frame_event(raw, Direction.TX, seq, seq, decoded, error))

    def _traffic_span_for_latest(self, direction: Any) -> tuple[int | None, int | None]:
        for item in reversed(self._traffic):
            if item.direction == direction:
                return item.seq, item.seq
        return None, None

    def _is_possible_echo(self, raw: bytes) -> bool:
        return bool(raw and any(raw == sent for sent in self._tx_history))

    def _mark_frame_flag(self, event: Any, flag: str) -> None:
        # FrameEvent is frozen in the public model.  The event remains useful
        # even when a model omits flags; replace it in the ring where possible.
        flags = frozenset(set(getattr(event, "flags", frozenset())) | {flag})
        try:
            from dataclasses import replace
            replacement = replace(event, flags=flags)
        except (TypeError, ValueError):
            replacement = event
        if self._frames and self._frames[-1] is event:
            self._frames[-1] = replacement

    def _make_traffic_record(self, direction: Any, data: bytes, flags: frozenset[str]) -> Any:
        session = self._session
        item = dict(
            seq=self._next_seq,
            monotonic_ns=time.monotonic_ns(),
            wall_time_utc=self._wall_clock(),
            direction=direction,
            data=bytes(data),
            mode=getattr(session.spec, "mode", None),
            protocol=getattr(session.spec, "protocol", None),
            transaction_id=self._pending.transaction_id if self._pending else None,
            flags=flags,
        )
        self._next_seq += 1
        return self._construct(TrafficRecord, item)

    def _make_frame_event(self, raw: bytes, direction: Any, first: Any, last: Any, decoded: Any, error: Any) -> Any:
        session = self._session
        item = dict(
            frame_id=self._next_frame_id,
            monotonic_ns=time.monotonic_ns(),
            direction=direction,
            raw_frame=bytes(raw),
            first_traffic_seq=first,
            last_traffic_seq=last,
            protocol=getattr(session.spec, "protocol", None),
            transaction_id=self._pending.transaction_id if self._pending else None,
            decoded=decoded,
            error=error,
        )
        self._next_frame_id += 1
        return self._construct(FrameEvent, item)

    @staticmethod
    def _construct(cls: Any, values: dict[str, Any]) -> Any:
        try:
            return cls(**values)
        except TypeError:
            # Accommodate an in-progress model that has not added optional
            # fields yet while keeping all transport data in the worker.
            allowed = {}
            try:
                import inspect
                allowed_names = set(inspect.signature(cls).parameters)
                allowed = {k: v for k, v in values.items() if k in allowed_names}
            except (TypeError, ValueError):
                allowed = values
            try:
                return cls(**allowed)
            except TypeError:
                return SimpleNamespace(**values)

    @staticmethod
    def _make_batch(cls: Any, records: list[Any], oldest: Any, newest: Any, dropped: Any, field_name: str) -> Any:
        values = {field_name: tuple(records), "oldest_seq": oldest, "newest_seq": newest, "dropped_before_seq": dropped}
        if cls is FrameBatch:
            values = {"frames": tuple(records), "oldest_frame_id": oldest, "newest_frame_id": newest, "dropped_before_frame_id": dropped}
        try:
            return cls(**values)
        except TypeError:
            return SimpleNamespace(**values)

    def _notify(self, value: Any) -> None:
        # SimpleQueue.put is non-blocking and cannot silently evict earlier
        # lifecycle/transaction events when the UI is behind.
        self._notifications.put(value)

    def _reject(self, command: Any, reason: str) -> None:
        self._notify(CommandRejected(command.command_id, reason))

    def _set_state(self, new_state: WorkerState, command_id: UUID | None, summary: Any) -> None:
        with self._lock:
            old = self._state
            self._state = new_state
        if old != new_state:
            self._notify(StateChanged(command_id, old, new_state, summary))

    def _finish_pending(self, result_kind: str, detail: Any) -> None:
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        self._slave_reply = None
        if result_kind == "success":
            self._notify(TransactionCompleted(pending.transaction_id, detail))
        else:
            self._notify(TransactionFailed(pending.transaction_id, result_kind, str(detail or "")))

    def _fault(self, command_id: UUID | None, exc: BaseException) -> None:
        self._notify(Faulted(command_id, f"{type(exc).__name__}: {exc}"))
        self._finish_pending("fault", str(exc))
        self._close_handle_direct()
        self._session = None
        self._slave_state = None
        self._framer = None
        if not self._stop_requested:
            self._set_state(WorkerState.FAULTED, command_id, None)
            self._set_state(WorkerState.IDLE, command_id, None)

    def _close_handle_direct(self) -> None:
        handle, self._serial = self._serial, None
        if handle is not None:
            try:
                close = getattr(handle, "close", None)
                if close:
                    close()
            except BaseException:
                pass

    def _open_serial(self, spec: Any) -> Any:
        settings = getattr(spec, "serial", spec)
        kwargs = dict(
            port=getattr(settings, "port", None),
            baudrate=getattr(settings, "baudrate", None),
            bytesize=getattr(settings, "bytesize", None),
            parity=getattr(settings, "parity", None),
            stopbits=getattr(settings, "stopbits", None),
            xonxoff=bool(getattr(settings, "xonxoff", False)),
            rtscts=bool(getattr(settings, "rtscts", False)),
            dsrdtr=bool(getattr(settings, "dsrdtr", False)),
            timeout=0,
            write_timeout=0,
        )
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        factory = self._serial_factory
        try:
            return factory(**kwargs)
        except TypeError as first_error:
            try:
                return factory(settings)
            except TypeError:
                try:
                    return factory()
                except TypeError:
                    raise first_error

    def _adapter_for(self, protocol: Any) -> Any:
        if protocol in self._adapters:
            return self._adapters[protocol]
        value = getattr(protocol, "value", protocol)
        if value in self._adapters:
            return self._adapters[value]
        candidates = {
            "partlow_p1160": ("yes_rs485.protocols.partlow", "PartlowAdapter"),
            "mks_275": ("yes_rs485.protocols.mks275", "Mks275Adapter"),
        }
        candidate = candidates.get(str(value))
        if candidate:
            try:
                module = importlib.import_module(candidate[0])
                cls = getattr(module, candidate[1])
                return cls()
            except (ImportError, AttributeError, TypeError):
                pass
        return _NullAdapter(protocol)

    @staticmethod
    def _validate_arm(spec: Any, arm: Any) -> str | None:
        mode = getattr(spec, "mode", None)
        if mode == BusMode.PASSIVE or getattr(mode, "value", mode) == "passive":
            return None if arm is None else "passive sessions do not accept a safety arm"
        if mode == BusMode.MASTER or getattr(mode, "value", mode) == "master":
            expected = getattr(SafetyArm, "PLC_DISCONNECTED", "plc_disconnected")
        elif mode == BusMode.SLAVE or getattr(mode, "value", mode) == "slave":
            expected = getattr(SafetyArm, "SENSORS_DISCONNECTED", "sensors_disconnected")
            if getattr(spec, "slave_profile", None) is None:
                return "active slave mode requires a slave profile"
        else:
            return "unknown bus mode"
        if arm != expected and getattr(arm, "value", arm) != getattr(expected, "value", expected):
            return "required safety arm was not provided"
        return None

    @staticmethod
    def _validate_plan(plan: Any) -> None:
        if not hasattr(plan, "steps"):
            raise ValueError("adapter returned no transaction plan")
        for step in getattr(plan, "steps", ()):
            tx = getattr(step, "tx", None)
            if not isinstance(tx, (bytes, bytearray, memoryview)):
                raise ValueError("transaction step tx must be bytes")
            timeout = float(getattr(step, "timeout_s", 0.0) or 0.0)
            recovery = float(getattr(step, "recovery_delay_s", 0.0) or 0.0)
            if timeout < 0 or recovery < 0:
                raise ValueError("transaction timing cannot be negative")

    @staticmethod
    def _quiet_time(adapter: Any, settings: Any) -> float:
        baudrate = int(getattr(settings, "baudrate", 0) or 0)
        try:
            return max(0.0, float(adapter.quiet_time_s(baudrate)))
        except (AttributeError, TypeError, ValueError):
            return 0.0

    def _next_wait(self) -> float:
        if self._pending is None:
            waits = [self._poll_interval_s]
            if self._slave_reply is not None:
                waits.append(max(0.0, self._slave_reply[0] - self._clock()))
            return max(0.0005, min(waits))
        now = self._clock()
        waits = [self._poll_interval_s]
        if self._pending.deadline is not None:
            waits.append(max(0.0, self._pending.deadline - now))
        if self._pending.recovery_until is not None:
            waits.append(max(0.0, self._pending.recovery_until - now))
        return max(0.0005, min(waits))

    def _summary(self) -> Any:
        if not self._session:
            return None
        spec = self._session.spec
        return {
            "mode": getattr(getattr(spec, "mode", None), "value", getattr(spec, "mode", None)),
            "protocol": getattr(getattr(spec, "protocol", None), "value", getattr(spec, "protocol", None)),
            "port": getattr(getattr(spec, "serial", None), "port", None),
        }

    @staticmethod
    def _operation_summary(operation: Any) -> Any:
        if isinstance(operation, (bytes, bytearray, memoryview)):
            return {"bytes": bytes(operation)}
        return getattr(operation, "summary", None) or type(operation).__name__

    @staticmethod
    def _result_detail(decoded: Any) -> str:
        if decoded is None:
            return ""
        return str(getattr(decoded, "detail", decoded))

    @staticmethod
    def _profile_state(profile: Any) -> Any:
        if profile is None:
            return None
        reset = getattr(profile, "reset_state", None)
        if callable(reset):
            return reset()
        try:
            from .protocols.base import SlaveState
            if isinstance(profile, SlaveState):
                return profile
            return SlaveState.from_profile(profile)
        except (ImportError, AttributeError, TypeError, ValueError):
            pass
        return profile


@dataclass(frozen=True)
class CommandAccepted:
    command_id: UUID
    transaction_id: UUID
    kind: str = field(default="command_accepted", init=False)


class CommandLike:
    def __init__(self, command_id: UUID) -> None:
        self.command_id = command_id


__all__ = [
    "SerialWorker",
    "WorkerState",
    "StateChanged",
    "CommandRejected",
    "TransactionStarted",
    "TransactionCompleted",
    "TransactionFailed",
    "SlaveStateChanged",
    "RawRecordsDropped",
    "FrameRecordsDropped",
    "Faulted",
    "TransactionPlan",
    "TransactionStep",
]
