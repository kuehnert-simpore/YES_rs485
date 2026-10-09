"""MKS Series 275 RS-485 command framing and response policy."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from ..models import (
    BusMode,
    DecodedFrame,
    Direction,
    MasterOperation,
    MKS_DOCUMENTED_BAUDRATES,
    MKS_DOCUMENTED_FRAMINGS,
    ProtocolId,
)
from ..models import TransactionPlan, TransactionStep
from .base import (
    Ignore,
    IncrementalFramer,
    MatchResult,
    NoReply,
    ProtocolErrorReply,
    Reply,
    ResponseExpectation,
    SerialDefaults,
    SlaveDecision,
    SlaveState,
)


class MKS275FrameError(ValueError):
    pass


class MKS275Framer:
    """Incrementally split ``#...CR`` requests or ``*...CR`` responses."""

    max_frame_length = 256

    def __init__(self, max_frame_length: int = 256) -> None:
        if max_frame_length < 6:
            raise ValueError("max_frame_length is too small for MKS frames")
        self.max_frame_length = max_frame_length
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, data: bytes) -> list[bytes]:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("framer input must be bytes-like")
        self._buffer.extend(bytes(data))
        result: list[bytes] = []
        while True:
            starts = [p for p in (self._buffer.find(b"#"), self._buffer.find(b"*"), self._buffer.find(b"?")) if p >= 0]
            if not starts:
                self._buffer.clear()
                break
            start = min(starts)
            if start:
                del self._buffer[:start]
            end = self._buffer.find(b"\r", 1)
            if end < 0:
                if len(self._buffer) > self.max_frame_length:
                    del self._buffer[0]
                    continue
                break
            end += 1
            if end > self.max_frame_length:
                del self._buffer[:end]
                continue
            result.append(bytes(self._buffer[:end]))
            del self._buffer[:end]
        return result


def format_address(address: int) -> str:
    """Format the documented two-ASCII-hex-digit module address."""

    if isinstance(address, bool) or not isinstance(address, int) or not 0 <= address <= 0x0F:
        raise ValueError("MKS 275 address must be in the range 00..0F")
    return f"{address:02X}"


def build_request(address: int, command: str, value: Any = None) -> bytes:
    """Build a command, retaining documented command-specific spaces."""

    address_text = format_address(address)
    body = str(command)
    if value is not None:
        body += str(value)
    if not body or any(ch in body for ch in "#\r\n"):
        raise ValueError("MKS command contains an invalid delimiter")
    # The manual prints a literal separator after these read commands.  The
    # trailing space is part of the wire request (it is not interchangeable
    # with the no-space RH+/RL- forms).
    if body.upper() in {"RD", "RSA", "VER"}:
        body += " "
    try:
        return f"#{address_text}{body}\r".encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("MKS command must be ASCII") from exc


def _split_response(text: str) -> tuple[str, int | None, str]:
    if not text or text[0] not in "*?":
        raise MKS275FrameError("MKS response must begin with '*' or '?'")
    prefix = text[0]
    rest = text[1:]
    address: int | None = None
    payload = rest
    # Normal replies are *01_payload.  RSA is explicitly addressless (*AVG: 04)
    # and must remain parseable as such.
    if len(rest) >= 3 and all(c in "0123456789abcdefABCDEF" for c in rest[:2]) and rest[2] in "_ ":
        address = int(rest[:2], 16)
        payload = rest[3:]
    elif (
        len(rest) >= 5
        and all(c in "0123456789abcdefABCDEF" for c in rest[:2])
        and rest[2:].upper().endswith("PCS")
    ):
        # PCS is the one response whose two relay-state characters are
        # immediately before the command marker.  Depending on firmware/text
        # rendering the separator after the address may be absent; recognize
        # both forms while retaining the status bytes in ``payload``.
        address = int(rest[:2], 16)
        payload = rest[2:]
    elif (
        len(rest) >= 3
        and all(c in "0123456789abcdefABCDEF" for c in rest[:2])
        and rest[2] in "+-"
    ):
        # RH-/RL- replies put the negative sign immediately after the
        # address, because the returned OFF value itself begins with '-'.
        address = int(rest[:2], 16)
        payload = rest[2:]
    return prefix, address, payload


def _parse_frame(frame: bytes) -> dict[str, Any]:
    raw = bytes(frame)
    if not raw.endswith(b"\r"):
        raise MKS275FrameError("MKS frame must terminate with CR")
    try:
        text = raw[:-1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise MKS275FrameError("MKS frame is not ASCII") from exc
    if not text:
        raise MKS275FrameError("empty MKS frame")
    if text[0] == "#":
        if len(text) < 4 or not all(c in "0123456789abcdefABCDEF" for c in text[1:3]):
            raise MKS275FrameError("MKS request must contain two hex address digits")
        address = int(text[1:3], 16)
        payload = text[3:]
        match = re.match(r"([A-Za-z]+(?:[+-])?)(.*)", payload)
        if not match:
            raise MKS275FrameError("MKS request has no command")
        command = match.group(1).upper()
        return {
            "kind": "request",
            "prefix": "#",
            "address": address,
            "payload": payload,
            "command": command,
        }
    prefix, address, payload = _split_response(text)
    return {
        "kind": "response",
        "prefix": prefix,
        "address": address,
        "payload": payload,
        "command": None,
    }


class MKS275Adapter:
    id = ProtocolId.MKS_275
    build_request = staticmethod(build_request)
    format_address = staticmethod(format_address)

    def __init__(self, device_address: int = 1, baudrate: int = 9600) -> None:
        self.device_address = device_address
        format_address(device_address)
        if baudrate <= 0:
            raise ValueError("baudrate must be positive")
        self.baudrate = baudrate

    def default_serial_settings(self) -> SerialDefaults:
        return SerialDefaults(
            9600, 8, "N", 1,
            supported_baudrates=MKS_DOCUMENTED_BAUDRATES,
            documented_framings=tuple(sorted(MKS_DOCUMENTED_FRAMINGS)),
        )

    def new_framer(self, role: BusMode) -> IncrementalFramer:
        return MKS275Framer()

    def _address_for(self, operation: MasterOperation) -> int:
        address = self.device_address if operation.address is None else operation.address
        format_address(address)
        return address

    @staticmethod
    def _command_and_value(operation: MasterOperation) -> tuple[str, Any]:
        command = operation.command or operation.kind
        key = command.strip().upper()
        aliases = {
            "READ": "RD",
            "READ_PRESSURE": "RD",
            "PRESSURE": "RD",
            "RESET": "RST",
            "FACTORY": "FAC",
        }
        key = aliases.get(key, key)
        if key == "RD":
            # RD is documented with a literal trailing space; build_request
            # adds it after this normalized command.
            return key, None
        value = operation.data if operation.data is not None else operation.value
        return key, value

    def compile(self, operation: MasterOperation, timeout_s: float) -> TransactionPlan:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        address = self._address_for(operation)
        command, value = self._command_and_value(operation)
        if not command or any(ch in command for ch in "#\r\n"):
            raise ValueError("invalid MKS command")
        # A caller may provide an already-composed body (SA20, SH+1.0E-1,
        # etc.) through command.  If a separate value is present it is appended
        # exactly as documented by that command's template.
        tx = build_request(address, command, value)
        upper = command.upper()
        if upper == "RST":
            return TransactionPlan(
                self.id,
                (TransactionStep(tx, None, timeout_s, recovery_delay_s=2.0),),
                True,
            )
        allow_addressless = upper == "RSA"
        expected = ResponseExpectation(
            self.id,
            kind="response",
            address=address,
            command=upper,
            prefixes=frozenset({"*", "?"}),
            allow_addressless=allow_addressless,
        )
        write_commands = {
            "SA", "TS", "TZ", "SH", "SL", "FAC", "SB", "SPN", "SPO",
            "SPE", "SU", "SS", "SF", "RST",
        }
        return TransactionPlan(
            self.id,
            (TransactionStep(tx, expected, timeout_s),),
            upper in write_commands,
        )

    def decode(self, frame: bytes, direction: Direction, role: BusMode) -> DecodedFrame:
        parsed = _parse_frame(frame)
        return DecodedFrame(
            protocol=self.id,
            raw=bytes(frame),
            direction=direction,
            kind=parsed["kind"],
            address=parsed["address"],
            prefix=parsed["prefix"],
            command=parsed.get("command"),
            payload=parsed.get("payload", ""),
            metadata={"addressless": parsed["address"] is None},
        )

    def match(self, expected: ResponseExpectation, actual: DecodedFrame) -> MatchResult:
        if expected.protocol is not self.id or actual.protocol is not self.id:
            return MatchResult.UNRELATED
        if expected.kind is not None and actual.kind != expected.kind:
            return MatchResult.UNRELATED
        if actual.prefix not in expected.prefixes:
            return MatchResult.UNRELATED
        # RSA is the documented irregular reply: it is addressless and starts
        # with ``AVG:``.  Do not let a normal addressed reply (or an unrelated
        # addressless error) satisfy an RSA transaction merely because it has
        # the same prefix.
        if expected.command == "RSA":
            if actual.address is not None or not actual.payload.upper().startswith("AVG:"):
                return MatchResult.UNRELATED
        if expected.command == "PCS":
            payload = actual.payload.upper()
            if len(payload) != 5 or not payload.endswith("PCS") or any(bit not in "01" for bit in payload[:2]):
                return MatchResult.UNRELATED
        # Several documented device faults use a ``*`` prefix even though
        # they are not successful results (the ``?`` prefix is reserved for
        # syntax/range errors).  Surface those as a negative transaction
        # result instead of silently reporting success.
        if actual.prefix == "*" and actual.payload.upper() in {
            "HI VOLTAGE", "LO VOLTAGE", "HI_ATM_V", "LO_ATM_V",
            "HI_VAC_V", "LO_VAC_V",
        }:
            return MatchResult.NEGATIVE
        if expected.address is not None:
            if actual.address is None and not expected.allow_addressless:
                return MatchResult.MALFORMED
            if actual.address is not None and actual.address != expected.address:
                return MatchResult.UNRELATED
        return MatchResult.NEGATIVE if actual.prefix == "?" else MatchResult.SUCCESS

    def respond(self, request: DecodedFrame, state: SlaveState) -> SlaveDecision:
        if request.protocol is not self.id or request.address != state.address:
            return Ignore("wrong protocol or address", state)
        command = (request.command or "").upper()
        addr = format_address(state.address)
        if command == "RST":
            return NoReply(state_after=state, recovery_s=2.0, reason="MKS reset has no response")
        if command == "RD":
            value = state.values.get("pressure", "0.00E+00")
            return Reply(f"*{addr}_{value}\r".encode("ascii"), state_after=state,
                         recovery_s=self.response_delay_s(command))
        if command in {"RH+", "RH-", "RL+", "RL-"} and command in state.values:
            value = str(state.values[command]).strip()
            if command.endswith("-"):
                value = value.lstrip("+").lstrip("-")
                return Reply(f"*{addr}-{value}\r".encode("ascii"), state_after=state,
                             recovery_s=self.response_delay_s(command))
            value = value.lstrip("+-")
            return Reply(f"*{addr}_{value}\r".encode("ascii"), state_after=state,
                         recovery_s=self.response_delay_s(command))
        if command == "RSA" and command in state.values:
            # RSA is explicitly documented as addressless (``*AVG: 04``),
            # unlike the normal ``*01_...`` response family.
            return Reply(f"*AVG: {state.values[command]}\r".encode("ascii"), state_after=state,
                         recovery_s=self.response_delay_s(command))
        if command == "PCS" and command in state.values:
            relay_state = str(state.values[command]).upper()
            if not relay_state.endswith("PCS"):
                relay_state += "PCS"
            return Reply(f"*{addr}{relay_state}\r".encode("ascii"), state_after=state,
                         recovery_s=self.response_delay_s(command))
        if command in {"RU", "RF", "RSA", "VER", "PCS"} and command in state.values:
            return Reply(f"*{addr}_{state.values[command]}\r".encode("ascii"), state_after=state,
                         recovery_s=self.response_delay_s(command))
        if command in {"SA", "TS", "TZ", "SH", "SL", "FAC", "SB", "SPN", "SPO", "SPE", "SU", "SS", "SF"}:
            return Reply(f"*{addr}_PROGM_OK\r".encode("ascii"), state_after=state,
                         recovery_s=self.response_delay_s(command))
        return Ignore("unsupported MKS slave command", state)

    def quiet_time_s(self, baudrate: int) -> float:
        # The manual's T is the approximate receive-to-response interval, not
        # a physical-layer guarantee.  Keep the documented table and use the
        # nearest slower entry for uncommon but valid rates.
        if baudrate <= 0:
            raise ValueError("baudrate must be positive")
        table = {
            115200: 0.0033, 57600: 0.0033, 38400: 0.0033,
            19200: 0.0039, 9600: 0.0051, 4800: 0.0075,
            2400: 0.013, 1200: 0.022, 300: 0.079,
        }
        if baudrate in table:
            return table[baudrate]
        supported = sorted(table)
        nearest = min(supported, key=lambda candidate: abs(candidate - baudrate))
        return table[nearest]

    def response_delay_s(self, command: str) -> float:
        command = command.upper()
        if command == "FAC":
            return self.quiet_time_s(self.baudrate) + 0.105
        if command == "RD":
            return self.quiet_time_s(self.baudrate)
        if command == "RST":
            return 2.0
        return self.quiet_time_s(self.baudrate) + 0.017


MKS275Codec = MKS275Adapter
MKS275Protocol = MKS275Adapter


def decode_frame(frame: bytes) -> dict[str, Any]:
    """Expose grammar parsing for diagnostics and focused tests."""

    return _parse_frame(frame)
