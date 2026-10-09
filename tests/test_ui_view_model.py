"""Headless checks for protocol-neutral UI frame evidence classification."""

from types import SimpleNamespace

import pytest

from yes_rs485.ui.app import _frame_role
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
