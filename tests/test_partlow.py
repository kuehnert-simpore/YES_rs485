from decimal import Decimal

import pytest

from yes_rs485.models import BusMode, Direction, MasterOperation
from yes_rs485.protocols.base import MatchResult, SlaveState
from yes_rs485.protocols.partlow import (
    PartlowAdapter,
    PartlowFramer,
    decode_data,
    encode_data,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1234"), "12340"),
        (Decimal("12.3"), "01231"),
        (Decimal("12.34"), "12342"),
        (Decimal("-1.234"), "12348"),
    ],
)
def test_partlow_numeric_vectors(value, expected):
    assert encode_data(value) == expected
    assert decode_data(expected) == value


def test_partlow_framer_split_and_coalesced_frames():
    framer = PartlowFramer()
    assert framer.feed(b"noiseL1M?") == []
    assert framer.feed(b"*L01?A*tail") == [b"L1M?*", b"L01?A*"]


def test_partlow_types_and_type3_type4_gating():
    adapter = PartlowAdapter(device_address=1)
    read = adapter.compile(MasterOperation("read", parameter="M"), 0.5)
    assert read.steps[0].tx == b"L1M?*"
    write = adapter.compile(MasterOperation("absolute_write", parameter="S", value=Decimal("12.34")), 0.5)
    assert [step.tx for step in write.steps] == [b"L1S#12342*", b"L1SI*"]
    ready = adapter.decode(b"L1S12342I*", Direction.RX, BusMode.MASTER)
    assert adapter.match(write.steps[0].expectation, ready) is MatchResult.SUCCESS
    negative = adapter.decode(b"L1S?????N*", Direction.RX, BusMode.MASTER)
    assert adapter.match(write.steps[0].expectation, negative) is MatchResult.NEGATIVE

    state = SlaveState(1, {"S": Decimal("1.0")})
    request = adapter.decode(b"L1S#00101*", Direction.RX, BusMode.SLAVE)
    reply = adapter.respond(request, state)
    assert reply.data == b"L1S00101I*"
    commit = adapter.decode(b"L1SI*", Direction.RX, BusMode.SLAVE)
    committed = adapter.respond(commit, reply.state_after)
    assert committed.data == b"L1S00101A*"
    ignored = adapter.respond(commit, state)
    assert ignored.__class__.__name__ == "NoReply"


def test_partlow_wrong_address_does_not_match():
    adapter = PartlowAdapter(device_address=1)
    expectation = adapter.compile(MasterOperation("read", parameter="M"), 1).steps[0].expectation
    frame = adapter.decode(b"L2M00000A*", Direction.RX, BusMode.MASTER)
    assert adapter.match(expectation, frame) is MatchResult.UNRELATED


def test_partlow_pv_range_sentinels_are_valid_type2_replies():
    adapter = PartlowAdapter(device_address=1)
    expectation = adapter.compile(MasterOperation("read", parameter="M"), 1).steps[0].expectation
    for suffix in (b"0", b"5"):
        frame = adapter.decode(b"L1M<??>" + suffix + b"A*", Direction.RX, BusMode.MASTER)
        assert frame.data == "<??>" + suffix.decode("ascii")
        assert adapter.match(expectation, frame) is MatchResult.SUCCESS


def test_partlow_probe_does_not_correlate_unrelated_parameter_nack():
    adapter = PartlowAdapter(device_address=1)
    expectation = adapter.compile(MasterOperation("probe"), 1).steps[0].expectation
    frame = adapter.decode(b"L1M?????N*", Direction.RX, BusMode.MASTER)
    assert adapter.match(expectation, frame) is MatchResult.UNRELATED


def test_partlow_rejects_space_parameter_instead_of_decoding_invalid_syntax():
    adapter = PartlowAdapter(device_address=1)
    with pytest.raises(ValueError):
        adapter.decode(b"L1 M00000A*", Direction.RX, BusMode.MASTER)
    with pytest.raises(ValueError):
        adapter.compile(MasterOperation("read", parameter=" "), 1)


def test_partlow_scan_table_reply_is_not_rejected_as_ordinary_data():
    adapter = PartlowAdapter(device_address=1)
    expectation = adapter.compile(MasterOperation("read", parameter="]"), 1).steps[0].expectation
    data = b"25" + b"00000" * 5
    frame = adapter.decode(b"L1]" + data + b"A*", Direction.RX, BusMode.MASTER)
    assert frame.data == data.decode("ascii")
    assert adapter.match(expectation, frame) is MatchResult.SUCCESS


def test_partlow_slave_rejects_unmodifiable_and_invalid_controller_writes():
    adapter = PartlowAdapter(device_address=1)
    state = SlaveState(1, {"M": Decimal("12.3"), "Z": Decimal("0")})
    read_only = adapter.respond(
        adapter.decode(b"L1M#12301*", Direction.RX, BusMode.SLAVE), state
    )
    invalid_command = adapter.respond(
        adapter.decode(b"L1Z#12340*", Direction.RX, BusMode.SLAVE), state
    )
    assert read_only.data == b"L1M?????N*"
    assert invalid_command.data == b"L1Z?????N*"
