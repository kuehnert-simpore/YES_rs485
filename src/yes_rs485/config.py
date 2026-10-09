"""Versioned, atomic JSON configuration with schema-1 migration."""

from __future__ import annotations

import json
import os
import tempfile
import warnings
from pathlib import Path
from typing import Any, Mapping

from .models import (
    AppConfig,
    CANONICAL_DEVICE_IDS,
    DeviceConfig,
    DeviceId,
    MKS_DOCUMENTED_FRAMINGS,
    PassiveCaptureSettings,
    PortIdentity,
    ProtocolId,
    SerialFormat,
    UiPreferences,
    default_devices,
)


APP_NAME = "YES_RS485"


def default_config_path(app_name: str = APP_NAME) -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / app_name / "config.json"
    return Path.home() / "AppData" / "Roaming" / app_name / "config.json"


def default_config() -> AppConfig:
    return AppConfig()


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
    if not isinstance(value, Mapping):
        raise ValueError("serial profile must be an object")
    try:
        return SerialFormat(
            int(value["baudrate"]),
            int(value["bytesize"]),
            str(value["parity"]),
            float(value["stopbits"]),
            bool(value.get("xonxoff", False)),
            bool(value.get("rtscts", False)),
            bool(value.get("dsrdtr", False)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid serial profile") from exc


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
    if not isinstance(value, Mapping):
        raise ValueError("preferred_port must be an object or null")
    try:
        return PortIdentity(
            device=str(value["device"]),
            vid=None if value.get("vid") is None else int(value["vid"]),
            pid=None if value.get("pid") is None else int(value["pid"]),
            serial_number=value.get("serial_number"),
            description=value.get("description"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid preferred_port") from exc


def _ui_from_dict(value: Any) -> UiPreferences:
    if value is None:
        return UiPreferences()
    if not isinstance(value, Mapping):
        raise ValueError("ui must be an object")
    return UiPreferences(
        theme=str(value.get("theme", "darkly")),
        show_hex=bool(value.get("show_hex", True)),
        timeline_window_s=float(value.get("timeline_window_s", 30.0)),
    )


def _passive_from_dict(value: Any, *, legacy: bool = False) -> PassiveCaptureSettings:
    if value is None:
        return PassiveCaptureSettings()
    if not isinstance(value, Mapping):
        raise ValueError("passive_capture must be an object")
    if "serial" in value:
        serial = _serial_from_dict(value["serial"])
    elif legacy and "bus_baudrate" in value:
        # Schema 1 represented passive capture as a baudrate/strategy pair.
        # The old strategy carried no UART framing information, so migrate it
        # deterministically to the documented shared 7E1 framing.
        serial = SerialFormat(int(value["bus_baudrate"]), 7, "E", 1)
    else:
        serial = PassiveCaptureSettings().serial
    return PassiveCaptureSettings(
        serial=serial,
        response_pairing_window_s=float(value.get("response_pairing_window_s", 0.250)),
    )


def _device_to_dict(device: DeviceConfig) -> dict[str, Any]:
    return {
        "device_id": device.device_id.value,
        "protocol": device.protocol.value,
        "address": device.address,
        "serial": _serial_to_dict(device.serial),
        "fake_values": dict(device.fake_values),
    }


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    """Convert a validated config to schema-2 JSON-compatible values.

    No schema-1 selection/profile/address keys are emitted, even when a caller
    passes a subclass or object assembled from a legacy file.
    """

    if not isinstance(config, AppConfig):
        raise TypeError("config must be an AppConfig")
    # AppConfig validates at construction; reconstructing also catches a
    # maliciously altered frozen instance before it reaches disk.
    validated = AppConfig(
        schema_version=2,
        preferred_port=config.preferred_port,
        devices=tuple(config.devices),
        passive_capture=config.passive_capture,
        response_timeout_s=config.response_timeout_s,
        raw_capacity=config.raw_capacity,
        continuous_cycle_delay_s=config.continuous_cycle_delay_s,
        ui=config.ui,
    )
    return {
        "schema_version": 2,
        "preferred_port": _port_to_dict(validated.preferred_port),
        "devices": [_device_to_dict(device) for device in validated.devices],
        "passive_capture": {
            "serial": _serial_to_dict(validated.passive_capture.serial),
            "response_pairing_window_s": validated.passive_capture.response_pairing_window_s,
        },
        "response_timeout_s": validated.response_timeout_s,
        "raw_capacity": validated.raw_capacity,
        "continuous_cycle_delay_s": validated.continuous_cycle_delay_s,
        "ui": {
            "theme": validated.ui.theme,
            "show_hex": validated.ui.show_hex,
            "timeline_window_s": validated.ui.timeline_window_s,
        },
    }


def _schema2_from_dict(value: Mapping[str, Any]) -> AppConfig:
    raw_devices = value.get("devices")
    if not isinstance(raw_devices, (list, tuple)):
        raise ValueError("schema-2 devices must be an array")
    devices: list[DeviceConfig] = []
    for raw in raw_devices:
        if not isinstance(raw, Mapping):
            raise ValueError("device config must be an object")
        if "device_id" not in raw or "protocol" not in raw or "address" not in raw or "serial" not in raw:
            raise ValueError("device config requires device_id, protocol, address, and serial")
        fake_values = raw.get("fake_values", {})
        if not isinstance(fake_values, Mapping):
            raise ValueError("fake_values must be an object")
        devices.append(
            DeviceConfig(
                device_id=DeviceId(raw["device_id"]),
                protocol=ProtocolId(raw["protocol"]),
                address=int(raw["address"]),
                serial=_serial_from_dict(raw["serial"]),
                fake_values=dict(fake_values),
            )
        )
    ui = _ui_from_dict(value.get("ui", {}))
    return AppConfig(
        schema_version=2,
        preferred_port=_port_from_dict(value.get("preferred_port")),
        devices=tuple(devices),
        passive_capture=_passive_from_dict(value.get("passive_capture", {})),
        response_timeout_s=float(value.get("response_timeout_s", 1.0)),
        raw_capacity=int(value.get("raw_capacity", 10_000)),
        continuous_cycle_delay_s=float(value.get("continuous_cycle_delay_s", 0.250)),
        ui=ui,
    )


def _legacy_profiles(value: Mapping[str, Any]) -> dict[ProtocolId, Mapping[str, Any]]:
    raw_profiles = value.get("protocol_profiles", {})
    if raw_profiles is None:
        raw_profiles = {}
    if not isinstance(raw_profiles, Mapping):
        raise ValueError("protocol_profiles must be an object")
    result: dict[ProtocolId, Mapping[str, Any]] = {}
    for key, raw in raw_profiles.items():
        try:
            protocol = ProtocolId(key)
        except ValueError as exc:
            raise ValueError(f"unknown protocol profile: {key!r}") from exc
        if not isinstance(raw, Mapping):
            raise ValueError("protocol profile must be an object")
        result[protocol] = raw
    return result


def _legacy_slave_values(value: Mapping[str, Any], protocol: ProtocolId) -> Mapping[str, Any] | None:
    raw_slaves = value.get("slave_profiles", {})
    if raw_slaves is None:
        raw_slaves = {}
    if not isinstance(raw_slaves, Mapping):
        raise ValueError("slave_profiles must be an object")
    for _, raw in raw_slaves.items():
        if not isinstance(raw, Mapping):
            raise ValueError("slave profile must be an object")
        if "protocol" not in raw:
            raise ValueError("slave profile requires protocol")
        try:
            profile_protocol = ProtocolId(raw["protocol"])
        except ValueError as exc:
            raise ValueError("invalid slave profile protocol") from exc
        if profile_protocol is protocol:
            values = raw.get("values", {})
            if not isinstance(values, Mapping):
                raise ValueError("slave profile values must be an object")
            return dict(values)
    return None


def _legacy_address_map(value: Mapping[str, Any], partlow_profile: Mapping[str, Any] | None) -> dict[int, int]:
    raw_addresses = value.get("partlow_addresses", {})
    if raw_addresses is None:
        raw_addresses = {}
    if not isinstance(raw_addresses, Mapping):
        raise ValueError("partlow_addresses must be an object")
    result = {number: number + 1 for number in range(1, 12)}
    seen: set[int] = set()
    for raw_pid, raw_address in raw_addresses.items():
        try:
            pid = int(raw_pid)
            address = int(raw_address)
        except (TypeError, ValueError) as exc:
            raise ValueError("Partlow PID addresses must be integers") from exc
        if pid in seen:
            raise ValueError(f"duplicate Partlow PID number: {pid}")
        seen.add(pid)
        if not 1 <= pid <= 11:
            raise ValueError("Partlow PID number must be in the range 1..11")
        if not 1 <= address <= 99:
            raise ValueError("Partlow device address must be in the range 1..99")
        result[pid] = address
    # Very old files had only one Partlow ProtocolProfile. Retain that value
    # as PID 1 when no explicit address map exists.
    if not raw_addresses and partlow_profile is not None and "device_address" in partlow_profile:
        address = int(partlow_profile["device_address"])
        if not 1 <= address <= 99:
            raise ValueError("Partlow device address must be in the range 1..99")
        result[1] = address
    return result


def _schema1_migrate(value: Mapping[str, Any]) -> AppConfig:
    profiles = _legacy_profiles(value)
    defaults = default_devices()
    mks_profile = profiles.get(ProtocolId.MKS_275)
    partlow_profile = profiles.get(ProtocolId.PARTLOW_P1160)

    # Preserve an explicitly saved legacy MKS profile (including a deliberate
    # non-default framing).  With no MKS profile, use the native GP default
    # from ``default_devices`` (9600/8N1), not the Partlow 7E1 profile.
    gp_serial = _serial_from_dict(mks_profile["serial"]) if mks_profile and "serial" in mks_profile else defaults[0].serial
    gp_address = int(mks_profile.get("device_address", defaults[0].address)) if mks_profile else defaults[0].address
    gp_values = _legacy_slave_values(value, ProtocolId.MKS_275)
    if gp_values is None:
        gp_values = dict(defaults[0].fake_values)
    # DeviceConfig enforces MKS's 0..15 range and makes a bad old address a
    # visible invalid-config fallback instead of silently correcting it.
    devices: list[DeviceConfig] = [DeviceConfig(DeviceId.GP, ProtocolId.MKS_275, gp_address, gp_serial, dict(gp_values))]

    partlow_serial_raw = value.get("partlow_bus_serial", value.get("partlow_serial"))
    if partlow_serial_raw is None and partlow_profile and "serial" in partlow_profile:
        partlow_serial_raw = partlow_profile["serial"]
    partlow_serial = _serial_from_dict(partlow_serial_raw) if partlow_serial_raw is not None else defaults[1].serial
    addresses = _legacy_address_map(value, partlow_profile)
    pid_values = _legacy_slave_values(value, ProtocolId.PARTLOW_P1160)
    if pid_values is None:
        pid_values = dict(defaults[1].fake_values)
    for number, device_id in enumerate(CANONICAL_DEVICE_IDS[1:], start=1):
        devices.append(DeviceConfig(device_id, ProtocolId.PARTLOW_P1160, addresses[number], partlow_serial, dict(pid_values)))

    return AppConfig(
        schema_version=2,
        preferred_port=_port_from_dict(value.get("preferred_port")),
        devices=tuple(devices),
        passive_capture=_passive_from_dict(value.get("passive_capture", {}), legacy=True),
        response_timeout_s=float(value.get("response_timeout_s", 1.0)),
        raw_capacity=int(value.get("raw_capacity", 10_000)),
        continuous_cycle_delay_s=float(value.get("continuous_cycle_delay_s", 0.250)),
        ui=_ui_from_dict(value.get("ui", {})),
    )


def config_from_dict(value: Any) -> AppConfig:
    if not isinstance(value, Mapping):
        raise ValueError("configuration root must be an object")
    raw_version = value.get("schema_version", 1)
    try:
        schema_version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("schema_version must be an integer") from exc
    if schema_version == 1:
        return _schema1_migrate(value)
    if schema_version == 2:
        return _schema2_from_dict(value)
    raise ValueError(f"unsupported configuration schema version: {schema_version}")


def save_config(config: AppConfig, path: str | os.PathLike[str] | None = None) -> Path:
    """Atomically write a validated config and return its destination path."""

    payload = config_to_dict(config)
    validated = config_from_dict(payload)
    destination = Path(path) if path is not None else default_config_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent))
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
        return default_config()


class ConfigStore:
    """Small object-oriented wrapper convenient for the Tk composition root."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_config_path()

    def load(self) -> AppConfig:
        return load_config(self.path)

    def save(self, config: AppConfig) -> Path:
        return save_config(config, self.path)
