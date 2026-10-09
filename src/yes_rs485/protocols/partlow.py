"""Partlow P1160 ASCII framing, numeric codec, and adapter policy.

Only the documented P1160 ASCII grammar is implemented here.  In particular,
the adapter expects native 7E1 on the serial port; it does not transform an
8N1 byte stream or invent a checksum.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from ..models import (
    BusMode,
    DecodedFrame,
    Direction,
    MasterOperation,
    PARTLOW_DOCUMENTED_BAUDRATES,
    PARTLOW_DOCUMENTED_FRAMINGS,
    ProtocolId,
)
from .base import (
    Ignore,
    IncrementalFramer,
    MatchResult,
    NoReply,
    ProtocolAdapter,
    ProtocolErrorReply,
    Reply,
    ResponseExpectation,
    SerialDefaults,
    SlaveDecision,
    SlaveState,
)
from ..models import TransactionPlan, TransactionStep


class PartlowFrameError(ValueError):
    """Raised when a complete Partlow frame violates the grammar."""


class PartlowFramer:
    """Incrementally split ``L...*`` frames and resynchronize after noise."""

    max_frame_length = 128

    def __init__(self, max_frame_length: int = 128) -> None:
        if max_frame_length < 8:
            raise ValueError("max_frame_length is too small for a Partlow frame")
        self.max_frame_length = max_frame_length
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, data: bytes) -> list[bytes]:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("framer input must be bytes-like")
        self._buffer.extend(bytes(data))
        out: list[bytes] = []
        while True:
            start = self._buffer.find(b"L")
            if start < 0:
                # Keep no more than one possible start byte (already absent).
                self._buffer.clear()
                break
            if start:
                del self._buffer[:start]
            terminator = self._buffer.find(b"*", 1)
            if terminator < 0:
                if len(self._buffer) > self.max_frame_length:
                    # Drop the malformed candidate, then continue looking for
                    # a later L that may be embedded in the noise.
                    del self._buffer[0]
                    continue
                break
            end = terminator + 1
            if end > self.max_frame_length:
                del self._buffer[:end]
                continue
            out.append(bytes(self._buffer[:end]))
            del self._buffer[:end]
        return out


def _decimal_from_value(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a Partlow numeric value")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid Partlow numeric value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError("Partlow numeric value must be finite")
    return result


def encode_data(value: Any, decimals: int | None = None) -> str:
    """Encode a numeric value into the P1160 five-character DATA field.

    ``decimals`` can force the decimal position (0..3).  Without it, a
    ``Decimal``'s scale, or the scale represented by ``str(value)``, is kept.
    The first four characters are magnitude digits and the final character
    encodes sign and decimal position.
    """

    number = _decimal_from_value(value)
    if decimals is None:
        exponent = number.as_tuple().exponent
        decimals = max(0, -exponent) if isinstance(exponent, int) else 0
    if decimals not in (0, 1, 2, 3):
        raise ValueError("Partlow DATA supports 0..3 decimal places")
    quantized = number.quantize(Decimal(1).scaleb(-decimals))
    if quantized != number:
        raise ValueError("value has more decimal places than requested")
    negative = quantized.is_signed()
    magnitude = abs(quantized)
    scaled = int(magnitude * (10**decimals))
    if scaled > 9999:
        raise ValueError("Partlow DATA magnitude exceeds four digits")
    digits = f"{scaled:04d}"
    suffix = decimals + (5 if negative else 0)
    return digits + str(suffix)


def decode_data(data: str | bytes) -> Decimal:
    """Decode a P1160 five-character DATA field to :class:`Decimal`."""

    if isinstance(data, bytes):
        try:
            data = data.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("Partlow DATA is not ASCII") from exc
    if len(data) != 5 or any(ch not in "0123456789" for ch in data[:4]) or data[4] not in "01235678":
        raise ValueError(f"invalid Partlow DATA field: {data!r}")
    code = int(data[4])
    decimals = code if code <= 3 else code - 5
    result = Decimal(int(data[:4])).scaleb(-decimals)
    return -result if code >= 5 else result


# Explicit names make call sites and tests self-documenting.
encode_numeric_data = encode_data
decode_numeric_data = decode_data


def format_address(address: int, width: int | None = None) -> str:
    if isinstance(address, bool) or not isinstance(address, int) or not 1 <= address <= 99:
        raise ValueError("Partlow address must be in the range 1..99")
    if width is None:
        width = 1 if address < 10 else 2
    if width not in (1, 2) or width == 1 and address > 9:
        raise ValueError("Partlow address width must be one or two digits")
    return f"{address:0{width}d}"


def _candidate_addresses(text: str, expected_address: int | None) -> list[tuple[int, str, str]]:
    """Return (address, address text, payload) candidates for a body."""

    if not text.startswith("L"):
        return []
    choices: list[str]
    if expected_address is not None:
        choices = [format_address(expected_address, 1 if expected_address < 10 else 2)]
        if expected_address < 10:
            choices.append(format_address(expected_address, 2))
    else:
        choices = [text[1:3], text[1:2]]
    found: list[tuple[int, str, str]] = []
    for addr_text in choices:
        if not addr_text or not addr_text.isdigit():
            continue
        address = int(addr_text)
        if not 1 <= address <= 99 or not text.startswith("L" + addr_text):
            continue
        payload = text[1 + len(addr_text):]
        if (address, addr_text, payload) not in found:
            found.append((address, addr_text, payload))
    return found


def _parse_frame(frame: bytes, expected_address: int | None = None) -> dict[str, Any]:
    if not isinstance(frame, (bytes, bytearray)):
        raise PartlowFrameError("frame must be bytes")
    raw = bytes(frame)
    if not raw.endswith(b"*"):
        raise PartlowFrameError("Partlow frame must terminate with '*'")
    try:
        text = raw[:-1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise PartlowFrameError("Partlow frame is not ASCII") from exc
    candidates = _candidate_addresses(text, expected_address)
    if not candidates:
        raise PartlowFrameError("invalid Partlow start or address")
    # Prefer the candidate whose remainder is a known grammar.  This resolves
    # one-vs-two digit addresses without assuming a fixed width globally.
    for address, addr_text, payload in candidates:
        result = _parse_payload(payload)
        if result is not None:
            result.update(address=address, address_text=addr_text)
            return result
    raise PartlowFrameError("invalid Partlow payload grammar")


def _parse_payload(payload: str) -> dict[str, Any] | None:
    if payload == "??":
        return {"kind": "type1_request", "payload": payload}
    if payload in ("?A", "?N"):
        return {"kind": "type1_response", "status": payload[1], "payload": payload}
    if not payload or not (0x21 <= ord(payload[0]) <= 0x7E) or payload[0] in "*#":
        return None
    if len(payload) == 2 and payload[1] in "?+-":
        return {
            "kind": "type2_request",
            "parameter": payload[0],
            "command": payload[1],
            "payload": payload,
        }
    if len(payload) == 2 and payload[1] == "I":
        return {"kind": "type4_request", "parameter": payload[0], "payload": payload}
    if len(payload) >= 3 and payload[1] == "#" and len(payload[2:]) == 5:
        data = payload[2:]
        try:
            decode_data(data)
        except ValueError:
            return None
        return {"kind": "type3_request", "parameter": payload[0], "data": data, "payload": payload}
    if len(payload) >= 3 and payload[-1] in "AIN":
        parameter = payload[0]
        data = payload[1:-1]
        if len(data) == 5:
            # P1160 documents two non-numeric PV/deviation range sentinels;
            # they use the normal five-character DATA envelope but are only
            # valid on an acknowledged M/V read.
            range_sentinel = (
                payload[-1] == "A"
                and parameter in {"M", "V"}
                and data in {"<??>0", "<??>5"}
            )
            if not range_sentinel:
                try:
                    decode_data(data)
                except ValueError:
                    # DATA is indeterminate on a negative ACK, so retain it
                    # for diagnostics rather than rejecting the complete
                    # frame.
                    if payload[-1] != "N":
                        return None
        elif parameter == "]" and payload[-1] == "A":
            # The P1160 scan-table reply is a Type-2 response whose DATA is
            # a two-character length marker followed by five five-character
            # numeric fields, not one ordinary DATA value.
            if len(data) != 27 or not data.startswith("25"):
                return None
            for offset in range(2, len(data), 5):
                try:
                    decode_data(data[offset:offset + 5])
                except ValueError:
                    return None
        elif payload[-1] != "N":
            return None
        return {
            "kind": "type2_response" if payload[-1] != "I" else "type3_response",
            "parameter": parameter,
            "data": data,
            "status": payload[-1],
            "payload": payload,
        }
    return None


_PARTLOW_UNMODIFIABLE = frozenset({"M", "V", "L", "]"})
_PARTLOW_CONTROLLER_COMMANDS = frozenset({
    "00010", "00020", "00030", "00040", "00050", "00060", "00130", "00140",
})


class PartlowAdapter:
    id = ProtocolId.PARTLOW_P1160
    encode_data = staticmethod(encode_data)
    decode_data = staticmethod(decode_data)
    format_address = staticmethod(format_address)

    def __init__(self, device_address: int = 1, address_width: int | None = None,
                 baudrate: int = 9600) -> None:
        self.device_address = device_address
        # Validate and retain canonical settings once at construction.
        format_address(device_address, address_width)
        self.address_width = address_width
        if baudrate <= 0:
            raise ValueError("baudrate must be positive")
        self.baudrate = baudrate

    def default_serial_settings(self) -> SerialDefaults:
        return SerialDefaults(
            9600, 7, "E", 1,
            supported_baudrates=PARTLOW_DOCUMENTED_BAUDRATES,
            documented_framings=tuple(sorted(PARTLOW_DOCUMENTED_FRAMINGS)),
        )

    def new_framer(self, role: BusMode) -> IncrementalFramer:
        return PartlowFramer()

    def _address_for(self, operation: MasterOperation) -> int:
        address = self.device_address if operation.address is None else operation.address
        format_address(address, self.address_width)
        return address

    def _address_text(self, address: int, operation: MasterOperation | None = None) -> str:
        width = self.address_width
        if operation is not None:
            requested = operation.metadata.get("address_width")
            if requested is not None:
                width = int(requested)
        return format_address(address, width)

    def compile(self, operation: MasterOperation, timeout_s: float) -> TransactionPlan:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        address = self._address_for(operation)
        addr_text = self._address_text(address, operation)
        kind = (operation.kind or "").lower().replace("-", "_")
        parameter = operation.parameter
        if kind in {"probe", "presence", "type1", "type1_probe"}:
            tx = f"L{addr_text}??*".encode("ascii")
            expected = ResponseExpectation(self.id, "type1_response", address,
                                           statuses=frozenset({"A", "N"}))
            return TransactionPlan(self.id, (TransactionStep(tx, expected, timeout_s),), False)
        if parameter is None:
            raise ValueError("Partlow operation requires a parameter")
        if (
            len(parameter) != 1
            or not parameter.isascii()
            or not (0x21 <= ord(parameter) <= 0x7E)
            or parameter in "*#"
        ):
            raise ValueError("Partlow parameter must be one non-delimiter character")
        if kind in {"read", "query", "type2", "type2_read", "increment", "decrement", "adjust"}:
            command = "?"
            if kind in {"increment", "decrement"}:
                command = "+" if kind == "increment" else "-"
            elif kind == "adjust":
                command = str(operation.metadata.get("direction", "?"))
                if command not in "?+-":
                    raise ValueError("adjust direction must be ?, +, or -")
            tx = f"L{addr_text}{parameter}{command}*".encode("ascii")
            expected = ResponseExpectation(self.id, "type2_response", address, parameter,
                                           frozenset({"A", "N"}))
            return TransactionPlan(self.id, (TransactionStep(tx, expected, timeout_s),), False)
        if kind in {"write", "absolute_write", "type3_4", "set"}:
            data = operation.data
            if data is None:
                data = encode_data(operation.value)
            if len(data) != 5:
                raise ValueError("Partlow write DATA must be five characters")
            # Validate normal numeric DATA, including its suffix.
            decode_data(data)
            tx3 = f"L{addr_text}{parameter}#{data}*".encode("ascii")
            tx4 = f"L{addr_text}{parameter}I*".encode("ascii")
            exp3 = ResponseExpectation(self.id, "type3_response", address, parameter,
                                       frozenset({"I", "N"}))
            exp4 = ResponseExpectation(self.id, "type2_response", address, parameter,
                                       frozenset({"A", "N"}))
            return TransactionPlan(self.id, (
                TransactionStep(tx3, exp3, timeout_s),
                TransactionStep(tx4, exp4, timeout_s),
            ), True)
        raise ValueError(f"unsupported Partlow operation kind: {operation.kind!r}")

    def decode(self, frame: bytes, direction: Direction, role: BusMode) -> DecodedFrame:
        parsed = _parse_frame(frame)
        return DecodedFrame(
            protocol=self.id,
            raw=bytes(frame),
            direction=direction,
            kind=parsed["kind"],
            address=parsed["address"],
            parameter=parsed.get("parameter"),
            status=parsed.get("status"),
            data=parsed.get("data"),
            payload=parsed.get("payload", ""),
            metadata={
                "address_text": parsed["address_text"],
                "command": parsed.get("command"),
            },
        )

    def match(self, expected: ResponseExpectation, actual: DecodedFrame) -> MatchResult:
        if expected.protocol is not self.id or actual.protocol is not self.id:
            return MatchResult.UNRELATED
        if expected.address is not None and actual.address != expected.address:
            return MatchResult.UNRELATED
        # The manual gives a negative acknowledgement the same DATA/status
        # envelope for Type 2 and Type 3, so the decoder cannot distinguish
        # which request stage caused an ``N``.  Address/parameter still have to
        # match, but an N is meaningful for either expected response kind.
        if expected.parameter is not None and actual.parameter != expected.parameter:
            return MatchResult.UNRELATED
        if actual.status == "N":
            # Type-2 and Type-3 negative acknowledgements share the same
            # wire envelope, so either decoded response kind is valid for
            # those expectations.  A Type-1 probe, however, must not be
            # completed by an unrelated parameter NACK.
            if expected.kind == "type1_response" and actual.kind != "type1_response":
                return MatchResult.UNRELATED
            if expected.kind in {"type2_response", "type3_response"} and actual.kind not in {
                "type2_response", "type3_response"
            }:
                return MatchResult.UNRELATED
            return MatchResult.NEGATIVE
        if expected.kind is not None and actual.kind != expected.kind:
            return MatchResult.UNRELATED
        if actual.status in expected.statuses:
            return MatchResult.SUCCESS
        return MatchResult.MALFORMED

    def respond(self, request: DecodedFrame, state: SlaveState) -> SlaveDecision:
        if request.protocol is not self.id or request.address != state.address:
            return Ignore("wrong protocol or address", state)
        addr = format_address(state.address)
        recovery = self.quiet_time_s(self.baudrate)
        if request.kind == "type1_request":
            return Reply(f"L{addr}?A*".encode("ascii"), state_after=state, recovery_s=recovery)
        if request.kind == "type2_request" and request.parameter:
            parameter = request.parameter
            if parameter not in state.values:
                return Reply(f"L{addr}{parameter}?????N*".encode("ascii"),
                             state_after=state, recovery_s=recovery)
            if request.metadata.get("command") in ("+", "-") and parameter in _PARTLOW_UNMODIFIABLE:
                return Reply(f"L{addr}{parameter}?????N*".encode("ascii"),
                             state_after=state, recovery_s=recovery)
            value = state.values[parameter]
            if request.metadata.get("command") in ("+", "-"):
                try:
                    value = _decimal_from_value(value) + (Decimal(1) if request.metadata["command"] == "+" else Decimal(-1))
                except ValueError:
                    return Reply(f"L{addr}{parameter}?????N*".encode("ascii"), state_after=state,
                                 recovery_s=recovery)
                next_values = dict(state.values)
                next_values[parameter] = value
                state = SlaveState(state.address, next_values, state.pending_parameter, state.pending_data)
            try:
                data = encode_data(value)
            except ValueError:
                return Reply(f"L{addr}{parameter}?????N*".encode("ascii"), state_after=state,
                             recovery_s=recovery)
            return Reply(f"L{addr}{parameter}{data}A*".encode("ascii"), state_after=state,
                         recovery_s=recovery)
        if request.kind == "type3_request" and request.parameter and request.data:
            if (
                request.parameter not in state.values
                or request.parameter in _PARTLOW_UNMODIFIABLE
                or (
                    request.parameter == "Z"
                    and request.data not in _PARTLOW_CONTROLLER_COMMANDS
                )
            ):
                return Reply(f"L{addr}{request.parameter}?????N*".encode("ascii"), state_after=state,
                             recovery_s=recovery)
            next_state = SlaveState(state.address, dict(state.values), request.parameter, request.data)
            return Reply(f"L{addr}{request.parameter}{request.data}I*".encode("ascii"),
                         state_after=next_state, recovery_s=recovery)
        if request.kind == "type4_request" and request.parameter:
            if state.pending_parameter != request.parameter or state.pending_data is None:
                return NoReply(state_after=state, reason="Type 4 without matching Type 3")
            next_values = dict(state.values)
            try:
                next_values[request.parameter] = decode_data(state.pending_data)
            except ValueError:
                return NoReply(state_after=state, reason="pending Type 3 data is invalid")
            next_state = SlaveState(state.address, next_values)
            return Reply(f"L{addr}{request.parameter}{state.pending_data}A*".encode("ascii"),
                         state_after=next_state, recovery_s=recovery)
        return Ignore("unsupported or malformed request", state)

    def quiet_time_s(self, baudrate: int) -> float:
        if baudrate <= 0:
            raise ValueError("baudrate must be positive")
        # 7 data + parity + start + stop = 10 bits/character; Partlow asks for
        # at least three character times before transmitting.
        return 3.0 * 10.0 / baudrate


# A name that reads naturally in code using the adapter as a codec.
PartlowCodec = PartlowAdapter
PartlowProtocol = PartlowAdapter


def decode_frame(frame: bytes, expected_address: int | None = None) -> dict[str, Any]:
    """Expose grammar parsing for diagnostics and focused tests."""

    return _parse_frame(frame, expected_address)
