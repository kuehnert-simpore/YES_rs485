import json

import pytest

from yes_rs485.config import config_from_dict, config_to_dict, default_config, load_config, save_config
from yes_rs485.models import (
    AppConfig,
    BusMode,
    CANONICAL_DEVICE_IDS,
    DeviceConfig,
    DeviceId,
    DeviceRuntimeState,
    PassiveCaptureSettings,
    ProtocolId,
    SerialFormat,
    SessionSpec,
    default_devices,
)


def test_default_inventory_is_canonical_and_independent():
    devices = default_devices()
    assert tuple(device.device_id for device in devices) == CANONICAL_DEVICE_IDS
    assert devices[0].protocol is ProtocolId.MKS_275
    assert devices[0].address == 1
    assert devices[0].fake_values == {"pressure": "0.00E+00"}
    assert [device.address for device in devices[1:]] == list(range(2, 13))
    assert all(device.protocol is ProtocolId.PARTLOW_P1160 for device in devices[1:])
    assert devices[0].serial == SerialFormat(9600, 8, "N", 1)
    assert all(device.serial == SerialFormat(9600, 7, "E", 1) for device in devices[1:])
    assert len({id(device.fake_values) for device in devices}) == 12
    fresh = default_devices()
    assert len({id(device.fake_values) for device in fresh}) == 12


def test_inventory_validation_rejects_order_protocol_range_and_duplicate_address():
    devices = default_devices()
    with pytest.raises(ValueError, match="canonical order"):
        AppConfig(devices=devices[1:] + devices[:1])
    with pytest.raises(ValueError, match="fixed to protocol"):
        DeviceConfig(DeviceId.GP, ProtocolId.PARTLOW_P1160, 1, devices[0].serial)
    duplicate = list(devices)
    duplicate[-1] = DeviceConfig(DeviceId.PID_11, ProtocolId.PARTLOW_P1160, 2, devices[-1].serial)
    with pytest.raises(ValueError, match="duplicate"):
        AppConfig(devices=tuple(duplicate))
    with pytest.raises(ValueError, match="range 1..99"):
        DeviceConfig(DeviceId.PID_1, ProtocolId.PARTLOW_P1160, 0, devices[1].serial)


def test_config_round_trip_contains_only_schema_two_fields(tmp_path):
    path = tmp_path / "nested" / "config.json"
    devices = list(default_devices())
    devices[0] = DeviceConfig(
        DeviceId.GP, ProtocolId.MKS_275, 3, SerialFormat(4800, 8, "N", 1), {"pressure": "12.3"}
    )
    original = AppConfig(devices=tuple(devices), continuous_cycle_delay_s=0.5)
    assert save_config(original, path) == path
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {
        "schema_version", "preferred_port", "devices", "passive_capture",
        "response_timeout_s", "raw_capacity", "continuous_cycle_delay_s", "ui",
    }
    assert not {"selected_protocol", "protocol_profiles", "partlow_addresses", "slave_profiles"} & set(payload)
    loaded = load_config(path)
    assert loaded == original
    assert not list(path.parent.glob("*.tmp"))


def test_schema_one_migration_ignores_selection_and_arm_and_copies_values():
    loaded = config_from_dict({
        "schema_version": 1,
        "selected_protocol": "mks_275",
        "safety_arm": "plc_disconnected",
        "protocol_profiles": {
            "mks_275": {
                "serial": {"baudrate": 4800, "bytesize": 8, "parity": "N", "stopbits": 1},
                "device_address": 4,
            },
            "partlow_p1160": {
                "serial": {"baudrate": 9600, "bytesize": 7, "parity": "E", "stopbits": 1},
            },
        },
        "partlow_addresses": {str(pid): pid + 20 for pid in range(1, 12)},
        "slave_profiles": {
            "mks": {"protocol": "mks_275", "values": {"pressure": "1.2"}},
            "partlow": {"protocol": "partlow_p1160", "values": {"M": 2, "S": 3}},
        },
    })
    assert loaded.schema_version == 2
    assert loaded.devices[0].address == 4
    assert loaded.devices[0].serial == SerialFormat(4800, 8, "N", 1)
    assert loaded.devices[0].fake_values == {"pressure": "1.2"}
    assert [device.address for device in loaded.devices[1:]] == list(range(21, 32))
    assert all(device.fake_values == {"M": 2, "S": 3} for device in loaded.devices[1:])
    assert len({id(device.fake_values) for device in loaded.devices}) == 12
    assert not hasattr(loaded, "selected_protocol")


def test_schema_one_migration_uses_mks_default_when_profile_is_absent():
    loaded = config_from_dict({"schema_version": 1, "protocol_profiles": {}})
    assert loaded.devices[0].serial == SerialFormat(9600, 8, "N", 1)
    assert all(device.serial == SerialFormat(9600, 7, "E", 1) for device in loaded.devices[1:])


def test_malformed_json_preserves_file_and_loads_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{invalid", encoding="utf-8")
    with pytest.warns(RuntimeWarning):
        loaded = load_config(path)
    assert loaded == default_config()
    assert path.read_text(encoding="utf-8") == "{invalid"


def test_session_spec_and_runtime_state_are_bus_device_aware():
    devices = default_devices()
    spec = SessionSpec(BusMode.MASTER, "COM1", devices, devices[0].serial)
    assert spec.devices == devices
    assert spec.listen_serial == devices[0].serial
    listen = SerialFormat(4800, 7, "E", 1)
    slave_spec = SessionSpec(BusMode.SLAVE, "COM1", devices, listen)
    assert slave_spec.listen_serial == listen
    state = DeviceRuntimeState(DeviceId.PID_1, 2, {"M": 1})
    with pytest.raises(TypeError):
        state.values["M"] = 2  # type: ignore[index]
