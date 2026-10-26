"""Headless checks for protocol-neutral UI frame evidence classification."""

from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from yes_rs485.models import BusMode, DeviceRuntimeState, DeviceId, default_devices
from yes_rs485.serial_worker import DeviceStateChanged
from yes_rs485.ui.app import YesRs485App, _coalesce_latest, _frame_device_id, _frame_role, _recover_frame_address
from yes_rs485.ui.views import _ascii


@pytest.mark.parametrize(
    ("kind", "direction", "expected"),
    (
        ("type2_request", "rx", "request"),
        ("type3_response", "rx", "response"),
        ("request", "rx", "request"),
        ("response", "rx", "response"),
        ("future_frame", "rx", "request"),
        ("future_frame", "tx", "response"),
    ),
)
def test_frame_role_prefers_decoded_kind_over_physical_direction(kind, direction, expected):
    assert _frame_role(SimpleNamespace(kind=kind), direction) == expected


def test_ascii_names_control_bytes_but_keeps_printable_payload():
    assert _ascii(b"A \r\n\x00") == "A<SP><CR><LF><NUL>"


def test_coalesce_latest_keeps_only_newest_item_per_device():
    events = [
        SimpleNamespace(device_id="pid_1", value=1),
        SimpleNamespace(device_id="pid_2", value=2),
        SimpleNamespace(device_id="pid_1", value=3),
    ]
    latest = _coalesce_latest(events, lambda item: item.device_id)
    assert {key: item.value for key, item in latest.items()} == {"pid_1": 3, "pid_2": 2}


def test_malformed_frame_routes_only_recoverable_partlow_address():
    devices = default_devices()
    frame = SimpleNamespace(
        protocol="partlow_p1160",
        raw_frame=b"L3Mnot-a-data-field*",
        decoded=None,
        error=SimpleNamespace(raw=b"L3Mnot-a-data-field*"),
        device_id=None,
    )
    assert _recover_frame_address("partlow_p1160", frame.error.raw, (d.address for d in devices if d.protocol.value == "partlow_p1160")) == 3
    assert _frame_device_id(frame, devices) == "pid_2"


def test_valid_addressed_frame_routes_only_that_device():
    devices = default_devices()
    frame = SimpleNamespace(
        protocol="partlow_p1160",
        raw_frame=b"L3M00121A*",
        decoded=SimpleNamespace(address=3, kind="type2_response"),
        error=None,
        device_id=None,
    )
    assert _frame_device_id(frame, devices) == "pid_2"


def test_unknown_malformed_frame_stays_unassigned_and_never_broadcasts():
    devices = default_devices()
    frame = SimpleNamespace(
        protocol="partlow_p1160",
        raw_frame=b"LXMnot-a-data-field*",
        decoded=None,
        error=SimpleNamespace(raw=b"LXMnot-a-data-field*"),
        device_id=None,
    )
    assert _frame_device_id(frame, devices) is None
    assert _frame_device_id(frame, devices, pending_device_id="pid_2", pending_protocol="mks_275") is None


def test_master_malformed_frame_can_fall_back_to_expected_target_only():
    devices = default_devices()
    frame = SimpleNamespace(
        protocol="partlow_p1160",
        raw_frame=b"LXMnot-a-data-field*",
        decoded=None,
        error=SimpleNamespace(raw=b"LXMnot-a-data-field*"),
        device_id=None,
    )
    assert _frame_device_id(frame, devices, pending_device_id="pid_2", pending_protocol="partlow_p1160") == "pid_2"


def test_mks_malformed_frame_routes_only_configured_gp_address():
    devices = default_devices()
    frame = SimpleNamespace(
        protocol="mks_275",
        raw_frame=b"*01_bad payload\r",
        decoded=None,
        error=SimpleNamespace(raw=b"*01_bad payload\r"),
        device_id=None,
    )
    assert _frame_device_id(frame, devices) == "gp"


def test_frame_evidence_does_not_overwrite_authoritative_transaction_result():
    app = object.__new__(YesRs485App)
    app._device_presentation = {
        "pid_1": {
            "result": "No response: protocol_error",
            "result_error": True,
            "last_error": "",
            "request_generation": 0,
            "pending_request_generation": None,
            "pending_request_deadline": None,
        }
    }
    app._record_evidence("pid_1", "malformed", error=True)
    assert app._device_presentation["pid_1"]["result"] == "No response: protocol_error"
    assert app._device_presentation["pid_1"]["result_error"] is True


def _presentation_state():
    return {
        "values": {},
        "online": False,
        "observed": False,
        "last_request": "Request —",
        "last_response": "Response —",
        "last_request_at": None,
        "last_response_at": None,
        "last_error": "",
        "request_generation": 0,
        "pending_request_generation": None,
        "pending_request_deadline": None,
        "result": None,
        "result_error": False,
    }


def test_mode_runtime_and_passive_timestamps_are_isolated():
    app = object.__new__(YesRs485App)
    app.__dict__["_operational_mode"] = "passive"
    passive = {"pid_1": _presentation_state()}
    master = {"pid_1": _presentation_state()}
    app.__dict__["_presentation_by_mode"] = {"passive": passive, "master": master, "slave": {}}
    app.__dict__["_device_presentation"] = passive
    request_at = datetime(2026, 10, 26, 15, 47, 32, 418000, tzinfo=timezone.utc)
    response_at = datetime(2026, 10, 26, 15, 47, 32, 426000, tzinfo=timezone.utc)

    app._record_evidence("pid_1", "type2_request", mode="passive", event_time=request_at, request=True)
    app._record_evidence("pid_1", "type2_response", mode="passive", event_time=response_at, response=True)
    app._record_evidence("pid_1", "type2_response", mode="master", event_time=response_at, response=True)

    assert passive["pid_1"]["last_response_at"] == response_at
    assert master["pid_1"]["last_response_at"] == response_at
    assert passive["pid_1"]["last_request_at"] == request_at
    assert passive["pid_1"]["last_response"] == "Response type2_response"


def test_device_state_notifications_route_to_their_source_mode():
    app = object.__new__(YesRs485App)
    passive = {"pid_1": _presentation_state()}
    master = {"pid_1": _presentation_state()}
    app.__dict__.update(
        _operational_mode="passive",
        _presentation_by_mode={"passive": passive, "master": master, "slave": {}},
        _device_presentation=passive,
        _runtime_by_mode={"passive": {}, "master": {}, "slave": {}},
        _session_running=True,
        _mode_for_tab=lambda: "passive",
        _sync_visible_devices=lambda *_args: None,
    )
    passive_state = DeviceRuntimeState(DeviceId.PID_1, 2, {"M": "100"})
    master_state = DeviceRuntimeState(DeviceId.PID_1, 2, {"M": "200"})

    app._consume_notifications(
        [
            DeviceStateChanged(DeviceId.PID_1, 1, passive_state, BusMode.PASSIVE),
            DeviceStateChanged(DeviceId.PID_1, 2, master_state, BusMode.MASTER),
        ]
    )

    assert app._runtime_by_mode["passive"]["pid_1"].values["M"] == "100"
    assert app._runtime_by_mode["master"]["pid_1"].values["M"] == "200"
    assert passive["pid_1"]["values"]["M"] == "100"
    assert master["pid_1"]["values"]["M"] == "200"


def test_passive_request_without_response_keeps_previous_response_timestamp():
    app = object.__new__(YesRs485App)
    app.__dict__["_operational_mode"] = "passive"
    passive = {"pid_8": _presentation_state()}
    app.__dict__["_presentation_by_mode"] = {"passive": passive}
    app.__dict__["_device_presentation"] = passive
    first_response = datetime(2026, 10, 26, 15, 47, 32, 426000, tzinfo=timezone.utc)
    later_request = datetime(2026, 10, 26, 15, 48, 0, 100000, tzinfo=timezone.utc)

    app._record_evidence("pid_8", "response", mode="passive", event_time=first_response, response=True)
    app._record_evidence("pid_8", "request", mode="passive", event_time=later_request, request=True)

    assert passive["pid_8"]["last_request_at"] == later_request
    assert passive["pid_8"]["last_response_at"] == first_response


def test_attributable_passive_malformed_response_updates_response_timestamp():
    app = object.__new__(YesRs485App)
    passive = {device.device_id.value: _presentation_state() for device in default_devices()}
    app.__dict__.update(
        _devices=default_devices(),
        _operational_mode="passive",
        _presentation_by_mode={"passive": passive, "master": {}, "slave": {}},
        _device_presentation=passive,
        _mode_for_tab=lambda: "passive",
        _sync_visible_devices=lambda *_args: None,
    )
    request_at = datetime(2026, 10, 26, 15, 47, 32, 418000, tzinfo=timezone.utc)
    malformed_at = datetime(2026, 10, 26, 15, 47, 32, 426000, tzinfo=timezone.utc)
    app._consume_frames([SimpleNamespace(
        protocol="partlow_p1160", raw_frame=b"L2M?*", decoded=SimpleNamespace(address=2, kind="type2_request"),
        error=None, device_id=None, direction="rx", mode="passive", wall_time_utc=request_at,
    )])
    app._consume_frames([SimpleNamespace(
        protocol="partlow_p1160", raw_frame=b"L2Mbad*", decoded=None,
        error=SimpleNamespace(raw=b"L2Mbad*"), device_id=None, direction="rx", mode="passive", wall_time_utc=malformed_at,
    )])

    assert passive["pid_1"]["last_request_at"] == request_at
    assert passive["pid_1"]["last_response_at"] == malformed_at
    assert passive["pid_1"]["last_error"].startswith("Error ")
