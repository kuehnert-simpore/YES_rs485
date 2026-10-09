import json

import pytest

from yes_rs485.config import config_from_dict, default_config, load_config, save_config
from yes_rs485.models import PassiveCaptureSettings, ProtocolId, SerialFormat


def test_config_defaults_use_native_serial_formats_and_no_arm():
    config = default_config()
    assert config.partlow_bus_serial == SerialFormat(9600, 7, "E", 1)
    assert config.protocol_profiles[ProtocolId.PARTLOW_P1160].serial == SerialFormat(9600, 7, "E", 1)
    assert dict(config.partlow_addresses) == {pid: pid + 1 for pid in range(1, 12)}
    assert config.protocol_profiles[ProtocolId.MKS_275].serial.parity == "N"
    assert config.protocol_profiles[ProtocolId.MKS_275].serial.baudrate == 9600
    assert config.passive_capture == PassiveCaptureSettings(9600, "mixed_raw")
    assert config.ui.theme == "darkly"
    assert not hasattr(config, "safety_arm")


def test_config_round_trip_and_atomic_write(tmp_path):
    path = tmp_path / "nested" / "config.json"
    original = default_config()
    assert save_config(original, path) == path
    loaded = load_config(path)
    assert loaded.selected_protocol is ProtocolId.PARTLOW_P1160
    assert loaded.protocol_profiles[ProtocolId.MKS_275].serial.baudrate == 9600
    assert loaded.partlow_addresses[11] == 12
    assert loaded.passive_capture.bus_baudrate == 9600
    assert not list(path.parent.glob("*.tmp"))


def test_invalid_json_preserves_file_and_loads_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{invalid", encoding="utf-8")
    with pytest.warns(RuntimeWarning):
        loaded = load_config(path)
    assert loaded == default_config()
    assert path.read_text(encoding="utf-8") == "{invalid"


def test_legacy_single_partlow_profile_is_retained_as_pid1():
    loaded = config_from_dict({
        "protocol_profiles": {
            "partlow_p1160": {
                "serial": {
                    "baudrate": 4800,
                    "bytesize": 7,
                    "parity": "E",
                    "stopbits": 1,
                },
                "device_address": 7,
            },
        },
    })
    assert loaded.partlow_bus_serial == SerialFormat(4800, 7, "E", 1)
    assert loaded.partlow_addresses[1] == 7
    assert loaded.partlow_addresses[11] == 12
