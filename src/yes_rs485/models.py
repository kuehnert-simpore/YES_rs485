"""Immutable values shared by the UI, serial worker, and protocol adapters.

The protocol modules deliberately accept these small values instead of UI
objects.  This keeps the serial boundary easy to test and makes it impossible
to accidentally persist an active safety arm or an open serial handle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID


class BusMode(str, Enum):
    PASSIVE = "passive"
    MASTER = "master"
    SLAVE = "slave"


class ProtocolId(str, Enum):
    PARTLOW_P1160 = "partlow_p1160"
    MKS_275 = "mks_275"


# These are intentionally data rather than UI constants.  The settings view
# can use them to populate a picker while the protocol/worker layers retain
# the ability to accept an advanced custom value for diagnostics.
COMMON_BAUDRATES: tuple[int, ...] = (
    1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200,
)
PARTLOW_DOCUMENTED_BAUDRATES: tuple[int, ...] = (
    1200, 2400, 4800, 9600, 19200,
)
MKS_DOCUMENTED_BAUDRATES: tuple[int, ...] = (
    300, 600, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200,
)
PARTLOW_DOCUMENTED_FRAMINGS: frozenset[str] = frozenset({"7E1"})
MKS_DOCUMENTED_FRAMINGS: frozenset[str] = frozenset({"8N1", "7E1", "7O1"})


class SafetyArm(str, Enum):
    PLC_DISCONNECTED = "plc_disconnected"
    SENSORS_DISCONNECTED = "sensors_disconnected"


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
    return MappingProxyType(dict(value or {}))


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
        if not isinstance(self.baudrate, int) or self.baudrate <= 0:
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
        """Compact human-readable framing label (for example ``7E1``)."""

        stopbits = int(self.stopbits) if float(self.stopbits).is_integer() else self.stopbits
        return f"{self.bytesize}{self.parity}{stopbits}"

    @property
    def format_label(self) -> str:
        """Alias used by settings/status presenters."""

        return self.framing

    def is_valid_custom(self) -> bool:
        """Whether this format is in the supported advanced/custom subset.

        The generic value object still accepts pyserial's less common 5/6-bit,
        mark/space, and 1.5-stop-bit combinations for backwards compatibility.
        The application-level advanced editor intentionally limits experiments
        to 7/8 data bits, N/E/O parity, and 1/2 stop bits.
        """

        return (
            self.bytesize in (7, 8)
            and self.parity in {"N", "E", "O"}
            and self.stopbits in (1, 2)
        )

    def is_documented_for(self, protocol: ProtocolId) -> bool:
        """Return whether the framing is documented for ``protocol``.

        This deliberately checks framing only.  Use :func:`is_documented_serial`
        when baud-rate support should be included too.
        """

        protocol = ProtocolId(protocol)
        if protocol is ProtocolId.PARTLOW_P1160:
            return self.framing in PARTLOW_DOCUMENTED_FRAMINGS
        if protocol is ProtocolId.MKS_275:
            return self.framing in MKS_DOCUMENTED_FRAMINGS
        return False


def is_documented_serial(
    protocol: ProtocolId,
    serial: SerialFormat,
) -> bool:
    """Return whether a complete device serial profile is documented."""

    protocol = ProtocolId(protocol)
    if not isinstance(serial, SerialFormat):
        raise TypeError("serial must be a SerialFormat")
    if protocol is ProtocolId.PARTLOW_P1160:
        return (
            serial.baudrate in PARTLOW_DOCUMENTED_BAUDRATES
            and serial.framing in PARTLOW_DOCUMENTED_FRAMINGS
        )
    if protocol is ProtocolId.MKS_275:
        return (
            serial.baudrate in MKS_DOCUMENTED_BAUDRATES
            and serial.framing in MKS_DOCUMENTED_FRAMINGS
        )
    return False


def documented_baudrates(protocol: ProtocolId) -> tuple[int, ...]:
    """Return the documented baud picker values for a protocol."""

    protocol = ProtocolId(protocol)
    if protocol is ProtocolId.PARTLOW_P1160:
        return PARTLOW_DOCUMENTED_BAUDRATES
    if protocol is ProtocolId.MKS_275:
        return MKS_DOCUMENTED_BAUDRATES
    return ()


def documented_framings(protocol: ProtocolId) -> tuple[str, ...]:
    """Return documented framing labels for a protocol."""

    protocol = ProtocolId(protocol)
    if protocol is ProtocolId.PARTLOW_P1160:
        return tuple(sorted(PARTLOW_DOCUMENTED_FRAMINGS))
    if protocol is ProtocolId.MKS_275:
        return tuple(sorted(MKS_DOCUMENTED_FRAMINGS))
    return ()


def serial_format_warning(protocol: ProtocolId, serial: SerialFormat) -> str | None:
    """Return the non-blocking warning for a custom device setting, if any."""

    if is_documented_serial(protocol, serial):
        return None
    return "Custom framing — not a documented device configuration."


@dataclass(frozen=True)
class SerialSettings:
    """Port plus native serial framing settings.

    Partlow's documented format is represented directly as 7E1.  No parity
    emulation or pyserial ``rs485_mode`` is hidden here.
    """

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
        fmt = SerialFormat(
            self.baudrate,
            self.bytesize,
            self.parity,
            self.stopbits,
            self.xonxoff,
            self.rtscts,
            self.dsrdtr,
        )
        object.__setattr__(self, "baudrate", fmt.baudrate)
        object.__setattr__(self, "bytesize", fmt.bytesize)
        object.__setattr__(self, "parity", fmt.parity)
        object.__setattr__(self, "stopbits", fmt.stopbits)

    @classmethod
    def from_format(cls, port: str, fmt: SerialFormat) -> "SerialSettings":
        return cls(port, fmt.baudrate, fmt.bytesize, fmt.parity, fmt.stopbits,
                   fmt.xonxoff, fmt.rtscts, fmt.dsrdtr)

    def as_format(self) -> SerialFormat:
        return SerialFormat(self.baudrate, self.bytesize, self.parity,
                            self.stopbits, self.xonxoff, self.rtscts,
                            self.dsrdtr)


@dataclass(frozen=True)
class PortIdentity:
    device: str
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if not self.device.strip():
            raise ValueError("device must be non-empty")
        for name in ("vid", "pid"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class ProtocolProfile:
    serial: SerialFormat
    device_address: int

    def __post_init__(self) -> None:
        if not 0 <= self.device_address <= 255:
            raise ValueError("device_address must be in the range 0..255")

    @property
    def framing(self) -> str:
        return self.serial.framing

    def is_documented(self, protocol: ProtocolId) -> bool:
        return is_documented_serial(protocol, self.serial)


@dataclass(frozen=True)
class UiPreferences:
    # Persist the concrete ttkbootstrap theme when available.  ``system``
    # remains accepted for older configuration files, but the application
    # default is intentionally dark as requested for the engineering UI.
    theme: str = "darkly"
    show_hex: bool = True
    timeline_window_s: float = 30.0

    def __post_init__(self) -> None:
        if self.theme not in {"system", "light", "dark", "darkly", "cyborg", "flatly"}:
            raise ValueError("theme must be system, light, dark, darkly, cyborg, or flatly")
        if self.timeline_window_s <= 0:
            raise ValueError("timeline_window_s must be positive")


@dataclass(frozen=True)
class SlaveProfile:
    """Initial state for a protocol-specific slave emulator.

    Values are intentionally generic because Partlow fields are numeric while
    MKS fields include strings and command state.  Adapters validate values
    again before using them on the wire.
    """

    name: str = "default"
    protocol: ProtocolId = ProtocolId.PARTLOW_P1160
    address: int = 1
    values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("slave profile name must be non-empty")
        if not 0 <= self.address <= 255:
            raise ValueError("slave address must be in the range 0..255")
        object.__setattr__(self, "values", _as_mapping(self.values))


@dataclass(frozen=True)
class PassiveCaptureSettings:
    """Settings for a passive bus listener, kept separate from device setup.

    ``strategy`` is descriptive policy, not a claim that a particular USB-UART
    framing has been validated.  In particular, ``mixed_raw`` means that the
    application records bytes/timestamps and attempts protocol pairing; it does
    not silently present its UART framing as Partlow's physical 7E1 setting.
    """

    bus_baudrate: int = 9600
    strategy: str = "mixed_raw"
    response_pairing_window_s: float = 0.250

    def __post_init__(self) -> None:
        if not isinstance(self.bus_baudrate, int) or self.bus_baudrate <= 0:
            raise ValueError("passive bus_baudrate must be a positive integer")
        if not isinstance(self.strategy, str) or not self.strategy.strip():
            raise ValueError("passive capture strategy must be non-empty")
        if self.response_pairing_window_s <= 0:
            raise ValueError("response_pairing_window_s must be positive")
        object.__setattr__(self, "strategy", self.strategy.strip().lower())

    @property
    def baudrate(self) -> int:
        """Compatibility alias for callers that use device-style naming."""

        return self.bus_baudrate

    @property
    def capture_strategy(self) -> str:
        return self.strategy


@dataclass(frozen=True)
class SessionSpec:
    mode: BusMode
    protocol: ProtocolId
    serial: SerialSettings
    response_timeout_s: float = 1.0
    raw_capacity: int = 10_000
    slave_profile: SlaveProfile | None = None

    def __post_init__(self) -> None:
        if self.response_timeout_s <= 0:
            raise ValueError("response_timeout_s must be positive")
        if self.raw_capacity < 1:
            raise ValueError("raw_capacity must be positive")
        if self.mode is BusMode.SLAVE and self.slave_profile is None:
            raise ValueError("slave mode requires a slave_profile")
        if self.mode is not BusMode.SLAVE and self.slave_profile is not None:
            raise ValueError("slave_profile is only valid in slave mode")


@dataclass(frozen=True)
class AppConfig:
    schema_version: int = 1
    selected_protocol: ProtocolId = ProtocolId.PARTLOW_P1160
    preferred_port: PortIdentity | None = None
    protocol_profiles: Mapping[ProtocolId, ProtocolProfile] = field(default_factory=dict)
    slave_profiles: Mapping[str, SlaveProfile] = field(default_factory=dict)
    response_timeout_s: float = 1.0
    raw_capacity: int = 10_000
    ui: UiPreferences = field(default_factory=UiPreferences)
    # All Partlow controllers on this machine share one UART profile.  The
    # optional spelling keeps direct construction of older configs working;
    # ``__post_init__`` fills it from the legacy profile when present.
    partlow_bus_serial: SerialFormat | None = None
    # Constructor/read alias used by older callers and by compact presenters.
    # It is normalized to the same value as ``partlow_bus_serial``.
    partlow_serial: SerialFormat | None = None
    partlow_addresses: Mapping[int, int] = field(default_factory=dict)
    passive_capture: PassiveCaptureSettings = field(default_factory=PassiveCaptureSettings)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported configuration schema version")
        if self.response_timeout_s <= 0:
            raise ValueError("response_timeout_s must be positive")
        if self.raw_capacity < 1:
            raise ValueError("raw_capacity must be positive")
        selected = ProtocolId(self.selected_protocol)
        object.__setattr__(self, "selected_protocol", selected)
        if self.partlow_bus_serial is None and self.partlow_serial is not None:
            partlow_serial = self.partlow_serial
        elif self.partlow_bus_serial is None:
            legacy_partlow = dict(self.protocol_profiles).get(ProtocolId.PARTLOW_P1160)
            partlow_serial = (
                legacy_partlow.serial
                if legacy_partlow is not None
                else SerialFormat(9600, 7, "E", 1)
            )
        else:
            partlow_serial = self.partlow_bus_serial
        if not isinstance(partlow_serial, SerialFormat):
            raise TypeError("partlow_bus_serial must be a SerialFormat")
        object.__setattr__(self, "partlow_bus_serial", partlow_serial)
        object.__setattr__(self, "partlow_serial", partlow_serial)

        # Persisted addresses are keyed by the machine's PID number (1..11),
        # not by protocol address.  Missing entries receive the machine's
        # documented initial mapping.  A legacy one-address Partlow profile is
        # retained as PID 1 when no new mapping was stored.
        addresses: dict[int, int] = {
            pid: pid + 1 for pid in range(1, 12)
        }
        raw_addresses = dict(self.partlow_addresses)
        legacy_partlow = dict(self.protocol_profiles).get(ProtocolId.PARTLOW_P1160)
        if not raw_addresses and legacy_partlow is not None:
            addresses[1] = legacy_partlow.device_address
        else:
            for raw_pid, raw_address in raw_addresses.items():
                pid = int(raw_pid)
                address = int(raw_address)
                if not 1 <= pid <= 11:
                    raise ValueError("Partlow PID number must be in the range 1..11")
                if not 1 <= address <= 99:
                    raise ValueError("Partlow device address must be in the range 1..99")
                addresses[pid] = address
        object.__setattr__(self, "partlow_addresses", _as_mapping(addresses))
        if not isinstance(self.passive_capture, PassiveCaptureSettings):
            raise TypeError("passive_capture must be PassiveCaptureSettings")

        normalized_profiles: dict[ProtocolId, ProtocolProfile] = {}
        for key, value in dict(self.protocol_profiles).items():
            protocol = ProtocolId(key)
            if not isinstance(value, ProtocolProfile):
                raise TypeError("protocol profile values must be ProtocolProfile instances")
            if protocol is ProtocolId.PARTLOW_P1160 and not 1 <= value.device_address <= 99:
                raise ValueError("Partlow device address must be in the range 1..99")
            if protocol is ProtocolId.MKS_275 and not 0 <= value.device_address <= 15:
                raise ValueError("MKS device address must be in the range 00..0F")
            if protocol is ProtocolId.PARTLOW_P1160:
                # Keep the legacy view usable while guaranteeing that all
                # Partlow consumers see the one shared bus serial profile.
                value = ProtocolProfile(partlow_serial, addresses[1])
            normalized_profiles[protocol] = value
        normalized_slaves = {str(k): v for k, v in dict(self.slave_profiles).items()}
        object.__setattr__(self, "protocol_profiles", _as_mapping(normalized_profiles))
        object.__setattr__(self, "slave_profiles", _as_mapping(normalized_slaves))

    @property
    def partlow_profile(self) -> ProtocolProfile:
        """Legacy-compatible shared Partlow profile view."""

        return ProtocolProfile(self.partlow_serial, self.partlow_addresses[1])


@dataclass(frozen=True)
class MasterOperation:
    """A small protocol-neutral master request.

    ``kind`` is intentionally a string so the UI can expose protocol command
    names without an ever-growing shared enum.  Adapters validate the
    supported kinds and fields before compiling bytes.
    """

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
        if self.address is not None and not 0 <= self.address <= 255:
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
        object.__setattr__(self, "metadata", _as_mapping(self.metadata))


@dataclass(frozen=True)
class ParseError:
    message: str
    raw: bytes = b""


# UUID-typed identifiers are used by the worker but kept here for UI/event
# consumers.  They carry no mutable state and are safe to cross the thread
# boundary.
@dataclass(frozen=True)
class TrafficRecord:
    seq: int
    monotonic_ns: int
    wall_time_utc: Any
    direction: Direction
    data: bytes
    mode: BusMode
    protocol: ProtocolId
    transaction_id: UUID | None = None
    flags: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FrameEvent:
    frame_id: int
    monotonic_ns: int
    direction: Direction
    raw_frame: bytes
    first_traffic_seq: int
    last_traffic_seq: int
    protocol: ProtocolId
    transaction_id: UUID | None = None
    decoded: DecodedFrame | None = None
    error: ParseError | None = None
