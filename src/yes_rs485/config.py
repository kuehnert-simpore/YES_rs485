"""Versioned, atomic JSON configuration with safe defaults.

Only user preferences and protocol/slave profiles are persisted.  Safety arms,
open-port state, and destructive-write confirmations intentionally have no
field in :class:`AppConfig`, so they cannot be restored accidentally.
"""

from __future__ import annotations

import json
import os
import tempfile
import warnings
from pathlib import Path
from typing import Any

from .models import (
    AppConfig,
    PassiveCaptureSettings,
    PortIdentity,
    ProtocolId,
    ProtocolProfile,
    SerialFormat,
    SlaveProfile,
    UiPreferences,
)


APP_NAME = "YES_RS485"


def default_config_path(app_name: str = APP_NAME) -> Path:
    """Return the normal per-user config location without extra dependencies."""

    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / app_name / "config.json"
    # This is the Windows fallback used when APPDATA is unavailable in a test
    # harness or a minimally configured process.
    return Path.home() / "AppData" / "Roaming" / app_name / "config.json"


def default_config() -> AppConfig:
    partlow_serial = SerialFormat(9600, 7, "E", 1)
    mks_serial = SerialFormat(9600, 8, "N", 1)
    return AppConfig(
        protocol_profiles={
            ProtocolId.PARTLOW_P1160: ProtocolProfile(
                partlow_serial, 2
            ),
            ProtocolId.MKS_275: ProtocolProfile(
                mks_serial, 1
            ),
        },
        slave_profiles={
            "partlow-default": SlaveProfile(
                name="Partlow default", protocol=ProtocolId.PARTLOW_P1160,
                address=1, values={"M": 0, "S": 0}
            ),
            "mks-default": SlaveProfile(
                name="MKS default", protocol=ProtocolId.MKS_275,
                address=1, values={"pressure": "0.00E+00"}
            ),
        },
        partlow_bus_serial=partlow_serial,
        partlow_addresses={pid: pid + 1 for pid in range(1, 12)},
        passive_capture=PassiveCaptureSettings(
            bus_baudrate=9600,
            strategy="mixed_raw",
        ),
    )


def _serial_to_dict(value: SerialFormat) -> dict[str, Any]:
    return {
        "baudrate": value.baudrate,
        "bytesize": value.bytesize,
        "parity": value.parity,
        "stopbits": value.stopbits,
        "xonxoff": value.xonxoff,
        "rtscts": value.rtscts,
        "dsrdtr": value.dsrdtr,
    }


def _serial_from_dict(value: Any) -> SerialFormat:
    if not isinstance(value, dict):
        raise ValueError("serial profile must be an object")
    return SerialFormat(
        int(value["baudrate"]), int(value["bytesize"]), str(value["parity"]),
        float(value["stopbits"]), bool(value.get("xonxoff", False)),
        bool(value.get("rtscts", False)), bool(value.get("dsrdtr", False)),
    )


def _port_to_dict(value: PortIdentity | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "device": value.device,
        "vid": value.vid,
        "pid": value.pid,
        "serial_number": value.serial_number,
        "description": value.description,
    }


def _port_from_dict(value: Any) -> PortIdentity | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("preferred_port must be an object or null")
    return PortIdentity(
        device=str(value["device"]),
        vid=None if value.get("vid") is None else int(value["vid"]),
        pid=None if value.get("pid") is None else int(value["pid"]),
        serial_number=value.get("serial_number"),
        description=value.get("description"),
    )


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    return {
        "schema_version": config.schema_version,
        "selected_protocol": config.selected_protocol.value,
        "preferred_port": _port_to_dict(config.preferred_port),
        "protocol_profiles": {
            protocol.value: {
                "serial": _serial_to_dict(profile.serial),
                "device_address": profile.device_address,
            }
            for protocol, profile in config.protocol_profiles.items()
        },
        "partlow_bus_serial": _serial_to_dict(config.partlow_serial),
        "partlow_addresses": {
            str(pid): address for pid, address in config.partlow_addresses.items()
        },
        "passive_capture": {
            "bus_baudrate": config.passive_capture.bus_baudrate,
            "strategy": config.passive_capture.strategy,
            "response_pairing_window_s": config.passive_capture.response_pairing_window_s,
        },
        "slave_profiles": {
            name: {
                "name": profile.name,
                "protocol": profile.protocol.value,
                "address": profile.address,
                "values": dict(profile.values),
            }
            for name, profile in config.slave_profiles.items()
        },
        "response_timeout_s": config.response_timeout_s,
        "raw_capacity": config.raw_capacity,
        "ui": {
            "theme": config.ui.theme,
            "show_hex": config.ui.show_hex,
            "timeline_window_s": config.ui.timeline_window_s,
        },
    }


def config_from_dict(value: Any) -> AppConfig:
    if not isinstance(value, dict):
        raise ValueError("configuration root must be an object")
    profiles: dict[ProtocolId, ProtocolProfile] = {}
    for key, raw in dict(value.get("protocol_profiles", {})).items():
        protocol = ProtocolId(key)
        if not isinstance(raw, dict):
            raise ValueError("protocol profile must be an object")
        profiles[protocol] = ProtocolProfile(
            _serial_from_dict(raw["serial"]), int(raw["device_address"])
        )
    slaves: dict[str, SlaveProfile] = {}
    for key, raw in dict(value.get("slave_profiles", {})).items():
        if not isinstance(raw, dict):
            raise ValueError("slave profile must be an object")
        slaves[str(key)] = SlaveProfile(
            name=str(raw.get("name", key)),
            protocol=ProtocolId(raw["protocol"]),
            address=int(raw["address"]),
            values=dict(raw.get("values", {})),
        )
    ui_raw = value.get("ui", {})
    if not isinstance(ui_raw, dict):
        raise ValueError("ui must be an object")
    raw_partlow_serial = value.get("partlow_bus_serial", value.get("partlow_serial"))
    partlow_bus_serial = (
        _serial_from_dict(raw_partlow_serial)
        if raw_partlow_serial is not None
        else None
    )
    raw_addresses = value.get("partlow_addresses", {})
    if not isinstance(raw_addresses, dict):
        raise ValueError("partlow_addresses must be an object")
    passive_raw = value.get("passive_capture", {})
    if not isinstance(passive_raw, dict):
        raise ValueError("passive_capture must be an object")
    passive_capture = PassiveCaptureSettings(
        bus_baudrate=int(
            passive_raw.get("bus_baudrate", passive_raw.get("baudrate", 9600))
        ),
        strategy=str(passive_raw.get("strategy", passive_raw.get("capture_strategy", "mixed_raw"))),
        response_pairing_window_s=float(
            passive_raw.get("response_pairing_window_s", 0.250)
        ),
    )
    result = AppConfig(
        schema_version=int(value.get("schema_version", 1)),
        selected_protocol=ProtocolId(value.get("selected_protocol", ProtocolId.PARTLOW_P1160.value)),
        preferred_port=_port_from_dict(value.get("preferred_port")),
        protocol_profiles=profiles,
        slave_profiles=slaves,
        response_timeout_s=float(value.get("response_timeout_s", 1.0)),
        raw_capacity=int(value.get("raw_capacity", 10_000)),
        ui=UiPreferences(
            theme=str(ui_raw.get("theme", "darkly")),
            show_hex=bool(ui_raw.get("show_hex", True)),
            timeline_window_s=float(ui_raw.get("timeline_window_s", 30.0)),
        ),
        partlow_bus_serial=partlow_bus_serial,
        partlow_addresses={int(pid): int(address) for pid, address in raw_addresses.items()},
        passive_capture=passive_capture,
    )
    # Missing profiles in an older/minimal file get safe documented defaults;
    # explicit values still win.
    defaults = default_config()
    # Fill missing protocol views individually, while retaining explicit
    # legacy/custom values and the single shared Partlow bus profile.
    merged_profiles = dict(result.protocol_profiles)
    for protocol, profile in defaults.protocol_profiles.items():
        if protocol not in merged_profiles:
            merged_profiles[protocol] = profile
    if ProtocolId.PARTLOW_P1160 in merged_profiles:
        merged_profiles[ProtocolId.PARTLOW_P1160] = ProtocolProfile(
            result.partlow_serial, result.partlow_addresses[1]
        )
    object.__setattr__(result, "protocol_profiles", result.protocol_profiles.__class__(merged_profiles))
    if not result.slave_profiles:
        object.__setattr__(result, "slave_profiles", defaults.slave_profiles)
    return result


def save_config(config: AppConfig, path: str | os.PathLike[str] | None = None) -> Path:
    """Atomically write a validated config and return its destination path."""

    # Reconstructing validates externally-created dataclass subclasses and
    # ensures the object has the current schema before serialization.
    validated = config_from_dict(config_to_dict(config))
    destination = Path(path) if path is not None else default_config_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(config_to_dict(validated), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return destination


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load and validate JSON, falling back to defaults with a warning."""

    source = Path(path) if path is not None else default_config_path()
    if not source.exists():
        return default_config()
    try:
        with source.open("r", encoding="utf-8") as handle:
            return config_from_dict(json.load(handle))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        warnings.warn(
            f"Could not load YES_RS485 configuration from {source}; using defaults: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
        # Leave the invalid file untouched so the user can inspect/recover it.
        return default_config()


class ConfigStore:
    """Small object-oriented wrapper convenient for the Tk composition root."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_config_path()

    def load(self) -> AppConfig:
        return load_config(self.path)

    def save(self, config: AppConfig) -> Path:
        return save_config(config, self.path)
