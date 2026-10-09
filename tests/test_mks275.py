from yes_rs485.models import BusMode, Direction, MasterOperation
from yes_rs485.protocols.base import MatchResult, SlaveState
from yes_rs485.protocols.mks275 import MKS275Adapter, MKS275Framer, build_request


def test_mks_defaults_spacing_and_hex_address():
    adapter = MKS275Adapter(device_address=0x0F)
    plan = adapter.compile(MasterOperation("read"), 1)
    assert plan.steps[0].tx == b"#0FRD \r"
    assert adapter.default_serial_settings().parity == "N"
    assert build_request(1, "RH+") == b"#01RH+\r"
    assert build_request(1, "RSA") == b"#01RSA \r"


def test_mks_split_frames_and_response_matching():
    adapter = MKS275Adapter(1)
    framer = MKS275Framer()
    assert framer.feed(b"junk#01RD ") == []
    assert framer.feed(b"\r*01_9.34E-02\r") == [b"#01RD \r", b"*01_9.34E-02\r"]
    plan = adapter.compile(MasterOperation("read"), 1)
    actual = adapter.decode(b"*01_9.34E-02\r", Direction.RX, BusMode.MASTER)
    assert adapter.match(plan.steps[0].expectation, actual) is MatchResult.SUCCESS
    wrong = adapter.decode(b"*02_9.34E-02\r", Direction.RX, BusMode.MASTER)
    assert adapter.match(plan.steps[0].expectation, wrong) is MatchResult.UNRELATED
    error = adapter.decode(b"?01_DEF_SNSR\r", Direction.RX, BusMode.MASTER)
    assert adapter.match(plan.steps[0].expectation, error) is MatchResult.NEGATIVE


def test_mks_negative_setpoint_reply_address_is_not_lost():
    adapter = MKS275Adapter()
    actual = adapter.decode(b"*01-9.80E-02\r", Direction.RX, BusMode.MASTER)
    assert actual.address == 1
    assert actual.payload == "-9.80E-02"


def test_mks_rst_is_no_response_with_recovery():
    plan = MKS275Adapter().compile(MasterOperation("reset"), 1)
    assert plan.steps[0].tx == b"#01RST\r"
    assert plan.steps[0].expectation is None
    assert plan.steps[0].recovery_delay_s == 2.0


def test_mks_addressless_rsa_reply_is_allowed():
    adapter = MKS275Adapter()
    plan = adapter.compile(MasterOperation("rsa"), 1)
    actual = adapter.decode(b"*AVG: 04\r", Direction.RX, BusMode.MASTER)
    assert adapter.match(plan.steps[0].expectation, actual) is MatchResult.SUCCESS


def test_mks_rsa_slave_reply_is_addressless():
    adapter = MKS275Adapter()
    request = adapter.decode(b"#01RSA \r", Direction.RX, BusMode.SLAVE)
    decision = adapter.respond(request, SlaveState(1, {"RSA": "04"}))
    assert decision.data == b"*AVG: 04\r"


def test_mks_pcs_irregular_reply_keeps_hex_address_and_status_marker():
    adapter = MKS275Adapter()
    plan = adapter.compile(MasterOperation("PCS"), 1)
    actual = adapter.decode(b"*0111PCS\r", Direction.RX, BusMode.MASTER)
    assert actual.address == 1
    assert actual.payload == "11PCS"
    assert adapter.match(plan.steps[0].expectation, actual) is MatchResult.SUCCESS


def test_mks_pcs_slave_reply_places_status_before_marker():
    adapter = MKS275Adapter()
    request = adapter.decode(b"#01PCS\r", Direction.RX, BusMode.SLAVE)
    decision = adapter.respond(request, SlaveState(1, {"PCS": "11"}))
    assert decision.data == b"*0111PCS\r"


def test_mks_slave_setpoint_on_and_off_reply_shapes():
    adapter = MKS275Adapter()
    values = {"RH+": "9.80E-02", "RH-": "9.80E-02"}
    on = adapter.respond(adapter.decode(b"#01RH+\r", Direction.RX, BusMode.SLAVE), SlaveState(1, values))
    off = adapter.respond(adapter.decode(b"#01RH-\r", Direction.RX, BusMode.SLAVE), SlaveState(1, values))
    assert on.data == b"*01_9.80E-02\r"
    assert off.data == b"*01-9.80E-02\r"


def test_mks_addressless_voltage_error_is_negative():
    adapter = MKS275Adapter()
    plan = adapter.compile(MasterOperation("read"), 1)
    actual = adapter.decode(b"*HI VOLTAGE\r", Direction.RX, BusMode.MASTER)
    assert adapter.match(plan.steps[0].expectation, actual) is MatchResult.NEGATIVE


def test_mks_addressed_voltage_error_is_negative():
    adapter = MKS275Adapter()
    plan = adapter.compile(MasterOperation("read"), 1)
    actual = adapter.decode(b"*01_HI_ATM_V\r", Direction.RX, BusMode.MASTER)
    assert adapter.match(plan.steps[0].expectation, actual) is MatchResult.NEGATIVE


def test_mks_slave_reset_has_no_reply():
    adapter = MKS275Adapter()
    request = adapter.decode(b"#01RST\r", Direction.RX, BusMode.SLAVE)
    decision = adapter.respond(request, SlaveState(1))
    assert decision.__class__.__name__ == "NoReply"
    assert decision.recovery_s == 2.0
