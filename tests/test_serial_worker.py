"""Focused whole-bus worker tests using an in-memory serial endpoint."""

from __future__ import annotations

from collections import deque
from decimal import Decimal
import inspect
import time

import pytest

from yes_rs485.models import (
    BusMode,
    DeviceConfig,
    DeviceId,
    Direction,
    MasterOperation,
    ProtocolId,
    SerialFormat,
    SessionSpec,
    default_devices,
)
from yes_rs485.serial_worker import (
    CommandRejected,
    DeviceStateChanged,
    ModeChanged,
    PollCycleCompleted,
    SerialWorker,
    SlaveRequestIgnored,
    TransactionCompleted,
    TransactionFailed,
    WorkerState,
)


class FakeSerial:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.incoming: deque[bytes] = deque()
        self.writes: list[bytes] = []
        self.closed = False
        for name in ("baudrate", "bytesize", "parity", "stopbits", "xonxoff", "rtscts", "dsrdtr"):
            setattr(self, name, kwargs[name])

    @property
    def in_waiting(self) -> int:
        return len(self.incoming[0]) if self.incoming else 0

    def read(self, size: int = 4096) -> bytes:
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


def wait_for(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    assert predicate()


@pytest.fixture
def endpoint():
    return FakeSerial(
        port="FAKE", baudrate=9600, bytesize=7, parity="E", stopbits=1,
        xonxoff=False, rtscts=False, dsrdtr=False,
    )


@pytest.fixture
def worker(endpoint):
    result = SerialWorker(serial_factory=lambda **kwargs: endpoint, poll_interval_s=0.001)
    result.start()
    yield result
    result.stop()


def make_spec(mode: BusMode = BusMode.MASTER, devices=None, **kwargs) -> SessionSpec:
    return SessionSpec(
        mode=mode,
        port="FAKE",
        devices=tuple(default_devices() if devices is None else devices),
        listen_serial=SerialFormat(9600, 7, "E", 1),
        response_timeout_s=kwargs.get("response_timeout_s", 0.08),
        continuous_cycle_delay_s=kwargs.get("continuous_cycle_delay_s", 0.01),
    )


def open_worker(worker, spec):
    worker.open(spec)
    wait_for(lambda: worker.state is WorkerState.RUNNING)
    worker.drain_notifications()


def feed_poll_response(endpoint: FakeSerial, tx: bytes) -> None:
    if tx.startswith(b"L"):
        address = tx[1:tx.find(b"M")]
        endpoint.incoming.append(b"L" + address + b"M00000A*")
    elif tx.startswith(b"#"):
        address = tx[1:3]
        endpoint.incoming.append(b"*" + address + b"_0.00E+00\r")


def test_open_all_modes_has_no_arm_path(worker, endpoint):
    assert "arm" not in inspect.signature(worker.open).parameters
    with pytest.raises(TypeError):
        worker.open(make_spec(), arm=object())
    for mode in (BusMode.PASSIVE, BusMode.MASTER, BusMode.SLAVE):
        open_worker(worker, make_spec(mode))
        if mode is BusMode.SLAVE:
            # One explicit listen format is selected for the UART; saved GP
            # and Partlow machine profiles need not match it.
            assert (endpoint.bytesize, endpoint.parity) == (7, "E")
        worker.close()
        wait_for(lambda: worker.state is WorkerState.IDLE)


def test_dual_passive_capture_is_hard_zero_write(worker, endpoint):
    open_worker(worker, make_spec(BusMode.PASSIVE))
    endpoint.incoming.append(b"L2M00000A*#01RD \r")
    wait_for(lambda: len(worker.drain_frames().frames) >= 2)
    assert endpoint.writes == []


def test_passive_observation_reduces_addressed_values_and_keeps_request_metadata(worker, endpoint):
    open_worker(worker, make_spec(BusMode.PASSIVE))
    endpoint.incoming.append(b"L2M?*L2M00121A*#01RD \r*01_1.23E+00\r")
    wait_for(lambda: len([item for item in worker.drain_notifications() if isinstance(item, DeviceStateChanged)]) >= 2)
    # The wait predicate consumed notifications, so drain the frame/ring and
    # replay a smaller assertion using the immutable runtime snapshot events
    # captured through a second response pair below.
    endpoint.incoming.append(b"L2M00121A*#01RD \r*01_1.23E+00\r")
    snapshots = []
    def collect_snapshots() -> bool:
        snapshots.extend(item for item in worker.drain_notifications() if isinstance(item, DeviceStateChanged))
        return len(snapshots) >= 2
    wait_for(collect_snapshots)
    by_device = {item.device_id: item.state_snapshot for item in snapshots}
    assert by_device[DeviceId.PID_1].values["M"] == Decimal("1.2")
    assert by_device[DeviceId.PID_1].pending_parameter == "M"
    assert by_device[DeviceId.GP].values["pressure"] == "1.23E+00"
    assert endpoint.writes == []
    frames = worker.drain_frames().frames
    assert {frame.protocol for frame in frames} == {ProtocolId.PARTLOW_P1160, ProtocolId.MKS_275}
    command_id = worker.submit(DeviceId.PID_1, MasterOperation("read", parameter="M"))
    wait_for(lambda: any(isinstance(item, CommandRejected) and item.command_id == command_id for item in worker.drain_notifications()))
    assert endpoint.writes == []


def test_poll_targets_device_address_and_reuses_one_handle(worker, endpoint):
    devices = list(default_devices())
    devices[1] = DeviceConfig(DeviceId.PID_1, ProtocolId.PARTLOW_P1160, 42, SerialFormat(4800, 7, "E", 1), devices[1].fake_values)
    spec = make_spec(devices=devices)
    open_worker(worker, spec)
    first = worker.poll(DeviceId.GP)
    wait_for(lambda: endpoint.writes)
    assert (endpoint.baudrate, endpoint.bytesize, endpoint.parity, endpoint.stopbits) == (9600, 8, "N", 1)
    feed_poll_response(endpoint, endpoint.writes[-1])
    wait_for(lambda: worker.pending_transaction_id is None)
    second = worker.poll(DeviceId.PID_1)
    wait_for(lambda: len(endpoint.writes) == 2)
    assert endpoint.writes[-1] == b"L42M?*"
    assert (endpoint.baudrate, endpoint.bytesize, endpoint.parity, endpoint.stopbits) == (4800, 7, "E", 1)
    feed_poll_response(endpoint, endpoint.writes[-1])
    wait_for(lambda: worker.pending_transaction_id is None)
    completed = [item for item in worker.drain_notifications() if isinstance(item, TransactionCompleted)]
    assert {item.device_id for item in completed} == {DeviceId.GP, DeviceId.PID_1}
    assert first != second


def test_poll_all_continues_after_timeout_and_completes_cycle(worker, endpoint):
    open_worker(worker, make_spec(response_timeout_s=0.01))
    worker.poll_all_once()
    # Answer every other device; the remaining six must fail independently.
    seen = 0
    notifications = []
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if len(endpoint.writes) > seen:
            tx = endpoint.writes[-1]
            seen = len(endpoint.writes)
            if seen % 2:
                feed_poll_response(endpoint, tx)
        notifications.extend(worker.drain_notifications())
        if any(isinstance(item, PollCycleCompleted) for item in notifications):
            break
        time.sleep(0.002)
    cycle = [item for item in notifications if isinstance(item, PollCycleCompleted)]
    assert len(endpoint.writes) == 12
    assert cycle and cycle[-1].succeeded == 6 and cycle[-1].failed == 6


def test_continuous_poll_repeats_after_delay_and_stop_blocks_next_tx(worker, endpoint):
    open_worker(worker, make_spec(response_timeout_s=0.05, continuous_cycle_delay_s=0.01))
    worker.start_continuous_poll()
    seen = 0
    notifications = []
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and len(endpoint.writes) < 13:
        if len(endpoint.writes) > seen:
            tx = endpoint.writes[-1]
            seen = len(endpoint.writes)
            feed_poll_response(endpoint, tx)
        notifications.extend(worker.drain_notifications())
        time.sleep(0.001)
    assert len(endpoint.writes) >= 13
    # The thirteenth request proves the next cycle was scheduled after the
    # first PollCycleCompleted and configured delay.
    worker.stop_continuous_poll()
    feed_poll_response(endpoint, endpoint.writes[-1])
    wait_for(lambda: worker.pending_transaction_id is None)
    time.sleep(0.04)
    assert len(endpoint.writes) == 13
    notifications.extend(worker.drain_notifications())
    assert sum(isinstance(item, PollCycleCompleted) for item in notifications) >= 1


def test_mode_switch_resets_capture_without_transmitting(worker, endpoint):
    open_worker(worker, make_spec(BusMode.MASTER))
    command_id = worker.set_mode(BusMode.PASSIVE)
    wait_for(lambda: worker.mode is BusMode.PASSIVE)
    assert any(isinstance(item, ModeChanged) and item.command_id == command_id for item in worker.drain_notifications())
    endpoint.incoming.append(b"L2M00000A*")
    time.sleep(0.03)
    assert endpoint.writes == []


def test_slave_routes_partlow_and_mks_by_protocol_and_address(worker, endpoint):
    open_worker(worker, make_spec(BusMode.SLAVE))
    endpoint.incoming.append(b"L2M?*")
    wait_for(lambda: endpoint.writes == [b"L2M00000A*"])
    endpoint.incoming.append(b"#01RD \r")
    wait_for(lambda: len(endpoint.writes) == 2)
    assert endpoint.writes[-1] == b"*01_0.00E+00\r"
    state_events = [item for item in worker.drain_notifications() if isinstance(item, DeviceStateChanged)]
    assert {item.device_id for item in state_events} == {DeviceId.PID_1, DeviceId.GP}
    endpoint.incoming.append(b"L99M?*")
    wait_for(lambda: any(isinstance(item, SlaveRequestIgnored) for item in worker.drain_notifications()))
