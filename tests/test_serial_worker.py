"""Transport invariants exercised with an in-memory serial endpoint."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
import time

import pytest

from yes_rs485.serial_worker import (
    BusMode,
    Direction,
    ProtocolId,
    SafetyArm,
    SerialWorker,
    TransactionPlan,
    TransactionStep,
    TransactionCompleted,
    TransactionFailed,
    CommandRejected,
    Faulted,
    WorkerState,
)


class FakeSerial:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.incoming: deque[bytes] = deque()
        self.writes: list[bytes] = []
        self.closed = False
        self.fail_read = False

    @property
    def in_waiting(self) -> int:
        return len(self.incoming[0]) if self.incoming else 0

    def read(self, size: int = 4096) -> bytes:
        if self.fail_read:
            raise OSError("read failed")
        if not self.incoming:
            return b""
        data = self.incoming.popleft()
        if len(data) <= size:
            return data
        self.incoming.appendleft(data[size:])
        return data[:size]

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeFramer:
    def __init__(self, delimiter: bytes = b"*"):
        self.delimiter = delimiter
        self.buffer = bytearray()

    def feed(self, data: bytes):
        self.buffer.extend(data)
        result = []
        while self.delimiter in self.buffer:
            end = self.buffer.index(self.delimiter) + len(self.delimiter)
            result.append(bytes(self.buffer[:end]))
            del self.buffer[:end]
        return result


class FakeAdapter:
    id = ProtocolId.PARTLOW_P1160

    def __init__(self):
        self.plans = {}

    def new_framer(self, role):
        return FakeFramer()

    def compile(self, operation, timeout_s):
        return self.plans.get(operation, TransactionPlan(
            self.id, (TransactionStep(bytes(operation), "reply", timeout_s),), False
        ))

    def decode(self, frame, direction, role):
        return frame

    def match(self, expected, actual):
        if actual == b"ECHO*":
            return "unrelated"
        if actual == b"NACK*":
            return "negative_ack"
        if actual == b"OK*":
            return "success"
        return "unrelated"

    def quiet_time_s(self, baudrate):
        return 0.0


def spec(mode=BusMode.MASTER, *, profile=None):
    return SimpleNamespace(
        mode=mode,
        protocol=ProtocolId.PARTLOW_P1160,
        serial=SimpleNamespace(
            port="FAKE", baudrate=4800, bytesize=7, parity="E", stopbits=1,
            xonxoff=False, rtscts=False, dsrdtr=False,
        ),
        response_timeout_s=0.08,
        raw_capacity=4,
        slave_profile=profile,
    )


def wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    assert predicate()


@pytest.fixture
def endpoint():
    return FakeSerial()


@pytest.fixture
def worker(endpoint):
    adapter = FakeAdapter()
    value = SerialWorker(
        serial_factory=lambda **kwargs: endpoint,
        adapters={ProtocolId.PARTLOW_P1160: adapter},
        traffic_capacity=4,
        frame_capacity=4,
        poll_interval_s=0.001,
    )
    value.start()
    yield value, adapter
    value.stop()


def open_master(worker):
    value, _ = worker
    command = value.open(spec(), SafetyArm.PLC_DISCONNECTED)
    wait_for(lambda: value.state == WorkerState.RUNNING)
    return command


def test_native_serial_settings_are_passed_and_passive_never_writes(endpoint):
    adapter = FakeAdapter()
    def factory(**kwargs):
        endpoint.kwargs = kwargs
        return endpoint
    worker = SerialWorker(serial_factory=factory, adapters={ProtocolId.PARTLOW_P1160: adapter})
    worker.start()
    worker.open(spec(BusMode.PASSIVE), None)
    wait_for(lambda: worker.state == WorkerState.RUNNING)
    assert endpoint.kwargs["baudrate"] == 4800
    assert endpoint.kwargs["bytesize"] == 7
    assert endpoint.kwargs["parity"] == "E"
    assert endpoint.kwargs["stopbits"] == 1
    assert endpoint.kwargs["timeout"] == 0
    assert "rs485_mode" not in endpoint.kwargs
    endpoint.incoming.append(b"L1M+0000A*")
    time.sleep(0.02)
    assert endpoint.writes == []
    transaction = worker.submit(b"M*")
    wait_for(lambda: any(isinstance(item, CommandRejected) and item.command_id == transaction for item in worker.drain_notifications()))
    worker.stop()


def test_active_arms_are_required(worker):
    value, _ = worker
    value.open(spec(), None)
    wait_for(lambda: any(isinstance(item, CommandRejected) for item in value.drain_notifications()))
    assert value.state == WorkerState.IDLE
    value.open(spec(BusMode.SLAVE, profile=object()), SafetyArm.PLC_DISCONNECTED)
    wait_for(lambda: any(isinstance(item, CommandRejected) for item in value.drain_notifications()))


def test_one_outstanding_transaction_and_split_response(worker):
    value, _ = worker
    open_master(worker)
    first = value.submit(b"Q*")
    second = value.submit(b"R*")
    wait_for(lambda: len([item for item in value.drain_notifications() if isinstance(item, CommandRejected)]) > 0)
    # The first request is written; the second is rejected as busy.
    assert value.pending_transaction_id == first
    endpoint = worker[0]._serial
    endpoint.incoming.extend([b"O", b"K*"])
    wait_for(lambda: value.pending_transaction_id is None)
    completed = value.drain_notifications()
    assert any(isinstance(item, TransactionCompleted) and item.transaction_id == first for item in completed)


def test_negative_ack_is_not_timeout(worker):
    value, _ = worker
    open_master(worker)
    transaction = value.submit(b"Q*")
    endpoint = worker[0]._serial
    wait_for(lambda: endpoint.writes)
    endpoint.incoming.append(b"NACK*")
    wait_for(lambda: value.pending_transaction_id is None)
    failed = [item for item in value.drain_notifications() if isinstance(item, TransactionFailed)]
    assert failed and failed[-1].transaction_id == transaction
    assert failed[-1].failure_kind == "negative_ack"


def test_echo_is_visible_but_cannot_complete(worker):
    value, _ = worker
    open_master(worker)
    transaction = value.submit(b"ECHO*")
    endpoint = worker[0]._serial
    wait_for(lambda: endpoint.writes)
    endpoint.incoming.append(b"ECHO*")
    time.sleep(0.02)
    assert value.pending_transaction_id == transaction
    endpoint.incoming.append(b"OK*")
    wait_for(lambda: value.pending_transaction_id is None)
    traffic = value.drain_traffic(0, 20)
    assert any("POSSIBLE_ECHO" in record.flags for record in traffic.records)


def test_timeout_and_bounded_gap(worker):
    value, _ = worker
    open_master(worker)
    value.submit(b"Q*")
    wait_for(lambda: bool(value._serial.writes), timeout=1)
    wait_for(lambda: value.pending_transaction_id is None, timeout=1)
    failed = [item for item in value.drain_notifications() if isinstance(item, TransactionFailed)]
    assert failed and failed[-1].failure_kind == "timeout"
    # The ring is small; old records are reported as a visible gap.
    for index in range(10):
        value._record_traffic(Direction.RX, bytes([index]), frozenset())
    batch = value.drain_traffic(0, 100)
    assert batch.dropped_before_seq is not None


def test_serial_fault_closes_handle(endpoint):
    adapter = FakeAdapter()
    value = SerialWorker(serial_factory=lambda **kwargs: endpoint, adapters={ProtocolId.PARTLOW_P1160: adapter}, poll_interval_s=0.001)
    value.start()
    value.open(spec(BusMode.PASSIVE), None)
    wait_for(lambda: value.state == WorkerState.RUNNING)
    endpoint.incoming.append(b"before*")
    endpoint.fail_read = True
    wait_for(lambda: endpoint.closed)
    assert value.state == WorkerState.IDLE
    assert any(isinstance(item, Faulted) for item in value.drain_notifications())
    value.stop()
