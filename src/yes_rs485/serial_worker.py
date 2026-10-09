"""Single-owner, bounded serial transport for the complete YES RS-485 bus."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import importlib
import queue
import threading
import time
from typing import Any, Callable, Mapping, Protocol
from uuid import UUID, uuid4

from .models import (
    BusMode, CANONICAL_DEVICE_IDS, DecodedFrame, DeviceConfig, DeviceId,
    DeviceRuntimeState, Direction, FrameEvent, LifecycleState, MasterOperation,
    ParseError, ProtocolId, SessionSpec, TrafficRecord, TransactionPlan,
    TransactionStep, runtime_state_for,
)


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


class SerialFactory(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class _Command:
    kind: str
    command_id: UUID
    value: Any = None


@dataclass(frozen=True)
class _Session:
    spec: SessionSpec
    devices: Mapping[DeviceId, DeviceConfig]
    adapters: Mapping[ProtocolId, Any]
    runtime: dict[DeviceId, DeviceRuntimeState]
    routes: Mapping[tuple[ProtocolId, int], DeviceId]


@dataclass(frozen=True)
class _ScheduledOperation:
    device_id: DeviceId
    operation: MasterOperation
    cycle_id: UUID | None = None
    ordinal: int | None = None


@dataclass(frozen=True)
class _PollCycle:
    cycle_id: UUID
    continuous: bool
    total: int = 12


@dataclass(frozen=True)
class _Pending:
    transaction_id: UUID
    device_id: DeviceId
    operation: MasterOperation
    plan: Any
    step_index: int
    deadline: float | None = None
    recovery_until: float | None = None
    waiting_for_response: bool = False
    sent_at: float | None = None
    cycle_id: UUID | None = None
    ordinal: int | None = None


@dataclass(frozen=True)
class _DelayedReply:
    due: float
    data: bytes
    protocol: ProtocolId
    device_id: DeviceId
    request_frame_id: int


@dataclass(frozen=True)
class StateChanged:
    command_id: UUID | None
    old_state: Any
    new_state: Any
    session_summary: Any = None
    kind: str = field(default="state_changed", init=False)


@dataclass(frozen=True)
class ModeChanged:
    command_id: UUID
    old_mode: BusMode
    new_mode: BusMode
    kind: str = field(default="mode_changed", init=False)


@dataclass(frozen=True)
class ModeChangeFailed:
    command_id: UUID
    requested_mode: BusMode
    reason: str
    kind: str = field(default="mode_change_failed", init=False)


@dataclass(frozen=True)
class CommandAccepted:
    command_id: UUID
    action: str
    related_id: UUID | None = None
    kind: str = field(default="command_accepted", init=False)


@dataclass(frozen=True)
class CommandRejected:
    command_id: UUID
    reason: str
    kind: str = field(default="command_rejected", init=False)


@dataclass(frozen=True)
class ContinuousPollChanged:
    command_id: UUID
    active: bool
    kind: str = field(default="continuous_poll_changed", init=False)


@dataclass(frozen=True)
class TransactionStarted:
    transaction_id: UUID
    device_id: DeviceId
    operation_summary: Any = None
    cycle_id: UUID | None = None
    ordinal: int | None = None
    kind: str = field(default="transaction_started", init=False)


@dataclass(frozen=True)
class TransactionCompleted:
    transaction_id: UUID
    device_id: DeviceId
    result: Any = None
    cycle_id: UUID | None = None
    ordinal: int | None = None
    kind: str = field(default="transaction_completed", init=False)


@dataclass(frozen=True)
class TransactionFailed:
    transaction_id: UUID
    device_id: DeviceId
    failure_kind: str
    detail: str = ""
    cycle_id: UUID | None = None
    ordinal: int | None = None
    kind: str = field(default="transaction_failed", init=False)


@dataclass(frozen=True)
class PollCycleStarted:
    cycle_id: UUID
    continuous: bool
    total: int = 12
    kind: str = field(default="poll_cycle_started", init=False)


@dataclass(frozen=True)
class PollCycleCompleted:
    cycle_id: UUID
    continuous: bool
    succeeded: int
    failed: int
    kind: str = field(default="poll_cycle_completed", init=False)


@dataclass(frozen=True)
class DeviceStateChanged:
    device_id: DeviceId
    request_frame_id: int | None
    state_snapshot: DeviceRuntimeState
    kind: str = field(default="device_state_changed", init=False)


@dataclass(frozen=True)
class SlaveRequestIgnored:
    frame_id: int | None
    protocol: ProtocolId
    address: int | None
    reason: str
    kind: str = field(default="slave_request_ignored", init=False)


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


WorkerState = LifecycleState


class _DelimiterFramer:
    def __init__(self, delimiter: bytes) -> None:
        self._delimiter = delimiter
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        result: list[bytes] = []
        while self._delimiter in self._buffer:
            end = self._buffer.index(self._delimiter) + len(self._delimiter)
            result.append(bytes(self._buffer[:end]))
            del self._buffer[:end]
        return result


class _NullAdapter:
    def __init__(self, protocol: ProtocolId) -> None:
        self.id = protocol

    def new_framer(self, role: BusMode) -> _DelimiterFramer:
        return _DelimiterFramer(b"*" if self.id is ProtocolId.PARTLOW_P1160 else b"\r")

    def compile(self, operation: MasterOperation, timeout_s: float) -> TransactionPlan:
        data = operation.data if isinstance(operation.data, (bytes, bytearray)) else b""
        return TransactionPlan(self.id, (TransactionStep(bytes(data), timeout_s=timeout_s),))

    def decode(self, frame: bytes, direction: Direction, role: BusMode) -> Any:
        return frame

    def match(self, expected: Any, actual: Any) -> Any:
        return "success" if expected is None else "unrelated"

    def respond(self, request: Any, state: Any) -> Any:
        return None


class SerialWorker:
    """Bounded serial worker: one thread, one handle, at most one TX plan."""

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
        del notification_capacity
        if command_capacity < 1 or traffic_capacity < 1 or frame_capacity < 1:
            raise ValueError("queue capacities must be positive")
        self._serial_factory = serial_factory or self._default_serial_factory
        self._adapter_overrides = dict(adapters or {})
        self._commands: queue.Queue[_Command] = queue.Queue(maxsize=command_capacity)
        self._notifications: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._traffic_capacity = traffic_capacity
        self._frame_capacity = frame_capacity
        self._traffic: deque[TrafficRecord] = deque(maxlen=traffic_capacity)
        self._frames: deque[FrameEvent] = deque(maxlen=frame_capacity)
        self._traffic_dropped = self._frame_dropped = 0
        self._next_seq = self._next_frame_id = 1
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._poll_interval_s = max(0.0005, float(poll_interval_s))
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_ident: int | None = None
        self._start_lock = threading.Lock()
        self._lock = threading.RLock()
        self._state: WorkerState = WorkerState.NEW
        self._serial: Any = None
        self._session: _Session | None = None
        self._framers: dict[ProtocolId, Any] = {}
        self._framer_first_seq: dict[ProtocolId, int | None] = {}
        self._pending: _Pending | None = None
        self._scheduled: deque[_ScheduledOperation] = deque()
        self._cycle: _PollCycle | None = None
        self._cycle_succeeded = self._cycle_failed = 0
        self._continuous = False
        self._continuous_next_at: float | None = None
        self._slave_reply: _DelayedReply | None = None
        self._tx_history: deque[bytes] = deque(maxlen=32)
        self._stop_requested = False
        self._last_command_id: UUID | None = None

    @property
    def state(self) -> WorkerState:
        with self._lock:
            return self._state

    @property
    def mode(self) -> BusMode | None:
        return self._session.spec.mode if self._session else None

    @property
    def pending_transaction_id(self) -> UUID | None:
        return self._pending.transaction_id if self._pending else None

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        with self._start_lock:
            if self._state is WorkerState.STOPPED:
                raise RuntimeError("worker is stopped")
            if self._thread and self._thread.is_alive():
                return
            self._stop_requested = False
            self._set_state(WorkerState.IDLE, None, None)
            self._thread = threading.Thread(target=self._run, name="yes-rs485-serial", daemon=True)
            self._thread.start()

    def open(self, spec: SessionSpec) -> UUID:
        command_id = uuid4()
        self._enqueue(_Command("open", command_id, spec))
        return command_id

    def set_mode(self, mode: BusMode) -> UUID:
        command_id = uuid4()
        self._enqueue(_Command("set_mode", command_id, BusMode(mode)))
        return command_id

    def close(self) -> UUID:
        command_id = uuid4()
        self._enqueue(_Command("close", command_id))
        return command_id

    def submit(self, device_id: DeviceId, operation: MasterOperation) -> UUID:
        command_id = uuid4()
        self._enqueue(_Command("submit", command_id, (DeviceId(device_id), operation)))
        return command_id

    def poll(self, device_id: DeviceId) -> UUID:
        command_id = uuid4()
        self._enqueue(_Command("poll", command_id, DeviceId(device_id)))
        return command_id

    def poll_all_once(self) -> UUID:
        command_id = uuid4()
        self._enqueue(_Command("poll_all_once", command_id))
        return command_id

    def start_continuous_poll(self) -> UUID:
        command_id = uuid4()
        self._enqueue(_Command("start_continuous_poll", command_id))
        return command_id

    def stop_continuous_poll(self) -> UUID:
        command_id = uuid4()
        self._enqueue(_Command("stop_continuous_poll", command_id))
        return command_id

    def replace_fake_values(self, device_id: DeviceId, values: Mapping[str, Any]) -> UUID:
        command_id = uuid4()
        self._enqueue(_Command("replace_fake_values", command_id, (DeviceId(device_id), dict(values))))
        return command_id

    def cancel(self, transaction_id: UUID) -> UUID:
        command_id = uuid4()
        self._enqueue(_Command("cancel", command_id, transaction_id))
        return command_id

    def drain_notifications(self, limit: int = 200) -> list[Any]:
        result: list[Any] = []
        for _ in range(max(0, limit)):
            try:
                result.append(self._notifications.get_nowait())
            except queue.Empty:
                break
        return result

    def drain_traffic(self, after_seq: int = 0, limit: int = 1000) -> TrafficBatch:
        with self._lock:
            records = [record for record in self._traffic if record.seq > after_seq]
            if limit >= 0:
                records = records[:limit]
            oldest = self._traffic[0].seq if self._traffic else None
            newest = self._traffic[-1].seq if self._traffic else None
        dropped = oldest - 1 if oldest is not None and after_seq < oldest - 1 else None
        return TrafficBatch(tuple(records), oldest, newest, dropped)

    def drain_frames(self, after_frame_id: int = 0, limit: int = 1000) -> FrameBatch:
        with self._lock:
            frames = [frame for frame in self._frames if frame.frame_id > after_frame_id]
            if limit >= 0:
                frames = frames[:limit]
            oldest = self._frames[0].frame_id if self._frames else None
            newest = self._frames[-1].frame_id if self._frames else None
        dropped = oldest - 1 if oldest is not None and after_frame_id < oldest - 1 else None
        return FrameBatch(tuple(frames), oldest, newest, dropped)

    def stop(self, join_timeout_s: float = 2.0) -> None:
        with self._start_lock:
            if self._state is WorkerState.STOPPED:
                return
            self._stop_requested = True
            self._wake.set()
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(max(0.0, join_timeout_s))
            if thread.is_alive():
                raise TimeoutError("serial worker did not stop before join timeout")
        else:
            self._close_handle_direct()
            self._set_state(WorkerState.STOPPED, None, None)

    def _enqueue(self, command: _Command) -> None:
        if self._state is WorkerState.NEW:
            raise RuntimeError("call start() before mutating worker state")
        if self._state is WorkerState.STOPPED:
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
        except ImportError as exc:  # pragma: no cover
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
                    self._advance_pending()
                    self._advance_scheduler()
                self._wake.wait(self._next_wait())
                self._wake.clear()
        except BaseException as exc:
            self._fault(self._last_command_id, exc)
        finally:
            self._cancel_all()
            self._close_handle_direct()
            self._session = None
            self._set_state(WorkerState.STOPPED, None, None)
            self._thread_ident = None

    def _drain_commands(self) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            self._last_command_id = command.command_id
            try:
                getattr(self, "_handle_" + command.kind)(command)
            except BaseException as exc:
                self._fault(command.command_id, exc)

    def _handle_open(self, command: _Command) -> None:
        spec = command.value
        if self._session is not None or self._serial is not None:
            self._reject(command, "session already open")
            return
        if not isinstance(spec, SessionSpec):
            self._reject(command, "invalid session specification")
            return
        self._set_state(WorkerState.OPENING, command.command_id, None)
        try:
            handle = self._open_serial(spec)
            devices = {device.device_id: device for device in spec.devices}
            adapters: dict[ProtocolId, Any] = {}
            for device in spec.devices:
                adapters.setdefault(device.protocol, self._adapter_for(device.protocol, device))
            runtime = {device.device_id: runtime_state_for(device) for device in spec.devices}
            routes = {(device.protocol, device.address): device.device_id for device in spec.devices}
            self._serial = handle
            self._session = _Session(spec, devices, adapters, runtime, routes)
            self._framers = {protocol: self._new_framer(protocol, spec.mode) for protocol in ProtocolId}
            self._framer_first_seq = {protocol: None for protocol in ProtocolId}
            self._reset_framers()
            self._set_state(WorkerState.RUNNING, command.command_id, self._summary())
            self._notify(CommandAccepted(command.command_id, "open"))
        except BaseException as exc:
            self._close_handle_direct()
            self._session = None
            self._fault(command.command_id, exc, transition=False)
            self._set_state(WorkerState.IDLE, command.command_id, None)

    def _handle_close(self, command: _Command) -> None:
        if self._session is None and self._serial is None:
            self._notify(CommandAccepted(command.command_id, "close"))
            return
        self._set_state(WorkerState.CLOSING, command.command_id, None)
        self._cancel_all()
        self._close_handle_direct()
        self._session = None
        self._framers.clear()
        self._set_state(WorkerState.IDLE, command.command_id, None)
        self._notify(CommandAccepted(command.command_id, "close"))

    def _handle_set_mode(self, command: _Command) -> None:
        requested = BusMode(command.value)
        if self._session is None or self._serial is None:
            self._reject(command, "no open session")
            return
        old = self._session.spec.mode
        if requested is old:
            self._notify(CommandAccepted(command.command_id, "set_mode"))
            return
        try:
            self._cancel_transactions("mode_changed")
            self._slave_reply = None
            self._reset_framers()
            if requested in (BusMode.PASSIVE, BusMode.SLAVE):
                if not self._configure_serial(self._session.spec.listen_serial):
                    raise OSError("serial format readback failed")
            spec = replace(self._session.spec, mode=requested)
            self._session = replace(self._session, spec=spec)
            self._framers = {protocol: self._new_framer(protocol, requested) for protocol in ProtocolId}
            self._reset_framers()
            self._notify(ModeChanged(command.command_id, old, requested))
        except BaseException as exc:
            self._close_handle_direct()
            self._cancel_all()
            self._session = None
            self._framers.clear()
            self._set_state(WorkerState.IDLE, command.command_id, None)
            self._notify(ModeChangeFailed(command.command_id, requested, str(exc)))

    def _handle_submit(self, command: _Command) -> None:
        device_id, operation = command.value
        if not self._master_ready(command):
            return
        if device_id not in self._session.devices:
            self._reject(command, "unknown device")
            return
        if not isinstance(operation, MasterOperation):
            self._reject(command, "operation must be MasterOperation")
            return
        if self._busy_for_manual():
            self._reject(command, "busy")
            return
        item = _ScheduledOperation(device_id, replace(operation, address=self._session.devices[device_id].address))
        self._run_item(item, transaction_id=command.command_id)

    def _handle_poll(self, command: _Command) -> None:
        if not self._master_ready(command):
            return
        if self._busy_for_manual():
            self._reject(command, "busy")
            return
        device_id = command.value
        if device_id not in self._session.devices:
            self._reject(command, "unknown device")
            return
        self._run_item(_ScheduledOperation(device_id, self._canonical_poll(device_id)), transaction_id=command.command_id)

    def _handle_poll_all_once(self, command: _Command) -> None:
        if not self._master_ready(command):
            return
        if self._busy_for_schedule():
            self._reject(command, "busy")
            return
        self._queue_cycle(False)
        self._notify(CommandAccepted(command.command_id, "poll_all_once", self._cycle.cycle_id if self._cycle else None))

    def _handle_start_continuous_poll(self, command: _Command) -> None:
        if not self._master_ready(command):
            return
        if self._continuous:
            self._notify(CommandAccepted(command.command_id, "start_continuous_poll"))
            return
        if self._busy_for_schedule():
            self._reject(command, "busy")
            return
        self._continuous = True
        self._queue_cycle(True)
        self._notify(ContinuousPollChanged(command.command_id, True))
        self._notify(CommandAccepted(command.command_id, "start_continuous_poll", self._cycle.cycle_id if self._cycle else None))

    def _handle_stop_continuous_poll(self, command: _Command) -> None:
        if self._session is None or self._session.spec.mode is not BusMode.MASTER:
            self._reject(command, "not running in master mode")
            return
        self._continuous = False
        self._continuous_next_at = None
        self._scheduled.clear()
        if self._cycle and self._cycle.continuous:
            # A pending wire transaction may still deliver its terminal
            # result, but an interrupted cycle is no longer a live scheduler
            # and must not leave the worker permanently busy.
            if self._pending is None:
                self._finish_cycle()
            else:
                self._cycle = None
        self._notify(ContinuousPollChanged(command.command_id, False))
        self._notify(CommandAccepted(command.command_id, "stop_continuous_poll"))

    def _handle_replace_fake_values(self, command: _Command) -> None:
        device_id, values = command.value
        if self._session is None or self._session.spec.mode is not BusMode.SLAVE:
            self._reject(command, "fake values require running slave mode")
            return
        if device_id not in self._session.runtime:
            self._reject(command, "unknown device")
            return
        state = self._session.runtime[device_id]
        self._session.runtime[device_id] = replace(state, values=dict(values))
        self._notify(CommandAccepted(command.command_id, "replace_fake_values"))

    def _handle_cancel(self, command: _Command) -> None:
        if self._pending and self._pending.transaction_id == command.value:
            self._finish_pending("cancelled", "cancelled")
            self._notify(CommandAccepted(command.command_id, "cancel", command.value))
        else:
            self._reject(command, "transaction not pending")

    def _master_ready(self, command: _Command) -> bool:
        if self._session is None or self._serial is None:
            self._reject(command, "no open session")
            return False
        if self._session.spec.mode is not BusMode.MASTER:
            self._reject(command, "master operation requires master mode")
            return False
        return True

    def _busy_for_manual(self) -> bool:
        return self._pending is not None or bool(self._scheduled) or self._cycle is not None or self._continuous

    def _busy_for_schedule(self) -> bool:
        return self._pending is not None or bool(self._scheduled) or self._cycle is not None or self._continuous

    def _queue_cycle(self, continuous: bool) -> None:
        cycle = _PollCycle(uuid4(), continuous)
        self._cycle = cycle
        self._cycle_succeeded = self._cycle_failed = 0
        self._scheduled.extend(_ScheduledOperation(device, self._canonical_poll(device), cycle.cycle_id, index) for index, device in enumerate(CANONICAL_DEVICE_IDS))
        self._notify(PollCycleStarted(cycle.cycle_id, continuous, len(CANONICAL_DEVICE_IDS)))

    def _advance_scheduler(self) -> None:
        if self._pending is not None:
            return
        if self._scheduled:
            self._run_item(self._scheduled.popleft())
            return
        # A completed continuous cycle deliberately clears ``_cycle`` while
        # retaining the next-cycle deadline.  The deadline, not the previous
        # cycle object, is the scheduler's wake-up state.
        if self._continuous and self._continuous_next_at is not None and self._clock() >= self._continuous_next_at:
            self._continuous_next_at = None
            self._queue_cycle(True)
        elif self._cycle and not self._cycle.continuous:
            self._finish_cycle()

    def _run_item(self, item: _ScheduledOperation, transaction_id: UUID | None = None) -> None:
        session = self._session
        if session is None:
            return
        device = session.devices.get(item.device_id)
        tx_id = transaction_id or uuid4()
        if device is None:
            self._terminal_item(item, tx_id, "compile_error", "unknown device")
            return
        operation = replace(item.operation, address=device.address)
        try:
            if not self._configure_serial(device.serial):
                raise OSError("serial format readback failed")
            plan = session.adapters[device.protocol].compile(operation, session.spec.response_timeout_s)
            self._validate_plan(plan)
            if ProtocolId(getattr(plan, "protocol", device.protocol)) is not device.protocol:
                raise ValueError("adapter returned a plan for the wrong protocol")
        except (ValueError, TypeError, AttributeError) as exc:
            self._terminal_item(item, tx_id, "compile_error", str(exc))
            return
        except BaseException as exc:
            self._fault(self._last_command_id, exc)
            return
        self._pending = _Pending(tx_id, device.device_id, operation, plan, 0, cycle_id=item.cycle_id, ordinal=item.ordinal)
        self._notify(TransactionStarted(tx_id, device.device_id, self._operation_summary(operation), item.cycle_id, item.ordinal))
        self._send_current_step()

    def _send_current_step(self) -> None:
        pending, session = self._pending, self._session
        if pending is None or session is None:
            return
        steps = tuple(pending.plan.steps)
        if pending.step_index >= len(steps):
            self._finish_pending("success", None)
            return
        step = steps[pending.step_index]
        device = session.devices[pending.device_id]
        tx = bytes(step.tx)
        traffic = self._record_traffic(Direction.TX, tx, frozenset({"WRITE_ATTEMPT"}), pending.device_id, pending.transaction_id, device.protocol)
        self._record_tx_frame(tx, traffic.seq if traffic else None, device.protocol, pending.device_id, pending.transaction_id)
        self._tx_history.append(tx)
        try:
            written = self._serial.write(tx)
            if written is not None and int(written) != len(tx):
                raise OSError("serial write was short")
            flush = getattr(self._serial, "flush", None)
            if flush:
                flush()
        except BaseException as exc:
            self._fault(self._last_command_id, exc)
            return
        now = self._clock()
        if step.expectation is None:
            recovery = float(getattr(step, "recovery_delay_s", 0.0) or 0.0)
            self._pending = replace(pending, waiting_for_response=False, sent_at=now, recovery_until=now + recovery if recovery else None)
            if not recovery:
                self._finish_pending("success", None)
        else:
            timeout = float(getattr(step, "timeout_s", session.spec.response_timeout_s))
            self._pending = replace(pending, waiting_for_response=True, sent_at=now, deadline=now + timeout)

    def _advance_pending(self) -> None:
        pending = self._pending
        if pending is None:
            return
        now = self._clock()
        if pending.recovery_until is not None:
            if now < pending.recovery_until:
                return
            self._pending = replace(pending, recovery_until=None)
            if pending.step_index + 1 >= len(pending.plan.steps):
                self._finish_pending("success", None)
            else:
                self._pending = replace(self._pending, step_index=pending.step_index + 1)
                self._send_current_step()
        elif pending.waiting_for_response and pending.deadline is not None and now >= pending.deadline:
            self._finish_pending("timeout", "response timeout")

    def _finish_pending(self, result_kind: str, detail: Any) -> None:
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        if result_kind == "success":
            self._reduce_master_result(pending, detail)
            self._notify(TransactionCompleted(pending.transaction_id, pending.device_id, detail, pending.cycle_id, pending.ordinal))
            self._terminal_cycle_result(pending, True)
        else:
            self._notify(TransactionFailed(pending.transaction_id, pending.device_id, result_kind, str(detail or ""), pending.cycle_id, pending.ordinal))
            self._terminal_cycle_result(pending, False)

    def _terminal_item(self, item: _ScheduledOperation, tx_id: UUID, kind: str, detail: str) -> None:
        self._notify(TransactionFailed(tx_id, item.device_id, kind, detail, item.cycle_id, item.ordinal))
        if item.cycle_id and self._cycle:
            self._cycle_failed += 1
            self._maybe_finish_cycle()

    def _terminal_cycle_result(self, pending: _Pending, succeeded: bool) -> None:
        if pending.cycle_id is None or self._cycle is None:
            return
        if succeeded:
            self._cycle_succeeded += 1
        else:
            self._cycle_failed += 1
        self._maybe_finish_cycle()

    def _maybe_finish_cycle(self) -> None:
        if self._cycle is None:
            return
        if self._cycle_succeeded + self._cycle_failed < self._cycle.total or self._pending is not None or self._scheduled:
            return
        self._finish_cycle()

    def _finish_cycle(self) -> None:
        cycle = self._cycle
        if cycle is None:
            return
        self._notify(PollCycleCompleted(cycle.cycle_id, cycle.continuous, self._cycle_succeeded, self._cycle_failed))
        if cycle.continuous and self._continuous and self._session is not None:
            self._continuous_next_at = self._clock() + self._session.spec.continuous_cycle_delay_s
            self._cycle = None
        else:
            self._cycle = None
            self._continuous_next_at = None

    def _poll_serial(self) -> None:
        serial = self._serial
        if serial is None:
            return
        try:
            waiting = int(getattr(serial, "in_waiting", 0) or 0)
            if waiting <= 0:
                return
            data = bytes(serial.read(min(waiting, 4096)))
            if data:
                traffic = self._record_traffic(Direction.RX, data, frozenset())
                self._feed_rx(data, traffic.seq if traffic else None)
        except BaseException as exc:
            self._fault(self._last_command_id, exc)

    def _feed_rx(self, data: bytes, seq: int | None) -> None:
        session = self._session
        if session is None:
            return
        events: list[tuple[FrameEvent, Any]] = []
        for protocol in ProtocolId:
            framer = self._framers.get(protocol)
            if framer is None:
                continue
            if self._framer_first_seq.get(protocol) is None and data:
                self._framer_first_seq[protocol] = seq
            try:
                frames = list(self._feed_protocol_framer(protocol, framer, data))
            except BaseException as exc:
                frames = []
                self._append_frame(self._make_frame(protocol, data, Direction.RX, self._framer_first_seq.get(protocol), seq, None, ParseError(str(exc), data)))
            for raw in frames:
                first = self._framer_first_seq.get(protocol) or seq
                try:
                    decoded = session.adapters[protocol].decode(raw, Direction.RX, session.spec.mode)
                    error = None
                except BaseException as exc:
                    decoded, error = None, ParseError(str(exc), raw)
                events.append((self._make_frame(protocol, raw, Direction.RX, first, seq, decoded, error), decoded))
                self._framer_first_seq[protocol] = seq if self._framer_has_pending_bytes(framer) else None
        # Framers normally emit each candidate once, but a third-party framer
        # may repeat a candidate after a reset.  Deduplicate only within the
        # same protocol/span/raw tuple; equal bytes from the other recognizer
        # remain evidence of an ambiguous protocol.
        unique_events: list[tuple[FrameEvent, Any]] = []
        seen: set[tuple[ProtocolId, bytes, int, int]] = set()
        for event, decoded in events:
            identity = (event.protocol, event.raw_frame, event.first_traffic_seq, event.last_traffic_seq)
            if identity in seen:
                continue
            seen.add(identity)
            unique_events.append((event, decoded))
        events = unique_events
        spans: dict[tuple[bytes, int, int], int] = {}
        for event, _ in events:
            if event.error is None:
                key = (event.raw_frame, event.first_traffic_seq, event.last_traffic_seq)
                spans[key] = spans.get(key, 0) + 1
        for event, decoded in events:
            key = (event.raw_frame, event.first_traffic_seq, event.last_traffic_seq)
            ambiguous = spans.get(key, 0) > 1
            event = replace(event, flags=frozenset({"AMBIGUOUS_PROTOCOL"}) if ambiguous else frozenset(), device_id=None if ambiguous else self._device_for_decoded(decoded))
            self._append_frame(event)
            if event.error is not None or ambiguous:
                if ambiguous and session.spec.mode is BusMode.SLAVE:
                    self._notify(SlaveRequestIgnored(event.frame_id, event.protocol, getattr(decoded, "address", None), "ambiguous_protocol"))
                elif event.error is not None and session.spec.mode is BusMode.SLAVE:
                    self._notify(SlaveRequestIgnored(event.frame_id, event.protocol, None, "parse_error"))
                elif event.error is not None and session.spec.mode is BusMode.MASTER and self._pending is not None:
                    target_protocol = session.devices[self._pending.device_id].protocol
                    if event.protocol is target_protocol:
                        self._finish_pending("protocol_error", event.error.message)
                continue
            if session.spec.mode is BusMode.MASTER:
                self._match_master(decoded, event)
            elif session.spec.mode is BusMode.PASSIVE:
                self._observe_passive(decoded, event)
            elif session.spec.mode is BusMode.SLAVE:
                self._handle_slave(decoded, event)

    def _observe_passive(self, decoded: Any, event: FrameEvent) -> None:
        """Reduce addressed passive observations into the device snapshot.

        Passive capture has no physical TX/RX distinction: requests and
        responses are both observed as RX.  The adapter's decoded ``kind`` is
        therefore authoritative.  Request metadata is retained on the
        addressed state, while only value-bearing responses emit a public
        state event.  ``replace(state, values=...)`` intentionally preserves
        pending parameter/data metadata from a preceding request.
        """
        session = self._session
        if session is None or decoded is None:
            return
        device_id = session.routes.get((event.protocol, getattr(decoded, "address", None)))
        if device_id is None:
            return
        state = session.runtime[device_id]
        kind = str(getattr(decoded, "kind", "")).lower()
        if event.protocol is ProtocolId.PARTLOW_P1160:
            parameter = getattr(decoded, "parameter", None)
            if kind == "type2_request" and parameter:
                session.runtime[device_id] = replace(state, pending_parameter=parameter, pending_data=None)
                return
            if kind == "type3_request" and parameter:
                session.runtime[device_id] = replace(state, pending_parameter=parameter, pending_data=getattr(decoded, "data", None))
                return
            if not kind.endswith("_response") or parameter not in {"M", "S"} or getattr(decoded, "data", None) is None:
                return
            try:
                from .protocols.partlow import decode_data
                value = decode_data(decoded.data)
            except (ValueError, TypeError):
                return
            new_state = replace(state, values={**dict(state.values), parameter: value})
        elif event.protocol is ProtocolId.MKS_275:
            if kind == "request":
                # Keep the request command as diagnostic metadata.  The
                # response reduction below deliberately does not clear it.
                session.runtime[device_id] = replace(state, pending_parameter=getattr(decoded, "command", None), pending_data=None)
                return
            if kind != "response" or getattr(decoded, "prefix", None) != "*" or not getattr(decoded, "payload", None):
                return
            new_state = replace(state, values={**dict(state.values), "pressure": decoded.payload})
        else:
            return
        session.runtime[device_id] = new_state
        self._notify(DeviceStateChanged(device_id, event.frame_id, new_state))

    @staticmethod
    def _feed_protocol_framer(protocol: ProtocolId, framer: Any, data: bytes) -> list[bytes]:
        """Feed a recognizer without letting another protocol's delimiter
        poison its candidate.  MKS uses ``*`` as a response prefix while
        Partlow uses it as a terminator; a coalesced ``L...*#...\\r`` read
        therefore needs the stray Partlow terminator discarded when the MKS
        recognizer has not yet seen a CR.
        """
        if protocol is ProtocolId.MKS_275:
            buffer = getattr(framer, "_buffer", None)
            if isinstance(buffer, (bytearray, bytes)) and b"\r" not in buffer:
                marker = data.find(b"#")
                stray = data.find(b"*")
                if marker >= 0 and isinstance(buffer, bytearray) and b"*" in buffer:
                    # A Partlow terminator may have arrived in an earlier
                    # read.  Drop that incomplete foreign candidate before
                    # accepting the MKS request in this read.
                    offset = buffer.find(b"*")
                    del buffer[: offset + 1]
                if stray >= 0 and marker > stray:
                    data = data[stray + 1:]
        return framer.feed(data)

    def _match_master(self, decoded: Any, event: FrameEvent) -> None:
        pending, session = self._pending, self._session
        if pending is None or not pending.waiting_for_response or session is None:
            return
        if event.protocol is not session.devices[pending.device_id].protocol:
            return
        if self._is_possible_echo(event.raw_frame):
            for index in range(len(self._frames) - 1, -1, -1):
                if self._frames[index].frame_id == event.frame_id:
                    self._frames[index] = replace(event, flags=frozenset(set(event.flags) | {"POSSIBLE_ECHO"}))
                    break
            return
        expected = pending.plan.steps[pending.step_index].expectation
        try:
            result = session.adapters[event.protocol].match(expected, decoded)
        except BaseException as exc:
            self._finish_pending("protocol_error", str(exc))
            return
        value = str(getattr(result, "value", result)).lower()
        if value in {"success", "match_success", "matched", "ok"}:
            step = pending.plan.steps[pending.step_index]
            recovery = float(getattr(step, "recovery_delay_s", 0.0) or 0.0)
            if pending.step_index + 1 >= len(pending.plan.steps):
                if recovery:
                    self._pending = replace(pending, waiting_for_response=False, deadline=None, recovery_until=self._clock() + recovery)
                else:
                    self._finish_pending("success", decoded)
            else:
                self._pending = replace(pending, waiting_for_response=False, deadline=None, recovery_until=self._clock() + recovery if recovery else None)
                if not recovery:
                    self._pending = replace(self._pending, step_index=pending.step_index + 1)
                    self._send_current_step()
        elif value in {"negative", "nack", "negative_ack"}:
            self._finish_pending("negative_ack", self._result_detail(decoded))
        elif value in {"malformed", "protocol_error"}:
            self._finish_pending("protocol_error", self._result_detail(decoded))

    def _handle_slave(self, decoded: Any, event: FrameEvent) -> None:
        session = self._session
        if session is None:
            return
        address = getattr(decoded, "address", None)
        device_id = session.routes.get((event.protocol, address))
        if self._slave_reply is not None:
            self._notify(SlaveRequestIgnored(event.frame_id, event.protocol, address, "reply_pending"))
            return
        if device_id is None:
            self._notify(SlaveRequestIgnored(event.frame_id, event.protocol, address, "unknown_address"))
            return
        state = session.runtime[device_id]
        try:
            from .protocols.base import SlaveState
            adapter_state = SlaveState(state.address, dict(state.values), state.pending_parameter, state.pending_data)
            decision = session.adapters[event.protocol].respond(decoded, adapter_state)
        except BaseException:
            self._notify(SlaveRequestIgnored(event.frame_id, event.protocol, address, "responder_error"))
            return
        if decision is None:
            return
        state_after = getattr(decision, "state_after", None)
        if state_after is not None:
            state = DeviceRuntimeState(device_id, state.address, dict(state_after.values), state_after.pending_parameter, state_after.pending_data)
            session.runtime[device_id] = state
            self._notify(DeviceStateChanged(device_id, event.frame_id, state))
        data = getattr(decision, "data", None)
        if data is None:
            return
        recovery = float(getattr(decision, "recovery_s", 0.0) or 0.0)
        due = self._clock() + recovery
        earliest_ns = int(getattr(decision, "earliest_send_ns", 0) or 0)
        if earliest_ns:
            due = max(due, earliest_ns / 1_000_000_000)
        self._slave_reply = _DelayedReply(due, bytes(data), event.protocol, device_id, event.frame_id)
        self._advance_slave_reply()

    def _advance_slave_reply(self) -> None:
        reply = self._slave_reply
        if reply is None or self._clock() < reply.due:
            return
        self._slave_reply = None
        self._write_reply(reply)

    def _write_reply(self, reply: _DelayedReply) -> None:
        session = self._session
        if self._serial is None or session is None or session.spec.mode is not BusMode.SLAVE:
            return
        traffic = self._record_traffic(Direction.TX, reply.data, frozenset({"WRITE_ATTEMPT", "SLAVE_REPLY"}), reply.device_id, None, reply.protocol)
        self._record_tx_frame(reply.data, traffic.seq if traffic else None, reply.protocol, reply.device_id, None)
        try:
            written = self._serial.write(reply.data)
            if written is not None and int(written) != len(reply.data):
                raise OSError("serial write was short")
            flush = getattr(self._serial, "flush", None)
            if flush:
                flush()
        except BaseException as exc:
            self._fault(self._last_command_id, exc)

    def _record_traffic(self, direction: Direction, data: bytes, flags: frozenset[str], device_id: DeviceId | None = None, transaction_id: UUID | None = None, protocol: ProtocolId | None = None) -> TrafficRecord | None:
        session = self._session
        if session is None:
            return None
        with self._lock:
            dropped = None
            if len(self._traffic) >= self._traffic_capacity:
                self._traffic_dropped += 1
                dropped = self._traffic_dropped
            record = TrafficRecord(self._next_seq, time.monotonic_ns(), self._wall_clock(), direction, bytes(data), session.spec.mode, protocol if direction is Direction.TX else None, device_id if direction is Direction.TX else None, transaction_id, frozenset(flags))
            self._next_seq += 1
            self._traffic.append(record)
        if dropped is not None:
            self._notify(RawRecordsDropped(dropped))
        return record

    def _append_frame(self, event: FrameEvent) -> None:
        with self._lock:
            dropped = None
            if len(self._frames) >= self._frame_capacity:
                self._frame_dropped += 1
                dropped = self._frame_dropped
            self._frames.append(event)
        if dropped is not None:
            self._notify(FrameRecordsDropped(dropped))

    def _record_tx_frame(self, raw: bytes, seq: int | None, protocol: ProtocolId, device_id: DeviceId | None, transaction_id: UUID | None) -> None:
        session = self._session
        if session is None:
            return
        try:
            decoded = session.adapters[protocol].decode(raw, Direction.TX, session.spec.mode)
            error = None
        except BaseException as exc:
            decoded, error = None, ParseError(str(exc), raw)
        self._append_frame(self._make_frame(protocol, raw, Direction.TX, seq, seq, decoded, error, device_id, transaction_id))

    def _make_frame(self, protocol: ProtocolId, raw: bytes, direction: Direction, first: int | None, last: int | None, decoded: Any, error: ParseError | None, device_id: DeviceId | None = None, transaction_id: UUID | None = None) -> FrameEvent:
        frame = FrameEvent(self._next_frame_id, time.monotonic_ns(), direction, bytes(raw), first or 0, last or first or 0, protocol, device_id, transaction_id, decoded, error)
        self._next_frame_id += 1
        return frame

    def _device_for_decoded(self, decoded: Any) -> DeviceId | None:
        if self._session is None or decoded is None:
            return None
        try:
            return self._session.routes.get((ProtocolId(decoded.protocol), getattr(decoded, "address", None)))
        except (AttributeError, ValueError):
            return None

    @staticmethod
    def _framer_has_pending_bytes(framer: Any) -> bool:
        for name in ("_buffer", "_buf", "buffer"):
            try:
                return bool(getattr(framer, name))
            except AttributeError:
                continue
        return False

    def _configure_serial(self, fmt: Any) -> bool:
        serial = self._serial
        if serial is None:
            return False
        changed = False
        for name in ("baudrate", "bytesize", "parity", "stopbits", "xonxoff", "rtscts", "dsrdtr"):
            value = getattr(fmt, name)
            try:
                if not hasattr(serial, name) or getattr(serial, name) != value:
                    setattr(serial, name, value)
                    changed = True
                if getattr(serial, name) != value:
                    return False
            except BaseException:
                return False
        if changed:
            self._reset_framers()
        return True

    def _open_serial(self, spec: SessionSpec) -> Any:
        fmt = spec.listen_serial
        kwargs = dict(port=spec.port, baudrate=fmt.baudrate, bytesize=fmt.bytesize, parity=fmt.parity, stopbits=fmt.stopbits, xonxoff=fmt.xonxoff, rtscts=fmt.rtscts, dsrdtr=fmt.dsrdtr, timeout=0, write_timeout=0)
        try:
            handle = self._serial_factory(**kwargs)
        except TypeError as first:
            try:
                handle = self._serial_factory(fmt)
            except TypeError:
                try:
                    handle = self._serial_factory()
                except TypeError:
                    raise first
        self._serial = handle
        if not self._configure_serial(fmt):
            self._close_handle_direct()
            raise OSError("serial format readback failed")
        return handle

    def _adapter_for(self, protocol: ProtocolId, device: DeviceConfig) -> Any:
        for key in (protocol, protocol.value):
            if key in self._adapter_overrides:
                return self._adapter_overrides[key]
        candidates = {
            ProtocolId.PARTLOW_P1160: ("yes_rs485.protocols.partlow", "PartlowAdapter"),
            ProtocolId.MKS_275: ("yes_rs485.protocols.mks275", "MKS275Adapter"),
        }
        module_name, class_name = candidates[protocol]
        cls = getattr(importlib.import_module(module_name), class_name)
        return cls(device_address=device.address, baudrate=device.serial.baudrate)

    def _new_framer(self, protocol: ProtocolId, role: BusMode) -> Any:
        try:
            return self._session.adapters[protocol].new_framer(role)
        except (AttributeError, TypeError):
            return _DelimiterFramer(b"*" if protocol is ProtocolId.PARTLOW_P1160 else b"\r")

    def _reset_framers(self) -> None:
        for framer in self._framers.values():
            try:
                framer.reset()
            except AttributeError:
                pass
        for protocol in ProtocolId:
            self._framer_first_seq[protocol] = None

    @staticmethod
    def _validate_plan(plan: Any) -> None:
        if not hasattr(plan, "steps") or not tuple(plan.steps):
            raise ValueError("adapter returned no transaction plan")
        for step in plan.steps:
            if not isinstance(step.tx, (bytes, bytearray, memoryview)) or not step.tx:
                raise ValueError("transaction step tx must be bytes")
            if float(getattr(step, "timeout_s", 0) or 0) < 0 or float(getattr(step, "recovery_delay_s", 0) or 0) < 0:
                raise ValueError("transaction timing cannot be negative")

    def _cancel_transactions(self, reason: str) -> None:
        if self._pending:
            self._finish_pending(reason, reason)
        self._scheduled.clear()
        self._cycle = None
        self._continuous = False
        self._continuous_next_at = None

    def _cancel_all(self) -> None:
        self._cancel_transactions("cancelled")
        self._slave_reply = None

    def _fault(self, command_id: UUID | None, exc: BaseException, transition: bool = True) -> None:
        self._notify(Faulted(command_id, f"{type(exc).__name__}: {exc}"))
        self._cancel_all()
        self._close_handle_direct()
        self._session = None
        self._framers.clear()
        if transition and not self._stop_requested:
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

    def _set_state(self, new_state: WorkerState, command_id: UUID | None, summary: Any) -> None:
        with self._lock:
            old = self._state
            self._state = new_state
        if old is not new_state:
            self._notify(StateChanged(command_id, old, new_state, summary))

    def _notify(self, value: Any) -> None:
        self._notifications.put(value)

    def _reject(self, command: _Command, reason: str) -> None:
        self._notify(CommandRejected(command.command_id, reason))

    def _summary(self) -> Mapping[str, Any] | None:
        return {"mode": self._session.spec.mode.value, "port": self._session.spec.port} if self._session else None

    @staticmethod
    def _operation_summary(operation: MasterOperation) -> Mapping[str, Any]:
        return {"kind": operation.kind, "parameter": operation.parameter, "command": operation.command}

    @staticmethod
    def _canonical_poll(device_id: DeviceId) -> MasterOperation:
        return MasterOperation(kind="RD") if device_id is DeviceId.GP else MasterOperation(kind="read", parameter="M")

    @staticmethod
    def _result_detail(decoded: Any) -> str:
        return "" if decoded is None else str(getattr(decoded, "detail", getattr(decoded, "payload", decoded)))

    def _reduce_master_result(self, pending: _Pending, result: Any) -> None:
        session = self._session
        if session is None:
            return
        state = session.runtime[pending.device_id]
        values = dict(state.values)
        decoded = result
        if state.device_id is DeviceId.GP and pending.operation.kind.upper() in {"RD", "READ", "READ_PRESSURE"}:
            payload = getattr(decoded, "payload", None)
            if payload is not None:
                values["pressure"] = payload
        elif state.device_id is not DeviceId.GP and getattr(decoded, "parameter", None):
            value = getattr(decoded, "data", None)
            if value is not None:
                try:
                    from .protocols.partlow import decode_data
                    value = decode_data(value)
                except (ValueError, TypeError):
                    pass
                values[decoded.parameter] = value
        if values != dict(state.values):
            new_state = replace(state, values=values)
            session.runtime[pending.device_id] = new_state
            self._notify(DeviceStateChanged(pending.device_id, None, new_state))

    def _is_possible_echo(self, raw: bytes) -> bool:
        return bool(raw and any(raw == sent for sent in self._tx_history))

    def _next_wait(self) -> float:
        waits = [self._poll_interval_s]
        if self._pending:
            if self._pending.deadline is not None:
                waits.append(max(0.0, self._pending.deadline - self._clock()))
            if self._pending.recovery_until is not None:
                waits.append(max(0.0, self._pending.recovery_until - self._clock()))
        if self._slave_reply:
            waits.append(max(0.0, self._slave_reply.due - self._clock()))
        if self._continuous_next_at is not None:
            waits.append(max(0.0, self._continuous_next_at - self._clock()))
        return max(0.0005, min(waits))


__all__ = [
    "SerialWorker", "WorkerState", "TrafficBatch", "FrameBatch", "StateChanged",
    "ModeChanged", "ModeChangeFailed", "CommandAccepted", "CommandRejected",
    "ContinuousPollChanged", "TransactionStarted", "TransactionCompleted",
    "TransactionFailed", "PollCycleStarted", "PollCycleCompleted",
    "DeviceStateChanged", "SlaveRequestIgnored", "RawRecordsDropped",
    "FrameRecordsDropped", "Faulted", "TransactionPlan", "TransactionStep",
]
