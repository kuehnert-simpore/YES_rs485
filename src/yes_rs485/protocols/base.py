"""Narrow protocol adapter contracts and common immutable results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol

from ..models import (
    BusMode,
    DecodedFrame,
    Direction,
    MasterOperation,
    ProtocolId,
    SlaveProfile,
    TransactionPlan,
)


@dataclass(frozen=True)
class SerialDefaults:
    baudrate: int
    bytesize: int
    parity: str
    stopbits: float
    xonxoff: bool = False
    rtscts: bool = False
    dsrdtr: bool = False
    # Metadata for settings pickers/diagnostics.  Empty tuples preserve the
    # old adapter contract for third-party protocol implementations.
    supported_baudrates: tuple[int, ...] = ()
    documented_framings: tuple[str, ...] = ()

    @property
    def framing(self) -> str:
        stopbits = int(self.stopbits) if float(self.stopbits).is_integer() else self.stopbits
        return f"{self.bytesize}{str(self.parity).upper()}{stopbits}"


class IncrementalFramer(Protocol):
    max_frame_length: int

    def feed(self, data: bytes) -> list[bytes]: ...

    def reset(self) -> None: ...


@dataclass(frozen=True)
class ResponseExpectation:
    """What a transaction step needs from a decoded peer frame."""

    protocol: ProtocolId
    kind: str | None = None
    address: int | None = None
    parameter: str | None = None
    statuses: frozenset[str] = frozenset({"A"})
    command: str | None = None
    prefixes: frozenset[str] = frozenset({"*", "?"})
    allow_addressless: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "statuses", frozenset(self.statuses))
        object.__setattr__(self, "prefixes", frozenset(self.prefixes))


class MatchResult(str, Enum):
    SUCCESS = "success"
    NEGATIVE = "negative"
    MALFORMED = "malformed"
    UNRELATED = "unrelated"
    # Readable aliases used by the transaction-loop handoff.
    MATCH_SUCCESS = "success"
    MATCH_NEGATIVE = "negative"
    MALFORMED_FOR_EXPECTED_PEER = "malformed"


@dataclass(frozen=True)
class SlaveState:
    address: int
    values: Mapping[str, Any] = field(default_factory=dict)
    pending_parameter: str | None = None
    pending_data: str | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.address <= 255:
            raise ValueError("slave address must be in range 0..255")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    @classmethod
    def from_profile(cls, profile: SlaveProfile) -> "SlaveState":
        return cls(profile.address, dict(profile.values))


@dataclass(frozen=True)
class Ignore:
    reason: str = "ignored"
    state_after: SlaveState | None = None


@dataclass(frozen=True)
class NoReply:
    state_after: SlaveState | None = None
    recovery_s: float = 0.0
    reason: str = "no reply"


@dataclass(frozen=True)
class Reply:
    data: bytes
    earliest_send_ns: int = 0
    state_after: SlaveState | None = None
    recovery_s: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes) or not self.data:
            raise ValueError("reply data must be non-empty bytes")


@dataclass(frozen=True)
class ProtocolErrorReply:
    data: bytes
    state_after: SlaveState | None = None
    reason: str = "protocol error"


SlaveDecision = Ignore | NoReply | Reply | ProtocolErrorReply


class ProtocolAdapter(Protocol):
    id: ProtocolId

    def default_serial_settings(self) -> SerialDefaults: ...

    def new_framer(self, role: BusMode) -> IncrementalFramer: ...

    def compile(self, operation: MasterOperation, timeout_s: float) -> TransactionPlan: ...

    def decode(self, frame: bytes, direction: Direction, role: BusMode) -> DecodedFrame: ...

    def match(self, expected: ResponseExpectation, actual: DecodedFrame) -> MatchResult: ...

    def respond(self, request: DecodedFrame, state: SlaveState) -> SlaveDecision: ...

    def quiet_time_s(self, baudrate: int) -> float: ...


def normalize_statuses(values: Iterable[str]) -> frozenset[str]:
    return frozenset(str(v).upper() for v in values)
