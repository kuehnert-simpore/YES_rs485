"""Tk views for the whole-bus YES RS-485 application.

The widgets in this module contain presentation state only. A ``DeviceCard``
is shared by the passive, master, and slave pages and receives immutable
configuration/runtime snapshots from :mod:`yes_rs485.ui.app`.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # pragma: no cover
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]


def _value(record: Any, name: str, default: Any = "") -> Any:
    if record is None:
        return default
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _hex(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    try:
        return bytes(data).hex(" ").upper()
    except (TypeError, ValueError):
        return _text(data)


def _ascii(data: Any) -> str:
    """Return a readable ASCII representation without hiding wire controls.

    The hexadecimal column remains the authoritative byte-for-byte view.  In
    the human-facing ASCII column, control bytes are named so a frame ending
    in CR/LF (or containing spaces) is not mistaken for a truncated value.
    """
    if data is None:
        return ""
    if isinstance(data, str):
        raw = data.encode("utf-8", "replace")
    else:
        try:
            raw = bytes(data)
        except (TypeError, ValueError):
            return _text(data)
    try:
        names = {
            0x00: "NUL", 0x01: "SOH", 0x02: "STX", 0x03: "ETX",
            0x04: "EOT", 0x05: "ENQ", 0x06: "ACK", 0x07: "BEL",
            0x08: "BS", 0x09: "TAB", 0x0A: "LF", 0x0B: "VT",
            0x0C: "FF", 0x0D: "CR", 0x0E: "SO", 0x0F: "SI",
            0x10: "DLE", 0x11: "DC1", 0x12: "DC2", 0x13: "DC3",
            0x14: "DC4", 0x15: "NAK", 0x16: "SYN", 0x17: "ETB",
            0x18: "CAN", 0x19: "EM", 0x1A: "SUB", 0x1B: "ESC",
            0x1C: "FS", 0x1D: "GS", 0x1E: "RS", 0x1F: "US",
            0x20: "SP", 0x7F: "DEL",
        }
        return "".join(
            chr(byte) if 0x21 <= byte <= 0x7E else f"<{names.get(byte, f'0x{byte:02X}')}>"
            for byte in raw
        )
    except Exception:  # pragma: no cover - defensive fallback for odd inputs
        return _text(data)


def _when(record: Any) -> str:
    value = _value(record, "wall_time_utc", _value(record, "timestamp", ""))
    if isinstance(value, datetime):
        return value.astimezone().strftime("%H:%M:%S.%f")[:-3]
    monotonic = _value(record, "monotonic_ns", "")
    return f"{monotonic} ns" if monotonic != "" else _text(value)


def _format_event_time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return _text(value) if value not in (None, "") else "—"


def _decoded_summary(decoded: Any) -> str:
    if decoded is None:
        return ""
    parts: list[str] = []
    kind = _text(_value(decoded, "kind", "")).strip()
    if kind:
        parts.append(kind)
    address = _value(decoded, "address", None)
    if address not in (None, ""):
        parts.append(f"addr={address}")
    metadata = _value(decoded, "metadata", {})
    command = _value(decoded, "command", "")
    if not command and isinstance(metadata, Mapping):
        command = metadata.get("command", "")
    for name, label in (("parameter", "param"), ("status", "status"), ("command", "cmd"), ("data", "data"), ("payload", "payload")):
        value = command if name == "command" else _value(decoded, name, "")
        value_text = _text(value).strip()
        if value_text and not (name == "payload" and value_text == _text(_value(decoded, "data", "")).strip()):
            parts.append(f"{label}={value_text}")
    return " ".join(parts)[:240] if parts else "Decoded frame"


def _device_key(device: Any) -> str:
    return _text(_value(device, "device_id", device)).lower()


def _protocol_label(protocol: Any) -> str:
    return {"partlow_p1160": "Partlow P1160", "mks_275": "MKS 275"}.get(_text(protocol), _text(protocol))


if tk is None:  # pragma: no cover

    class _NoTk:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("Tkinter is not available in this Python installation")

    BaseView = DeviceCard = PassiveView = MasterView = SlaveView = SettingsView = RawTrafficView = TimelineView = _NoTk

else:

    class BaseView(ttk.Frame):
        def __init__(self, parent: tk.Misc, **kwargs: Any) -> None:
            super().__init__(parent, padding=10, **kwargs)
            self.columnconfigure(0, weight=1)
            self.rowconfigure(1, weight=1)

        def _heading(self, title: str, subtitle: str = "") -> None:
            ttk.Label(self, text=title, style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 2))
            if subtitle:
                ttk.Label(self, text=subtitle, style="Muted.TLabel").grid(row=0, column=1, sticky="e", pady=(0, 2))

        def set_status(self, text: str) -> None:
            if hasattr(self, "status"):
                self.status.set(text)


    class DeviceCard(ttk.LabelFrame):
        """Compact presentation of one canonical bus device."""

        def __init__(
            self,
            parent: tk.Misc,
            device: Any,
            mode: str = "passive",
            on_poll: Callable[[str], None] | None = None,
            on_apply: Callable[[str, Mapping[str, Any]], None] | None = None,
            **kwargs: Any,
        ) -> None:
            self.device = device
            self.device_id = _device_key(device)
            self.mode = mode
            self.on_poll = on_poll
            self.on_apply = on_apply
            name = "GP" if self.device_id == "gp" else self.device_id.replace("_", " ").upper()
            kwargs.setdefault("style", "Device.TLabelframe")
            super().__init__(parent, text=name, padding=4, **kwargs)
            self.columnconfigure(1, weight=1)
            self.columnconfigure(3, weight=1)
            self.protocol_text = _protocol_label(_value(device, "protocol", ""))
            self.address = tk.StringVar(value=str(_value(device, "address", "")))
            serial = _value(device, "serial", None)
            serial_text = _text(_value(serial, "framing", ""))
            if _value(serial, "baudrate", ""):
                serial_text = f"{_value(serial, 'baudrate', '')}/{serial_text}"
            self.serial = tk.StringVar(value=serial_text)
            self.identity_label = ttk.Label(self, text=f"{self.device_id}  ·  {self.protocol_text}", style="Muted.TLabel")
            self.identity_label.grid(row=0, column=0, columnspan=4, sticky="w")
            ttk.Label(self, text="Address").grid(row=1, column=0, sticky="w")
            self.address_label = ttk.Label(self, textvariable=self.address); self.address_label.grid(row=1, column=1, sticky="w")
            self.serial_label = ttk.Label(self, textvariable=self.serial, style="Muted.TLabel"); self.serial_label.grid(row=1, column=2, sticky="w")
            self._has_observation = False
            self.activity = tk.StringVar(value="Not observed")
            self.activity_label = ttk.Label(self, textvariable=self.activity, style="Muted.TLabel")
            self.activity_label.grid(row=1, column=3, sticky="e")
            self.last_request = tk.StringVar(value="Request —")
            self.last_response = tk.StringVar(value="Response —")
            self.last_request_at = tk.StringVar(value="Last request —")
            self.last_response_at = tk.StringVar(value="Last response —")
            self.last_error = tk.StringVar(value="")
            ttk.Label(self, textvariable=self.last_request, anchor="w").grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
            ttk.Label(self, textvariable=self.last_response, anchor="w").grid(row=2, column=2, columnspan=2, sticky="ew", pady=(4, 0))
            if mode == "passive":
                ttk.Label(self, textvariable=self.last_request_at, anchor="w", style="Muted.TLabel").grid(row=3, column=0, columnspan=2, sticky="ew")
                ttk.Label(self, textvariable=self.last_response_at, anchor="w", style="Muted.TLabel").grid(row=3, column=2, columnspan=2, sticky="ew")
                error_row, values_row = 4, 5
            else:
                error_row, values_row = 3, 4
            self._error_label = ttk.Label(self, textvariable=self.last_error, anchor="w", style="CardWarning.TLabel")
            self._error_label.grid(row=error_row, column=0, columnspan=4, sticky="ew")
            self._error_label.grid_remove()
            self.values_text = tk.StringVar(value="No traffic yet · configured values")
            ttk.Label(self, textvariable=self.values_text, anchor="w", wraplength=0, style="CardValues.TLabel").grid(row=values_row, column=0, columnspan=4, sticky="ew", pady=(4, 0))
            self._fields: dict[str, tk.StringVar] = {}
            self._respond = tk.BooleanVar(value=True)
            self._evidence_generation = 0
            self._pending_request_generation: int | None = None
            self._pending_no_response_after: str | None = None
            if mode == "master":
                self.poll_button = ttk.Button(self, text="Poll", command=self._poll, width=7)
                self.poll_button.grid(row=6 if mode != "passive" else 7, column=0, sticky="w", pady=(5, 0))
            elif mode == "slave":
                values = dict(_value(device, "fake_values", {}) or {})
                names = ("pressure", "units") if self.device_id == "gp" else ("M", "S", "W")
                editor = ttk.Frame(self)
                editor.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(2, 0))
                editor.columnconfigure(1, weight=1)
                editor.columnconfigure(3, weight=1)
                labels = {"pressure": "Pressure", "units": "Units", "M": "PV", "S": "SP", "W": "Output"}
                for offset, key in enumerate(names):
                    variable = tk.StringVar(value=str(values.get(key, "")))
                    if key == "pressure" and not variable.get():
                        variable.set(str(values.get("pressure", "0.00E+00")))
                    if key == "units" and not variable.get():
                        variable.set(str(values.get("units", "Torr")))
                    if key == "W" and not variable.get():
                        variable.set(str(values.get("output", 0)))
                    self._fields[key] = variable
                    col = offset * 2
                    ttk.Label(editor, text=f"{labels[key]}:").grid(row=0, column=col, sticky="w", padx=(0, 2))
                    ttk.Entry(editor, textvariable=variable, width=8).grid(row=0, column=col + 1, sticky="ew", padx=(0, 4 if offset < len(names) - 1 else 0))
                self._respond.set(bool(values.get("respond_enabled", True)))
                ttk.Checkbutton(self, text="Respond enabled", variable=self._respond).grid(row=6, column=0, columnspan=3, sticky="w", pady=(3, 0))
                self.apply_button = ttk.Button(self, text="Apply", command=self._apply, width=7)
                self.apply_button.grid(row=6, column=3, sticky="e", pady=(3, 0))
            self.set_runtime_state(_value(device, "fake_values", {}), online=False)

        def _poll(self) -> None:
            if self.on_poll:
                self.on_poll(self.device_id)

        def _apply(self) -> None:
            if self.on_apply:
                values = {key: variable.get().strip() for key, variable in self._fields.items()}
                values["respond_enabled"] = bool(self._respond.get())
                self.on_apply(self.device_id, values)

        def set_enabled(self, enabled: bool) -> None:
            if hasattr(self, "poll_button"):
                self.poll_button.configure(state="normal" if enabled else "disabled")
            if hasattr(self, "apply_button"):
                self.apply_button.configure(state="normal" if enabled else "disabled")

        def set_runtime_state(self, values: Mapping[str, Any] | Any, online: bool = True) -> None:
            mapping = dict(values or {}) if isinstance(values, Mapping) else dict(_value(values, "values", {}) or {})
            if online:
                self._has_observation = True
                self.activity.set("Observed")
                self.activity_label.configure(style="Status.TLabel")
            elif self._has_observation:
                self.activity.set("Stale")
                self.activity_label.configure(style="Warning.TLabel")
            else:
                self.activity.set("Not observed")
                self.activity_label.configure(style="Muted.TLabel")
            # Runtime notifications update the read-only value summary. Keep
            # editor variables untouched: a worker snapshot must not erase a
            # partially entered fake value (including an intentional blank).
            if self.device_id == "gp":
                bits = [f"Pressure: {mapping.get('pressure', '0.00E+00')}", f"Units: {mapping.get('units', 'Torr')}"]
            else:
                pv = mapping.get("M", mapping.get("pv", "—")); sp = mapping.get("S", mapping.get("sp", "—"))
                output = mapping.get("W", mapping.get("output", "—"))
                deviation = mapping.get("V", mapping.get("deviation", "—"))
                if deviation == "—":
                    try:
                        from decimal import Decimal
                        deviation = Decimal(str(pv)) - Decimal(str(sp))
                    except (ArithmeticError, TypeError, ValueError):
                        pass
                bits = [f"PV: {pv}", f"SP: {sp}", f"Out: {output}", f"Dev: {deviation}"]
            # Observation state is intentionally carried by the compact status
            # label above (Not observed / Observed / Stale); keep this value
            # line reserved for aligned process values so realistic numbers do
            # not get clipped inside the twelve-card grid.
            self.values_text.set(" · ".join(bits))

        def set_evidence(self, evidence: str, *, request: bool = False, response: bool = False, error: bool = False) -> None:
            """Apply live evidence while keeping at most one timeout per card."""
            if request:
                self._cancel_no_response_timer()
                self._evidence_generation += 1
                generation = self._evidence_generation
                self._pending_request_generation = generation
                self.last_request.set(f"Request {evidence}")
                # A new request starts a new response wait.  Do not use the
                # historical response text as the pending-state sentinel.
                self.last_response.set("Response —")
                self.last_error.set("")
                self._error_label.grid_remove()
                self._pending_no_response_after = self.after(900, lambda: self._mark_no_response(generation))
            if response:
                self._cancel_no_response_timer()
                self._pending_request_generation = None
                self.last_response.set(f"Response {evidence}")
            if error:
                self._cancel_no_response_timer()
                self._pending_request_generation = None
                self.last_error.set(f"Error {evidence}")
                self._error_label.grid()
            elif response:
                self.last_error.set("")
                self._error_label.grid_remove()
            self._has_observation = True
            self.activity.set("Observed")
            self.activity_label.configure(style="Status.TLabel")

        def _cancel_no_response_timer(self) -> None:
            pending = self._pending_no_response_after
            if pending is not None:
                try:
                    self.after_cancel(pending)
                except tk.TclError:
                    pass
                self._pending_no_response_after = None

        def _mark_no_response(self, generation: int) -> None:
            self._pending_no_response_after = None
            if generation == self._evidence_generation and self._pending_request_generation == generation:
                self._pending_request_generation = None
                self.last_error.set("Error no response")
                self._error_label.grid()

        def sync_presentation_state(self, state: Mapping[str, Any]) -> None:
            """Render a cached state without replaying worker events.

            The application owns the canonical state while this card is on a
            hidden notebook page.  This method intentionally writes the
            current snapshot directly, scheduling only the one pending timer
            needed to preserve an in-flight request's no-response behavior.
            """
            self._cancel_no_response_timer()
            self.set_runtime_state(state.get("values", {}), online=bool(state.get("online", False)) and bool(state.get("observed", False)))
            self.last_request.set(str(state.get("last_request", "Request —")))
            self.last_response.set(str(state.get("last_response", "Response —")))
            self.last_request_at.set(f"Last request {_format_event_time(state.get('last_request_at'))}")
            self.last_response_at.set(f"Last response {_format_event_time(state.get('last_response_at'))}")
            self.last_error.set(str(state.get("last_error", "")))
            if self.last_error.get():
                self._error_label.grid()
            else:
                self._error_label.grid_remove()
            self._evidence_generation = max(self._evidence_generation, int(state.get("request_generation", 0) or 0))
            pending = state.get("pending_request_generation")
            deadline = state.get("pending_request_deadline")
            self._pending_request_generation = int(pending) if pending is not None else None
            if self._pending_request_generation is not None and deadline is not None:
                remaining_ms = int((float(deadline) - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    self._mark_no_response(self._pending_request_generation)
                else:
                    generation = self._pending_request_generation
                    self._pending_no_response_after = self.after(remaining_ms, lambda: self._mark_no_response(generation))
            result = state.get("result")
            if result:
                self.set_result(str(result), bool(state.get("result_error", False)))

        def set_result(self, text: str, error: bool = False) -> None:
            if error:
                self._cancel_no_response_timer()
                self._pending_request_generation = None
                self.last_error.set(text)
                self._error_label.grid()
            else:
                if str(text).lower().startswith("response"):
                    self._cancel_no_response_timer()
                    self._pending_request_generation = None
                self.last_response.set(text)
                self.last_error.set("")
                self._error_label.grid_remove()

        def mark_stale(self) -> None:
            """Mark a card disconnected without erasing its last observation."""
            if self._has_observation:
                self.activity.set("Stale")
                self.activity_label.configure(style="Warning.TLabel")
            else:
                self.activity.set("Not observed")
                self.activity_label.configure(style="Muted.TLabel")

        def fake_values(self) -> dict[str, Any]:
            values = {key: variable.get().strip() for key, variable in self._fields.items()}
            if self.mode == "slave":
                values["respond_enabled"] = bool(self._respond.get())
            return values

        def apply_device_config(self, device: Any) -> None:
            self.device = device
            self.address.set(str(_value(device, "address", "")))
            serial = _value(device, "serial", None)
            serial_text = _text(_value(serial, "framing", ""))
            if _value(serial, "baudrate", ""):
                serial_text = f"{_value(serial, 'baudrate', '')}/{serial_text}"
            self.serial.set(serial_text)
            self.protocol_text = _protocol_label(_value(device, "protocol", ""))
            self.identity_label.configure(text=f"{self.device_id}  ·  {self.protocol_text}")


    class _CardGridView(BaseView):
        def __init__(self, parent: tk.Misc, devices: Sequence[Any], **kwargs: Any) -> None:
            super().__init__(parent, **kwargs)
            self.cards: dict[str, DeviceCard] = {}
            self._grid = ttk.Frame(self)
            self._grid.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(9, 0))
            self._grid.columnconfigure(0, weight=1)
            self._grid.rowconfigure(0, weight=1)
            self._columns = 4
            self._layout_after: str | None = None
            self._grid.bind("<Configure>", self._responsive_layout)

        def _place_cards(self) -> None:
            for index, card in enumerate(self.cards.values()):
                row, column = divmod(index, self._columns)
                card.grid(row=row, column=column, sticky="nsew", padx=4, pady=4)
                self._grid.columnconfigure(column, weight=1, uniform="device")
                self._grid.rowconfigure(row, weight=1)

        def _responsive_layout(self, event: Any = None) -> None:
            width = int(getattr(event, "width", self.winfo_width()) or 0)
            columns = 3 if width and width < 1020 else 4
            if columns != self._columns:
                self._columns = columns
                if self._layout_after is None:
                    self._layout_after = self.after_idle(self._apply_pending_layout)

        def _apply_pending_layout(self) -> None:
            self._layout_after = None
            self._place_cards()

        def update_device(self, device_id: Any, values: Any, online: bool = True) -> None:
            card = self.cards.get(_text(device_id).lower())
            if card:
                card.set_runtime_state(_value(values, "values", values), online=online)

        def apply_devices(self, devices: Sequence[Any]) -> None:
            for device in devices:
                key = _device_key(device)
                card = self.cards.get(key)
                if card is not None:
                    card.apply_device_config(device)

        def sync_device_state(self, device_id: Any, state: Mapping[str, Any]) -> None:
            card = self.cards.get(_text(device_id).lower())
            if card:
                card.sync_presentation_state(state)

        def set_evidence(self, device_id: Any, evidence: str, **flags: bool) -> None:
            card = self.cards.get(_text(device_id).lower())
            if card:
                card.set_evidence(evidence, **flags)

        def set_session_active(self, active: bool) -> None:
            if not active:
                for card in self.cards.values():
                    card.mark_stale()


    class PassiveView(_CardGridView):
        def __init__(self, parent: tk.Misc, devices: Sequence[Any] = (), **kwargs: Any) -> None:
            super().__init__(parent, devices, **kwargs)
            self._heading("Passive Sniffer", "RX only · twelve-device evidence")
            ttk.Label(self, text="Informational warning: passive capture never transmits application traffic. It listens using one selected UART framing; one UART cannot hear 7E1 and 8N1 simultaneously.", style="Warning.TLabel", wraplength=1100).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
            self.status = tk.StringVar(value="Port closed")
            ttk.Label(self, textvariable=self.status, style="Status.TLabel").grid(row=3, column=0, columnspan=2, sticky="w", pady=(5, 0))
            for device in devices:
                card = DeviceCard(self._grid, device, "passive"); self.cards[card.device_id] = card
            self._place_cards()


    class MasterView(_CardGridView):
        def __init__(self, parent: tk.Misc, devices: Sequence[Any] = (), on_poll: Callable[[str], None] | None = None, on_poll_all: Callable[[], None] | None = None, on_continuous: Callable[[bool], None] | None = None, on_submit: Callable[[], None] | None = None, **kwargs: Any) -> None:
            super().__init__(parent, devices, **kwargs)
            if on_poll is None and on_submit is not None:
                on_poll = lambda _device_id: on_submit()
            self._heading("Active Master", "one target at a time · no raw address form")
            ttk.Label(self, text="Informational warning: disconnect the PLC/controller before opening. Polls are device-selected.", style="Warning.TLabel", wraplength=1100).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(5, 0))
            toolbar = ttk.Frame(self); toolbar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(7, 0))
            self.poll_all_button = ttk.Button(toolbar, text="Poll All Once", command=on_poll_all); self.poll_all_button.pack(side="left")
            self.continuous = tk.BooleanVar(value=False)
            self.continuous_label = tk.StringVar(value="Start Continuous Scan")
            self.continuous_button = ttk.Button(toolbar, textvariable=self.continuous_label, command=self._toggle_continuous)
            self.continuous_button.pack(side="left", padx=(12, 0))
            self.cycle_status = tk.StringVar(value="Last cycle: none")
            ttk.Label(toolbar, textvariable=self.cycle_status, style="Status.TLabel").pack(side="left", padx=(18, 0))
            self.target_status = tk.StringVar(value="Current target: — · failures: —")
            ttk.Label(self, textvariable=self.target_status, style="Muted.TLabel").grid(row=4, column=0, columnspan=2, sticky="w")
            self._on_continuous = on_continuous
            self.status = tk.StringVar(value="Open an Active Master session to poll."); ttk.Label(self, textvariable=self.status, style="Status.TLabel").grid(row=5, column=0, columnspan=2, sticky="w")
            for device in devices:
                card = DeviceCard(self._grid, device, "master", on_poll=on_poll); self.cards[card.device_id] = card
            self._place_cards()

        def _toggle_continuous(self) -> None:
            self.set_continuous(not bool(self.continuous.get()))
            if self._on_continuous:
                self._on_continuous(bool(self.continuous.get()))

        def set_enabled(self, enabled: bool) -> None:
            state = "normal" if enabled else "disabled"; self.poll_all_button.configure(state=state); self.continuous_button.configure(state=state)
            for card in self.cards.values(): card.set_enabled(enabled)

        def set_continuous(self, active: bool) -> None:
            self.continuous.set(bool(active))
            self.continuous_label.set("Stop Continuous Scan" if active else "Start Continuous Scan")

        def set_cycle(self, text: str) -> None:
            text = str(text)
            self.cycle_status.set(text if text.lower().startswith("last cycle") else f"Last cycle: {text}")

        def set_target(self, device_id: Any, status: str = "") -> None:
            target = _text(device_id) or "—"
            self.target_status.set(f"Current target: {target} · {status or 'waiting'}")

        def set_result(self, device_id: Any, text: str, error: bool = False) -> None:
            card = self.cards.get(_text(device_id).lower())
            if card: card.set_result(text, error)

        def operation(self) -> dict[str, str]: return {"operation": "read", "parameter": "M", "value": ""}


    class SlaveView(_CardGridView):
        def __init__(self, parent: tk.Misc, devices: Sequence[Any] = (), on_apply: Callable[[str, Mapping[str, Any]], None] | None = None, **kwargs: Any) -> None:
            super().__init__(parent, devices, **kwargs)
            self._heading("Active Slave", "twelve independent emulators")
            ttk.Label(self, text="Informational warning: disconnect the real sensors before opening. Each device can independently respond.", style="Warning.TLabel", wraplength=1100).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(5, 0))
            self.status = tk.StringVar(value="Open an Active Slave session to apply values."); ttk.Label(self, textvariable=self.status, style="Status.TLabel").grid(row=3, column=0, columnspan=2, sticky="w")
            for device in devices:
                card = DeviceCard(self._grid, device, "slave", on_apply=on_apply); self.cards[card.device_id] = card
            self._place_cards()

        def set_enabled(self, enabled: bool) -> None:
            for card in self.cards.values(): card.set_enabled(enabled)

        def fake_values(self) -> dict[str, dict[str, Any]]: return {key: card.fake_values() for key, card in self.cards.items()}


    class SettingsView(BaseView):
        """Schema-2 settings: shared serial profiles, all addresses, timing, UI."""

        def __init__(self, parent: tk.Misc, devices: Sequence[Any] = (), on_save: Callable[[], None] | None = None, **kwargs: Any) -> None:
            super().__init__(parent, **kwargs); self._heading("Settings", "Saved as schema 2 · SAVE SETTINGS applies changes immediately")
            self.on_save = on_save
            self.save_status = tk.StringVar(value="Saved")
            self._tracking_changes = False
            viewport = ttk.Frame(self); viewport.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(8, 0)); viewport.columnconfigure(0, weight=1); viewport.rowconfigure(0, weight=1)
            canvas = tk.Canvas(viewport, highlightthickness=0, borderwidth=0); scroll = ttk.Scrollbar(viewport, orient="vertical", command=canvas.yview); canvas.configure(yscrollcommand=scroll.set); canvas.grid(row=0, column=0, sticky="nsew"); scroll.grid(row=0, column=1, sticky="ns")
            content = ttk.Frame(canvas); content.columnconfigure(0, weight=1); content.columnconfigure(1, weight=1); window = canvas.create_window((0, 0), window=content, anchor="nw"); content.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all"))); canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
            self._selected_mode = "passive"
            general = ttk.LabelFrame(content, text="General", padding=8); general.grid(row=0, column=0, sticky="new", padx=(0, 5)); general.columnconfigure(1, weight=1)
            self.theme = tk.StringVar(value="darkly"); self.preferred_port = tk.StringVar(); self.timeout = tk.StringVar(value="1.0"); self.raw_capacity = tk.StringVar(value="10000"); self.cycle_delay = tk.StringVar(value="0.250"); self.timeline_window = tk.StringVar(value="30.0"); self.show_hex = tk.BooleanVar(value=True)
            fields = (("Theme", self.theme), ("Preferred COM", self.preferred_port), ("Response timeout (s)", self.timeout), ("Raw capacity", self.raw_capacity), ("Cycle delay (s)", self.cycle_delay), ("Timeline window (s)", self.timeline_window))
            for row, (label, var) in enumerate(fields):
                ttk.Label(general, text=label).grid(row=row, column=0, sticky="w", padx=(0, 7), pady=2); ttk.Entry(general, textvariable=var, width=18).grid(row=row, column=1, sticky="ew", pady=2)
            ttk.Combobox(general, textvariable=self.theme, values=("darkly", "cyborg", "flatly", "light", "system"), state="readonly", width=16).grid(row=0, column=1, sticky="ew", pady=2); ttk.Checkbutton(general, text="Show hexadecimal bytes", variable=self.show_hex).grid(row=6, column=0, columnspan=2, sticky="w", pady=(5, 0))
            capture = ttk.LabelFrame(content, text="Passive capture / slave listen", padding=8); capture.grid(row=0, column=1, sticky="new", padx=(5, 0)); capture.columnconfigure(1, weight=1); self.passive_baudrate = tk.StringVar(value="9600"); self.passive_framing = tk.StringVar(value="7E1"); self.pairing_window = tk.StringVar(value="0.250"); self._serial_fields(capture, (("Baud", self.passive_baudrate), ("Framing", self.passive_framing), ("Pairing window (s)", self.pairing_window)), ("7E1", "8N1", "7O1"))
            partlow = ttk.LabelFrame(content, text="Partlow shared bus · PID 1–11", padding=8); partlow.grid(row=1, column=0, sticky="new", padx=(0, 5), pady=(8, 0)); partlow.columnconfigure(1, weight=1); self.partlow_baudrate = tk.StringVar(value="9600"); self.partlow_framing = tk.StringVar(value="7E1"); self._serial_fields(partlow, (("Baud", self.partlow_baudrate), ("Framing", self.partlow_framing)), ("7E1", "Custom"))
            addr_frame = ttk.Frame(partlow); addr_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0)); self.partlow_addresses: dict[int, tk.StringVar] = {}
            for number in range(1, 12):
                var = tk.StringVar(value=str(number + 1)); self.partlow_addresses[number] = var; cell = ttk.Frame(addr_frame); cell.grid(row=(number - 1) // 6, column=(number - 1) % 6, padx=3, pady=2); ttk.Label(cell, text=f"PID {number} addr").pack(side="left"); ttk.Entry(cell, textvariable=var, width=4).pack(side="left")
            mks = ttk.LabelFrame(content, text="MKS / GP profile", padding=8); mks.grid(row=1, column=1, sticky="new", padx=(5, 0), pady=(8, 0)); mks.columnconfigure(1, weight=1); self.mks_address = tk.StringVar(value="1"); self.mks_baudrate = tk.StringVar(value="9600"); self.mks_framing = tk.StringVar(value="8N1"); self._serial_fields(mks, (("MKS / GP address", self.mks_address), ("Baud", self.mks_baudrate), ("Framing", self.mks_framing)), ("8N1", "7E1", "7O1", "Custom"))
            advanced = ttk.LabelFrame(content, text="Advanced framing (used when Custom is selected)", padding=8); advanced.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0)); self.custom_bytesize = tk.StringVar(value="7"); self.custom_parity = tk.StringVar(value="E"); self.custom_stopbits = tk.StringVar(value="1")
            for col, (label, var, values) in enumerate((("Data bits", self.custom_bytesize, ("7", "8")), ("Parity", self.custom_parity, ("N", "E", "O")), ("Stop bits", self.custom_stopbits, ("1", "2")))):
                ttk.Label(advanced, text=label).grid(row=0, column=col * 2, padx=(0, 5)); ttk.Combobox(advanced, textvariable=var, values=values, state="readonly", width=7).grid(row=0, column=col * 2 + 1, padx=(0, 18))
            self.effective_uart_summary = tk.StringVar()
            ttk.Label(content, textvariable=self.effective_uart_summary, style="Status.TLabel", wraplength=1050).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
            ttk.Label(content, text="Advanced framing applies only to a profile whose framing is set to Custom. Addresses are device properties; Partlow serial is shared by PID 1–11. Passive capture and Active Slave listen use the selected capture UART format; one UART cannot listen to both 7E1 and 8N1 at the same time.", style="Muted.TLabel", wraplength=1050).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
            ttk.Label(content, textvariable=self.save_status, style="Status.TLabel", wraplength=1050).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
            ttk.Button(content, text="SAVE SETTINGS", command=self._save_settings).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
            watched = [self.theme, self.preferred_port, self.timeout, self.raw_capacity, self.cycle_delay, self.timeline_window, self.show_hex, self.passive_baudrate, self.passive_framing, self.pairing_window, self.partlow_baudrate, self.partlow_framing, self.mks_address, self.mks_baudrate, self.mks_framing, self.custom_bytesize, self.custom_parity, self.custom_stopbits, *self.partlow_addresses.values()]
            for variable in watched:
                variable.trace_add("write", self._mark_dirty)
            self._tracking_changes = True
            self._update_effective_uart_summary()

        def _save_settings(self) -> None:
            if self.on_save:
                self.on_save()

        def set_save_status(self, text: str) -> None:
            self.save_status.set(str(text))

        def _mark_dirty(self, *_args: Any) -> None:
            if self._tracking_changes:
                self.save_status.set("Unsaved changes")

        def _serial_fields(self, parent: tk.Misc, fields: Sequence[tuple[str, tk.StringVar]], framings: Sequence[str]) -> None:
            for row, (label, variable) in enumerate(fields):
                ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
                if label == "Framing": ttk.Combobox(parent, textvariable=variable, values=framings, state="readonly", width=16).grid(row=row, column=1, sticky="ew", pady=2)
                else: ttk.Entry(parent, textvariable=variable, width=18).grid(row=row, column=1, sticky="ew", pady=2)

        def set_context(self, mode: str) -> None: self._selected_mode = str(mode); self._update_effective_uart_summary()

        def _update_effective_uart_summary(self, *_args: Any) -> None:
            self.effective_uart_summary.set(f"Mode: {self._selected_mode} · Partlow {self.partlow_baudrate.get()} {self.partlow_framing.get()} · MKS {self.mks_baudrate.get()} {self.mks_framing.get()} · Passive {self.passive_baudrate.get()} {self.passive_framing.get()}")

        @staticmethod
        def _framing(text: str, custom: tuple[str, str, str]) -> tuple[int, str, float]:
            selected = str(text).strip().upper(); selected = "".join(custom) if selected == "CUSTOM" else selected
            if len(selected) != 3: raise ValueError(f"Unsupported serial framing: {text!r}")
            return int(selected[0]), selected[1], float(selected[2])

        def serial_values(self, protocol: str = "partlow_p1160") -> dict[str, Any]:
            selected = self.mks_framing.get() if protocol == "mks_275" else self.partlow_framing.get(); bits, parity, stop = self._framing(selected, (self.custom_bytesize.get(), self.custom_parity.get(), self.custom_stopbits.get())); baud = self.mks_baudrate.get() if protocol == "mks_275" else self.partlow_baudrate.get(); return {"baudrate": int(baud), "bytesize": bits, "parity": parity, "stopbits": stop}

        def set_serial_values(self, protocol: str, values: Mapping[str, Any]) -> None:
            framing = f"{int(values.get('bytesize', 7))}{str(values.get('parity', 'E')).upper()}{int(float(values.get('stopbits', 1)))}"
            if protocol == "passive":
                self.passive_baudrate.set(str(values.get("baudrate", 9600))); self.passive_framing.set(framing)
            elif protocol == "mks_275": self.mks_baudrate.set(str(values.get("baudrate", 9600))); self.mks_framing.set(framing)
            else: self.partlow_baudrate.set(str(values.get("baudrate", 9600))); self.partlow_framing.set(framing)

        def partlow_address_values(self) -> dict[int, int]: return {number: int(variable.get()) for number, variable in self.partlow_addresses.items()}
        def mks_address_value(self) -> int: return int(self.mks_address.get())
        def passive_values(self) -> dict[str, Any]:
            bits, parity, stop = self._framing(self.passive_framing.get(), ("7", "E", "1")); return {"serial": {"baudrate": int(self.passive_baudrate.get()), "bytesize": bits, "parity": parity, "stopbits": stop}, "response_pairing_window_s": float(self.pairing_window.get())}
        def ui_values(self) -> dict[str, Any]: return {"theme": self.theme.get(), "show_hex": bool(self.show_hex.get()), "timeline_window_s": float(self.timeline_window.get())}

        def set_device_addresses(self, devices: Sequence[Any]) -> None:
            for device in devices:
                key = _device_key(device)
                if key == "gp": self.mks_address.set(str(_value(device, "address", 1)))
                elif key.startswith("pid_"):
                    try: self.partlow_addresses[int(key.split("_")[-1])].set(str(_value(device, "address", 1)))
                    except (KeyError, ValueError): pass


    class RawTrafficView(BaseView):
        def __init__(self, parent: tk.Misc, **kwargs: Any) -> None:
            super().__init__(parent, **kwargs); self._heading("Raw Traffic", "bytes are retained as received"); frame = ttk.Frame(self); frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0)); frame.columnconfigure(0, weight=1); frame.rowconfigure(0, weight=1)
            columns = ("seq", "time", "direction", "ascii", "hex", "flags"); self.table = ttk.Treeview(frame, columns=columns, show="headings", height=14, style="Traffic.Treeview"); self._all_columns = columns; self.show_hex = True
            self._display_limit = 1000
            self._item_ids: deque[str] = deque()
            self._last_rendered_seq = 0
            for column, heading, width in (("seq", "#", 60), ("time", "Time", 105), ("direction", "Dir", 55), ("ascii", "ASCII", 250), ("hex", "Hex", 300), ("flags", "Flags", 170)):
                self.table.heading(column, text=heading); self.table.column(column, width=width, anchor="w", stretch=column in {"ascii", "hex"})
            y = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview); x = ttk.Scrollbar(frame, orient="horizontal", command=self.table.xview); self.table.configure(yscrollcommand=y.set, xscrollcommand=x.set); self.table.grid(row=0, column=0, sticky="nsew"); y.grid(row=0, column=1, sticky="ns"); x.grid(row=1, column=0, sticky="ew"); self.notice = tk.StringVar(value="No traffic captured"); ttk.Label(self, textvariable=self.notice, style="Muted.TLabel").grid(row=2, column=0, sticky="w")

        def set_show_hex(self, enabled: bool) -> None: self.show_hex = bool(enabled); self.table["displaycolumns"] = self._all_columns if self.show_hex else tuple(c for c in self._all_columns if c != "hex")
        def has_rows(self) -> bool: return bool(self._item_ids)
        def add_records(self, records: Iterable[Any], dropped_before: Any = None) -> int:
            count = 0
            for record in records:
                try:
                    seq = int(_value(record, "seq", 0) or 0)
                except (TypeError, ValueError):
                    seq = 0
                if seq and seq <= self._last_rendered_seq:
                    continue
                direction = _text(_value(record, "direction", "")).upper(); flags = _value(record, "flags", ()); flags = flags if isinstance(flags, str) else ", ".join(sorted(_text(flag) for flag in flags)); item = self.table.insert("", "end", values=(_value(record, "seq", ""), _when(record), direction, _ascii(_value(record, "data")), _hex(_value(record, "data")), flags), tags=(direction,) if direction in {"RX", "TX"} else ()); self._item_ids.append(item); self._last_rendered_seq = max(self._last_rendered_seq, seq); count += 1
            excess = len(self._item_ids) - self._display_limit
            if excess > 0:
                old_items = [self._item_ids.popleft() for _ in range(excess)]
                self.table.delete(*old_items)
            if dropped_before not in (None, "", 0): self.notice.set(f"Capture gap: records before sequence {dropped_before} were dropped")
            elif count: self.notice.set(f"{len(self._item_ids)} records shown")
            return count
        def clear(self) -> None:
            if self._item_ids:
                self.table.delete(*self._item_ids)
            self._item_ids.clear(); self._last_rendered_seq = 0; self.notice.set("No traffic captured")


    class TimelineView(BaseView):
        def __init__(self, parent: tk.Misc, **kwargs: Any) -> None:
            super().__init__(parent, **kwargs); self._heading("Bus Timeline", "decoded frames and worker events"); frame = ttk.Frame(self); frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0)); frame.columnconfigure(0, weight=1); frame.rowconfigure(0, weight=1)
            columns = ("id", "time", "direction", "frame", "decoded", "status"); self.table = ttk.Treeview(frame, columns=columns, show="headings", height=14); self.window_s = 30.0; self._item_times: dict[str, int] = {}; self._item_ids: deque[str] = deque(); self._display_limit = 500; self._last_rendered_frame_id = 0; self._timeline_origin_ns: int | None = None
            for column, heading, width in (("id", "#", 60), ("time", "Time", 105), ("direction", "Dir", 55), ("frame", "Frame", 270), ("decoded", "Decoded", 270), ("status", "Status", 180)):
                self.table.heading(column, text=heading); self.table.column(column, width=width, anchor="w", stretch=column in {"frame", "decoded"})
            y = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview); self.table.configure(yscrollcommand=y.set); self.table.grid(row=0, column=0, sticky="nsew"); y.grid(row=0, column=1, sticky="ns"); self.notice = tk.StringVar(value="No decoded frames"); ttk.Label(self, textvariable=self.notice, style="Muted.TLabel").grid(row=2, column=0, sticky="w")
        def set_window_s(self, value: float) -> None:
            try: parsed = float(value)
            except (TypeError, ValueError): return
            if parsed > 0: self.window_s = parsed
        def has_rows(self) -> bool: return bool(self._item_ids)
        def add_frames(self, frames: Iterable[Any]) -> int:
            count = 0
            for frame in frames:
                try:
                    frame_id = int(_value(frame, "frame_id", 0) or 0)
                except (TypeError, ValueError):
                    frame_id = 0
                if frame_id and frame_id <= self._last_rendered_frame_id:
                    continue
                timestamp = _value(frame, "monotonic_ns", None); decoded = _value(frame, "decoded", None); error = _value(frame, "error", None)
                if isinstance(timestamp, int):
                    if self._timeline_origin_ns is None: self._timeline_origin_ns = timestamp
                    time_text = f"+{(timestamp - self._timeline_origin_ns) / 1_000_000:.1f} ms"
                else: time_text = _when(frame)
                item = self.table.insert("", "end", values=(_value(frame, "frame_id", ""), time_text, _text(_value(frame, "direction", "")).upper(), _ascii(_value(frame, "raw_frame", b"")), _decoded_summary(decoded), f"error: {_text(error)}" if error else "ok")); self._item_ids.append(item); self._last_rendered_frame_id = max(self._last_rendered_frame_id, frame_id)
                if isinstance(timestamp, int): self._item_times[item] = timestamp
                count += 1
            excess = len(self._item_ids) - self._display_limit
            if excess > 0:
                old_items = [self._item_ids.popleft() for _ in range(excess)]
                self.table.delete(*old_items)
                for item in old_items:
                    self._item_times.pop(item, None)
            if count: self.notice.set(f"{len(self._item_ids)} frames shown")
            return count
        def clear(self) -> None:
            if self._item_ids:
                self.table.delete(*self._item_ids)
            self._item_ids.clear(); self._item_times.clear(); self._last_rendered_frame_id = 0; self._timeline_origin_ns = None; self.notice.set("No decoded frames")


__all__ = ["BaseView", "DeviceCard", "PassiveView", "MasterView", "SlaveView", "SettingsView", "RawTrafficView", "TimelineView"]
