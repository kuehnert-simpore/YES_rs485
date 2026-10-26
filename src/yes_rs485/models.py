"""Immutable values shared by the UI, worker, and protocol adapters.

The public configuration model describes one physical bus containing the
canonical GP plus eleven PID devices. Protocol adapters consume the small
protocol-neutral values at the bottom of this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from uuid import UUID


class BusMode(str, Enum):
    PASSIVE = "passive"
    MASTER = "master"
    SLAVE = "slave"


class ProtocolId(str, Enum):
    PARTLOW_P1160 = "partlow_p1160"
    MKS_275 = "mks_275"


class DeviceId(str, Enum):
    GP = "gp"
    PID_1 = "pid_1"
    PID_2 = "pid_2"
    PID_3 = "pid_3"
    PID_4 = "pid_4"
    PID_5 = "pid_5"
    PID_6 = "pid_6"
    PID_7 = "pid_7"
    PID_8 = "pid_8"
    PID_9 = "pid_9"
    PID_10 = "pid_10"
    PID_11 = "pid_11"


CANONICAL_DEVICE_IDS: tuple[DeviceId, ...] = tuple(DeviceId)

_DEVICE_PROTOCOL: Mapping[DeviceId, ProtocolId] = MappingProxyType(
    {
        DeviceId.GP: ProtocolId.MKS_275,
        **{device_id: ProtocolId.PARTLOW_P1160 for device_id in CANONICAL_DEVICE_IDS[1:]},
    }
)

COMMON_BAUDRATES: tuple[int, ...] = (
    1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200,
)
PARTLOW_DOCUMENTED_BAUDRATES: tuple[int, ...] = (1200, 2400, 4800, 9600, 19200)
MKS_DOCUMENTED_BAUDRATES: tuple[int, ...] = (
    300, 600, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200,
)
PARTLOW_DOCUMENTED_FRAMINGS: frozenset[str] = frozenset({"7E1"})
MKS_DOCUMENTED_FRAMINGS: frozenset[str] = frozenset({"8N1", "7E1", "7O1"})


class Direction(str, Enum):
    RX = "rx"
    TX = "tx"


class LifecycleState(str, Enum):
    NEW = "new"
    IDLE = "idle"
    OPENING = "opening"
    RUNNING = "running"
    CLOSING = "closing"
    FAULTED = "faulted"
    STOPPED = "stopped"


def _as_mapping(value: Mapping[Any, Any] | None) -> MappingProxyType:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("value must be a mapping")
    return MappingProxyType(dict(value))


def protocol_for_device(device_id: DeviceId) -> ProtocolId:
    """Return the fixed protocol associated with a canonical device ID."""

    try:
        return _DEVICE_PROTOCOL[DeviceId(device_id)]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unknown device ID: {device_id!r}") from exc


def _validate_device_address(device_id: DeviceId, address: int) -> None:
    if isinstance(address, bool) or not isinstance(address, int):
        raise TypeError("device address must be an integer")
    protocol = protocol_for_device(device_id)
    if protocol is ProtocolId.PARTLOW_P1160:
        if not 1 <= address <= 99:
            raise ValueError("Partlow device address must be in the range 1..99")
    elif not 0 <= address <= 15:
        raise ValueError("MKS device address must be in the range 0..15")


@dataclass(frozen=True)
class SerialFormat:
    """A serial framing format without a selected port."""

    baudrate: int
    bytesize: int
    parity: str
    stopbits: float
    xonxoff: bool = False
    rtscts: bool = False
    dsrdtr: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.baudrate, bool) or not isinstance(self.baudrate, int) or self.baudrate <= 0:
            raise ValueError("baudrate must be a positive integer")
        if self.bytesize not in (5, 6, 7, 8):
            raise ValueError("bytesize must be 5, 6, 7, or 8")
        parity = str(self.parity).upper()
        if parity not in {"N", "E", "O", "M", "S"}:
            raise ValueError("parity must be N, E, O, M, or S")
        if self.stopbits not in (1, 1.5, 2):
            raise ValueError("stopbits must be 1, 1.5, or 2")
        object.__setattr__(self, "parity", parity)

    @property
    def framing(self) -> str:
        stopbits = int(self.stopbits) if float(self.stopbits).is_integer() else self.stopbits
        return f"{self.bytesize}{self.parity}{stopbits}"

    @property
    def format_label(self) -> str:
        return self.framing

    def is_valid_custom(self) -> bool:
        return (
            self.bytesize in (7, 8)
            and self.parity in {"N", "E", "O"}
            and self.stopbits in (1, 2)
        )

    def is_documented_for(self, protocol: ProtocolId) -> bool:
        protocol = ProtocolId(protocol)
        if protocol is ProtocolId.PARTLOW_P1160:
            return self.framing in PARTLOW_DOCUMENTED_FRAMINGS
        return self.framing in MKS_DOCUMENTED_FRAMINGS if protocol is ProtocolId.MKS_275 else False


def is_documented_serial(protocol: ProtocolId, serial: SerialFormat) -> bool:
    protocol = ProtocolId(protocol)
    if not isinstance(serial, SerialFormat):
        raise TypeError("serial must be a SerialFormat")
    if protocol is ProtocolId.PARTLOW_P1160:
        return serial.baudrate in PARTLOW_DOCUMENTED_BAUDRATES and serial.framing in PARTLOW_DOCUMENTED_FRAMINGS
    if protocol is ProtocolId.MKS_275:
        return serial.baudrate in MKS_DOCUMENTED_BAUDRATES and serial.framing in MKS_DOCUMENTED_FRAMINGS
    return False


def documented_baudrates(protocol: ProtocolId) -> tuple[int, ...]:
    protocol = ProtocolId(protocol)
    return PARTLOW_DOCUMENTED_BAUDRATES if protocol is ProtocolId.PARTLOW_P1160 else (
        MKS_DOCUMENTED_BAUDRATES if protocol is ProtocolId.MKS_275 else ()
    )


def documented_framings(protocol: ProtocolId) -> tuple[str, ...]:
    protocol = ProtocolId(protocol)
    return tuple(sorted(PARTLOW_DOCUMENTED_FRAMINGS)) if protocol is ProtocolId.PARTLOW_P1160 else (
        tuple(sorted(MKS_DOCUMENTED_FRAMINGS)) if protocol is ProtocolId.MKS_275 else ()
    )


def serial_format_warning(protocol: ProtocolId, serial: SerialFormat) -> str | None:
    return None if is_documented_serial(protocol, serial) else "Custom framing — not a documented device configuration."


@dataclass(frozen=True)
class SerialSettings:
    """Port plus native serial framing settings (legacy adapter/UI value)."""

    port: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: float
    xonxoff: bool = False
    rtscts: bool = False
    dsrdtr: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.port, str) or not self.port.strip():
            raise ValueError("port must be a non-empty string")
        fmt = SerialFormat(self.baudrate, self.bytesize, self.parity, self.stopbits, self.xonxoff, self.rtscts, self.dsrdtr)
        for name in ("baudrate", "bytesize", "parity", "stopbits"):
            object.__setattr__(self, name, getattr(fmt, name))

    @classmethod
    def from_format(cls, port: str, fmt: SerialFormat) -> "SerialSettings":
        return cls(port, fmt.baudrate, fmt.bytesize, fmt.parity, fmt.stopbits, fmt.xonxoff, fmt.rtscts, fmt.dsrdtr)

    def as_format(self) -> SerialFormat:
        return SerialFormat(self.baudrate, self.bytesize, self.parity, self.stopbits, self.xonxoff, self.rtscts, self.dsrdtr)


@dataclass(frozen=True)
class PortIdentity:
    device: str
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.device, str) or not self.device.strip():
            raise ValueError("device must be non-empty")
        for name in ("vid", "pid"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class DeviceConfig:
    device_id: DeviceId
    protocol: ProtocolId
    address: int
    serial: SerialFormat
    fake_values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        device_id = DeviceId(self.device_id)
        protocol = ProtocolId(self.protocol)
        expected = protocol_for_device(device_id)
        if protocol is not expected:
            raise ValueError(f"{device_id.value} is fixed to protocol {expected.value}")
        if not isinstance(self.serial, SerialFormat):
            raise TypeError("serial must be a SerialFormat")
        _validate_device_address(device_id, self.address)
        object.__setattr__(self, "device_id", device_id)
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "fake_values", _as_mapping(self.fake_values))


def default_devices() -> tuple[DeviceConfig, ...]:
    """Return the exact canonical GP/PID inventory with independent values."""

    # The two protocol families have different native machine defaults.  Keep
    # those profiles independent: the master can reconfigure this one UART at
    # each transaction boundary, while passive/slave sessions must choose one
    # explicit listen format for capture.
    gp_serial = SerialFormat(9600, 8, "N", 1)
    partlow_serial = SerialFormat(9600, 7, "E", 1)
    devices = [DeviceConfig(DeviceId.GP, ProtocolId.MKS_275, 1, gp_serial, {"pressure": "0.00E+00"})]
    for number, device_id in enumerate(CANONICAL_DEVICE_IDS[1:], start=1):
        devices.append(DeviceConfig(device_id, ProtocolId.PARTLOW_P1160, number + 1, partlow_serial, {"M": 0, "S": 0}))
    return tuple(devices)


def validate_device_inventory(devices: Sequence[DeviceConfig], *, name: str = "devices") -> tuple[DeviceConfig, ...]:
    """Validate and freeze an ordered, complete canonical device inventory."""

    result = tuple(devices)
    if len(result) != len(CANONICAL_DEVICE_IDS):
        raise ValueError(f"{name} must contain exactly {len(CANONICAL_DEVICE_IDS)} devices")
    keys: set[tuple[ProtocolId, int]] = set()
    for index, (expected_id, device) in enumerate(zip(CANONICAL_DEVICE_IDS, result)):
        if not isinstance(device, DeviceConfig):
            raise TypeError(f"{name}[{index}] must be a DeviceConfig")
        if device.device_id is not expected_id:
            raise ValueError(f"{name} must be in canonical order ({expected_id.value} expected at index {index})")
        key = (device.protocol, device.address)
        if key in keys:
            raise ValueError(f"duplicate device protocol/address: {device.protocol.value}/{device.address}")
        keys.add(key)
    return result


def device_by_id(devices: Sequence[DeviceConfig], device_id: DeviceId) -> DeviceConfig:
    target = DeviceId(device_id)
    for device in devices:
        if device.device_id is target:
            return device
    raise KeyError(target)


@dataclass(frozen=True)
class UiPreferences:
    theme: str = "darkly"
    show_hex: bool = True
    timeline_window_s: float = 30.0

    def __post_init__(self) -> None:
        if self.theme not in {"system", "light", "dark", "darkly", "cyborg", "flatly"}:
            raise ValueError("theme must be system, light, dark, darkly, cyborg, or flatly")
        if self.timeline_window_s <= 0:
            raise ValueError("timeline_window_s must be positive")


@dataclass(frozen=True)
class PassiveCaptureSettings:
    """The one UART format used to listen in passive/slave capture roles."""

    serial: SerialFormat = field(default_factory=lambda: SerialFormat(9600, 7, "E", 1))
    response_pairing_window_s: float = 0.250

    def __post_init__(self) -> None:
        if not isinstance(self.serial, SerialFormat):
            raise TypeError("passive capture serial must be a SerialFormat")
        if self.response_pairing_window_s <= 0:
            raise ValueError("response_pairing_window_s must be positive")

    @property
    def bus_baudrate(self) -> int:
        return self.serial.baudrate

    @property
    def baudrate(self) -> int:
        return self.serial.baudrate

    @property
    def strategy(self) -> str:
        return "mixed_raw"

    @property
    def capture_strategy(self) -> str:
        return self.strategy


@dataclass(frozen=True)
class AppConfig:
    schema_version: int = 2
    preferred_port: PortIdentity | None = None
    devices: tuple[DeviceConfig, ...] = field(default_factory=default_devices)
    passive_capture: PassiveCaptureSettings = field(default_factory=PassiveCaptureSettings)
    response_timeout_s: float = 1.0
    raw_capacity: int = 10_000
    continuous_cycle_delay_s: float = 0.250
    ui: UiPreferences = field(default_factory=UiPreferences)

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError("unsupported configuration schema version")
        if self.preferred_port is not None and not isinstance(self.preferred_port, PortIdentity):
            raise TypeError("preferred_port must be a PortIdentity or None")
        if self.response_timeout_s <= 0:
            raise ValueError("response_timeout_s must be positive")
        if isinstance(self.raw_capacity, bool) or not isinstance(self.raw_capacity, int) or self.raw_capacity < 1:
            raise ValueError("raw_capacity must be positive")
        if self.continuous_cycle_delay_s < 0:
            raise ValueError("continuous_cycle_delay_s cannot be negative")
        if not isinstance(self.passive_capture, PassiveCaptureSettings):
            raise TypeError("passive_capture must be PassiveCaptureSettings")
        if not isinstance(self.ui, UiPreferences):
            raise TypeError("ui must be UiPreferences")
        object.__setattr__(self, "devices", validate_device_inventory(self.devices))


@dataclass(frozen=True)
class SessionSpec:
    """One bus session and its single explicit UART listen/open profile.

    Passive and slave roles keep ``listen_serial`` while receiving; master
    retunes the same handle to each target device profile at transaction
    boundaries.
    """

    mode: BusMode
    port: str
    devices: tuple[DeviceConfig, ...]
    listen_serial: SerialFormat
    response_timeout_s: float = 1.0
    raw_capacity: int = 10_000
    continuous_cycle_delay_s: float = 0.250

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", BusMode(self.mode))
        if not isinstance(self.port, str) or not self.port.strip():
            raise ValueError("port must be a non-empty string")
        if not isinstance(self.listen_serial, SerialFormat):
            raise TypeError("listen_serial must be a SerialFormat")
        if self.response_timeout_s <= 0:
            raise ValueError("response_timeout_s must be positive")
        if isinstance(self.raw_capacity, bool) or not isinstance(self.raw_capacity, int) or self.raw_capacity < 1:
            raise ValueError("raw_capacity must be positive")
        if self.continuous_cycle_delay_s < 0:
            raise ValueError("continuous_cycle_delay_s cannot be negative")
        devices = validate_device_inventory(self.devices)
        object.__setattr__(self, "devices", devices)


@dataclass(frozen=True)
class DeviceRuntimeState:
    device_id: DeviceId
    address: int
    values: Mapping[str, Any]
    pending_parameter: str | None = None
    pending_data: str | None = None

    def __post_init__(self) -> None:
        device_id = DeviceId(self.device_id)
        _validate_device_address(device_id, self.address)
        object.__setattr__(self, "device_id", device_id)
        object.__setattr__(self, "values", _as_mapping(self.values))


def runtime_state_for(device: DeviceConfig) -> DeviceRuntimeState:
    return DeviceRuntimeState(device.device_id, device.address, dict(device.fake_values))


def default_runtime_states(devices: Sequence[DeviceConfig] | None = None) -> dict[DeviceId, DeviceRuntimeState]:
    inventory = validate_device_inventory(default_devices() if devices is None else devices)
    return {device.device_id: runtime_state_for(device) for device in inventory}


@dataclass(frozen=True)
class MasterOperation:
    """A small protocol-neutral master request."""

    kind: str
    parameter: str | None = None
    value: Any = None
    address: int | None = None
    command: str | None = None
    data: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("operation kind must be non-empty")
        if self.parameter is not None and len(self.parameter) != 1:
            raise ValueError("parameter must be one character")
        if self.address is not None and (isinstance(self.address, bool) or not isinstance(self.address, int) or not 0 <= self.address <= 255):
            raise ValueError("operation address must be in range 0..255")
        object.__setattr__(self, "metadata", _as_mapping(self.metadata))


@dataclass(frozen=True)
class TransactionStep:
    tx: bytes
    expectation: Any = None
    timeout_s: float = 1.0
    recovery_delay_s: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.tx, bytes) or not self.tx:
            raise ValueError("transaction step tx must be non-empty bytes")
        if self.timeout_s <= 0:
            raise ValueError("transaction step timeout must be positive")
        if self.recovery_delay_s < 0:
            raise ValueError("recovery delay cannot be negative")


@dataclass(frozen=True)
class TransactionPlan:
    protocol: ProtocolId
    steps: tuple[TransactionStep, ...]
    is_write: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol", ProtocolId(self.protocol))
        if not self.steps:
            raise ValueError("transaction plan requires at least one step")
        object.__setattr__(self, "steps", tuple(self.steps))


@dataclass(frozen=True)
class DecodedFrame:
    protocol: ProtocolId
    raw: bytes
    direction: Direction
    kind: str
    address: int | None = None
    parameter: str | None = None
    status: str | None = None
    data: str | None = None
    prefix: str | None = None
    command: str | None = None
    payload: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol", ProtocolId(self.protocol))
        object.__setattr__(self, "direction", Direction(self.direction))
        object.__setattr__(self, "metadata", _as_mapping(self.metadata))


@dataclass(frozen=True)
class ParseError:
    message: str
    raw: bytes = b""


@dataclass(frozen=True)
class TrafficRecord:
    seq: int
    monotonic_ns: int
    wall_time_utc: datetime
    direction: Direction
    data: bytes
    mode: BusMode
    protocol: ProtocolId | None = None
    device_id: DeviceId | None = None
    transaction_id: UUID | None = None
    flags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "direction", Direction(self.direction))
        object.__setattr__(self, "mode", BusMode(self.mode))
        if self.protocol is not None:
            object.__setattr__(self, "protocol", ProtocolId(self.protocol))
        if self.device_id is not None:
            object.__setattr__(self, "device_id", DeviceId(self.device_id))
        object.__setattr__(self, "flags", frozenset(self.flags))


@dataclass(frozen=True)
class FrameEvent:
    frame_id: int
    monotonic_ns: int
    direction: Direction
    raw_frame: bytes
    first_traffic_seq: int
    last_traffic_seq: int
    protocol: ProtocolId
    device_id: DeviceId | None = None
    transaction_id: UUID | None = None
    decoded: DecodedFrame | None = None
    error: ParseError | None = None
    flags: frozenset[str] = frozenset()
    mode: BusMode = BusMode.PASSIVE
    wall_time_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        object.__setattr__(self, "direction", Direction(self.direction))
        object.__setattr__(self, "protocol", ProtocolId(self.protocol))
        if self.device_id is not None:
            object.__setattr__(self, "device_id", DeviceId(self.device_id))
        object.__setattr__(self, "flags", frozenset(self.flags))
        object.__setattr__(self, "mode", BusMode(self.mode))
        if self.wall_time_utc.tzinfo is None:
            object.__setattr__(self, "wall_time_utc", self.wall_time_utc.replace(tzinfo=timezone.utc))


# Adapter-only compatibility values. They are not AppConfig fields and are
# never serialized; protocols/base.py still imports them for direct adapter
# tests and third-party adapter code.
@dataclass(frozen=True)
class ProtocolProfile:
    serial: SerialFormat
    device_address: int

    def __post_init__(self) -> None:
        if not isinstance(self.serial, SerialFormat):
            raise TypeError("serial must be a SerialFormat")
        if not isinstance(self.device_address, int) or not 0 <= self.device_address <= 255:
            raise ValueError("device_address must be in the range 0..255")

    @property
    def framing(self) -> str:
        return self.serial.framing

    def is_documented(self, protocol: ProtocolId) -> bool:
        return is_documented_serial(protocol, self.serial)


@dataclass(frozen=True)
class SlaveProfile:
    name: str = "default"
    protocol: ProtocolId = ProtocolId.PARTLOW_P1160
    address: int = 1
    values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("slave profile name must be non-empty")
        object.__setattr__(self, "protocol", ProtocolId(self.protocol))
        if isinstance(self.address, bool) or not isinstance(self.address, int) or not 0 <= self.address <= 255:
            raise ValueError("slave address must be in the range 0..255")
        object.__setattr__(self, "values", _as_mapping(self.values))
