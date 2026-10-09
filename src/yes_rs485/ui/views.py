"""Small, deliberately boring Tkinter views.

Views do not open serial ports or implement protocol rules.  They expose a
few callbacks to :class:`yes_rs485.ui.app.YesRs485App`, and accept plain
records for display.  Keeping these widgets independent also makes the raw
traffic and timeline panes useful in smoke tests without a live adapter.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

try:  # Importing the package should still work in minimal/headless installs.
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # pragma: no cover - exercised only on Python builds sans Tk
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]


def _value(record: Any, name: str, default: Any = "") -> Any:
    """Read an attribute or mapping key from a worker event."""

    if record is None:
        return default
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "value"):
        value = value.value
    return str(value)


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
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    try:
        return bytes(data).decode("ascii", "backslashreplace")
    except (TypeError, ValueError):
        return _text(data)


def _when(record: Any) -> str:
    value = _value(record, "wall_time_utc", _value(record, "timestamp", ""))
    if isinstance(value, datetime):
        return value.astimezone().strftime("%H:%M:%S.%f")[:-3]
    monotonic = _value(record, "monotonic_ns", "")
    if monotonic != "":
        return f"{monotonic} ns"
    return _text(value)


def _decoded_summary(decoded: Any) -> str:
    """Render the useful fields of a decoded frame without dataclass repr noise."""

    if decoded is None:
        return ""
    parts: list[str] = []
    kind = _text(_value(decoded, "kind", "")).strip()
    if kind:
        parts.append(kind)
    address = _value(decoded, "address", None)
    if address not in (None, ""):
        parts.append(f"addr={_text(address)}")

    metadata = _value(decoded, "metadata", {})
    command = _value(decoded, "command", "")
    if not command and isinstance(metadata, Mapping):
        command = metadata.get("command", "")
    for name, label in (
        ("parameter", "param"),
        ("status", "status"),
        ("prefix", "prefix"),
        ("command", "cmd"),
        ("data", "data"),
        ("payload", "payload"),
    ):
        value = command if name == "command" else _value(decoded, name, "")
        value_text = _text(value).strip()
        if value_text and not (name == "payload" and value_text == _text(_value(decoded, "data", "")).strip()):
            parts.append(f"{label}={value_text}")
    if parts:
        return " ".join(parts)[:240]
    return "Decoded frame"


if tk is None:  # pragma: no cover - allows import on a Python without Tk

    class _NoTk:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("Tkinter is not available in this Python installation")

    BaseView = _NoTk
    PassiveView = MasterView = SlaveView = SettingsView = RawTrafficView = TimelineView = _NoTk

else:

    class BaseView(ttk.Frame):
        """Common frame helper used by mode and diagnostic views."""

        def __init__(self, parent: tk.Misc, **kwargs: Any) -> None:
            super().__init__(parent, padding=10, **kwargs)
            self.columnconfigure(0, weight=1)
            self.rowconfigure(1, weight=1)

        def _heading(self, title: str, subtitle: str = "") -> None:
            ttk.Label(self, text=title, style="Title.TLabel").grid(
                row=0, column=0, sticky="w", pady=(0, 2)
            )
            if subtitle:
                ttk.Label(self, text=subtitle, style="Muted.TLabel").grid(
                    row=0, column=1, sticky="e", pady=(0, 2)
                )


    class PassiveView(BaseView):
        """Passive sniffer landing page."""

        def __init__(self, parent: tk.Misc, **kwargs: Any) -> None:
            super().__init__(parent, **kwargs)
            self._heading("Passive Sniffer", "RX only")
            body = ttk.Frame(self)
            body.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
            body.columnconfigure(0, weight=1)
            ttk.Label(
                body,
                text=(
                    "Observe bytes and decoded frames without sending application traffic.\n"
                    "The selected protocol controls framing and decoding; it does not enable writes."
                ),
                justify="left",
            ).grid(row=0, column=0, sticky="nw")
            ttk.Label(
                body,
                text="No traffic captured yet — open a passive session to begin listening.",
                style="Muted.TLabel",
            ).grid(row=1, column=0, sticky="w", pady=(10, 0))
            self.status = tk.StringVar(value="Port closed")
            ttk.Label(body, textvariable=self.status, style="Status.TLabel").grid(
                row=2, column=0, sticky="w", pady=(12, 0)
            )

        def set_status(self, text: str) -> None:
            self.status.set(text)


    class MasterView(BaseView):
        """Controls for an explicitly armed active master session."""

        def __init__(
            self,
            parent: tk.Misc,
            on_submit: Callable[[], None] | None = None,
            **kwargs: Any,
        ) -> None:
            super().__init__(parent, **kwargs)
            self.rowconfigure(1, weight=0)
            self._heading("Active Master", "Requires PLC/controller disconnected")
            self.warning = tk.StringVar(
                value="WARNING: requires the PLC/controller to be physically disconnected before opening."
            )
            ttk.Label(self, textvariable=self.warning, style="Warning.TLabel", wraplength=720).grid(
                row=1, column=0, columnspan=2, sticky="ew", pady=(4, 10)
            )

            form = ttk.LabelFrame(self, text="One request at a time", padding=8)
            form.grid(row=2, column=0, columnspan=2, sticky="new")
            for column in (1, 3):
                form.columnconfigure(column, weight=1)
            self.master_command = tk.StringVar(value="read")
            self.parameter = tk.StringVar(value="M")
            self.value = tk.StringVar()
            self.address = tk.StringVar(value="1")
            ttk.Label(form, text="Operation").grid(row=0, column=0, sticky="w", padx=(0, 6))
            self.operation_combo = ttk.Combobox(
                form,
                textvariable=self.master_command,
                values=("read", "write", "raw"),
                state="readonly",
                width=12,
            )
            self.operation_combo.grid(row=0, column=1, sticky="ew", padx=(0, 12))
            ttk.Label(form, text="Address").grid(row=0, column=2, sticky="w", padx=(0, 6))
            ttk.Entry(form, textvariable=self.address, width=8).grid(row=0, column=3, sticky="ew")
            ttk.Label(form, text="Parameter").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(8, 0))
            ttk.Entry(form, textvariable=self.parameter, width=12).grid(
                row=1, column=1, sticky="ew", padx=(0, 12), pady=(8, 0)
            )
            ttk.Label(form, text="Value / payload").grid(row=1, column=2, sticky="w", padx=(0, 6), pady=(8, 0))
            ttk.Entry(form, textvariable=self.value).grid(row=1, column=3, sticky="ew", pady=(8, 0))
            self.submit_button = ttk.Button(form, text="Send request", command=on_submit)
            self.submit_button.grid(row=2, column=0, columnspan=4, sticky="e", pady=(12, 0))
            self.set_enabled(False)

            self.status = tk.StringVar(value="Arm the PLC-disconnected safety check, then open the port.")
            ttk.Label(self, textvariable=self.status, style="Status.TLabel", wraplength=720).grid(
                row=3, column=0, columnspan=2, sticky="w", pady=(12, 0)
            )

        def set_status(self, text: str) -> None:
            self.status.set(text)

        def set_enabled(self, enabled: bool) -> None:
            self.submit_button.configure(state="normal" if enabled else "disabled")

        def operation(self) -> dict[str, str]:
            return {
                "operation": self.master_command.get().strip(),
                "parameter": self.parameter.get().strip(),
                "value": self.value.get().strip(),
                "address": self.address.get().strip(),
            }


    class SlaveView(BaseView):
        """Controls for a worker-owned, request-triggered slave emulator."""

        def __init__(
            self,
            parent: tk.Misc,
            on_apply: Callable[[], None] | None = None,
            **kwargs: Any,
        ) -> None:
            super().__init__(parent, **kwargs)
            self.rowconfigure(1, weight=0)
            self._heading("Active Slave", "Requires real sensors disconnected")
            ttk.Label(
                self,
                text="WARNING: requires real sensors to be disconnected before opening. Replies are generated only for valid addressed requests.",
                style="Warning.TLabel",
                wraplength=720,
            ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 10))
            form = ttk.LabelFrame(self, text="Emulated profile", padding=8)
            form.grid(row=2, column=0, columnspan=2, sticky="new")
            form.columnconfigure(1, weight=1)
            self.slave_address = tk.StringVar(value="1")
            self.process_value = tk.StringVar(value="0")
            self.setpoint = tk.StringVar(value="0")
            self.pressure = tk.StringVar(value="0.00E+00")
            self.units = tk.StringVar(value="Torr")
            self._field_labels: dict[str, Any] = {}
            self._field_entries: dict[str, Any] = {}
            fields = (
                ("address", "Address", self.slave_address),
                ("process_value", "Process value", self.process_value),
                ("setpoint", "Setpoint", self.setpoint),
                ("pressure", "Pressure", self.pressure),
                ("units", "Units", self.units),
            )
            for row, (name, label, variable) in enumerate(fields):
                label_widget = ttk.Label(form, text=label)
                label_widget.grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
                self._field_labels[name] = label_widget
                entry = ttk.Entry(form, textvariable=variable, width=26)
                entry.grid(row=row, column=1, sticky="ew", pady=3)
                self._field_entries[name] = entry
            self.protocol_hint = tk.StringVar()
            ttk.Label(form, textvariable=self.protocol_hint, style="Muted.TLabel", wraplength=560).grid(
                row=len(fields), column=0, columnspan=2, sticky="w", pady=(6, 0)
            )
            self.apply_button = ttk.Button(form, text="Apply profile", command=on_apply)
            self.apply_button.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="e", pady=(10, 0))
            self.status = tk.StringVar(value="Arm the sensors-disconnected safety check before opening.")
            ttk.Label(self, textvariable=self.status, style="Status.TLabel", wraplength=720).grid(
                row=3, column=0, columnspan=2, sticky="w", pady=(12, 0)
            )
            self.set_protocol("partlow_p1160")

        def set_status(self, text: str) -> None:
            self.status.set(text)

        def set_protocol(self, protocol: str) -> None:
            """Show which profile fields apply to the selected protocol."""

            if protocol == "mks_275":
                self.protocol_hint.set(
                    "MKS 275: Pressure is used for RD; process value and setpoint are not used by this profile form."
                )
                self._field_labels["pressure"].configure(text="Pressure (MKS RD)")
                self._field_labels["process_value"].configure(text="Process value (Partlow only)")
                self._field_labels["setpoint"].configure(text="Setpoint (Partlow only)")
                self._field_entries["pressure"].configure(state="normal")
                self._field_entries["process_value"].configure(state="disabled")
                self._field_entries["setpoint"].configure(state="disabled")
            else:
                self.protocol_hint.set(
                    "Partlow P1160: process value and setpoint are the active profile fields; pressure is not used."
                )
                self._field_labels["pressure"].configure(text="Pressure (MKS only)")
                self._field_labels["process_value"].configure(text="Process value (Partlow)")
                self._field_labels["setpoint"].configure(text="Setpoint (Partlow)")
                self._field_entries["pressure"].configure(state="disabled")
                self._field_entries["process_value"].configure(state="normal")
                self._field_entries["setpoint"].configure(state="normal")

        def profile(self) -> dict[str, str]:
            return {
                "address": self.slave_address.get().strip(),
                "process_value": self.process_value.get().strip(),
                "setpoint": self.setpoint.get().strip(),
                "pressure": self.pressure.get().strip(),
                "units": self.units.get().strip(),
            }


    class SettingsView(BaseView):
        """Visible serial and application settings.

        Device communication settings intentionally live in separate cards:
        Partlow has one shared bus profile and eleven small address controls,
        while the MKS/GP profile has its own address, baud, and framing.  The
        passive capture UART is presented separately so an engineer never
        mistakes the capture strategy for a device configuration.
        """

        def __init__(self, parent: tk.Misc, **kwargs: Any) -> None:
            super().__init__(parent, **kwargs)
            self._heading("Settings", "Auto-saved; changes apply on next open")
            self.columnconfigure(0, weight=1)
            self.rowconfigure(1, weight=1)
            self._selected_mode = "passive"
            self._selected_protocol = "partlow_p1160"

            # The settings surface contains several intentionally separate
            # cards.  Keep the title visible while allowing the cards to grow
            # beyond a compact notebook viewport (especially at 100% Windows
            # scaling and in the dark screenshot review size).
            viewport = ttk.Frame(self)
            viewport.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
            viewport.columnconfigure(0, weight=1)
            viewport.rowconfigure(0, weight=1)
            self._settings_canvas = tk.Canvas(viewport, highlightthickness=0, borderwidth=0)
            self._settings_scrollbar = ttk.Scrollbar(
                viewport, orient="vertical", command=self._settings_canvas.yview
            )
            self._settings_canvas.configure(yscrollcommand=self._settings_scrollbar.set)
            self._settings_canvas.grid(row=0, column=0, sticky="nsew")
            self._settings_scrollbar.grid(row=0, column=1, sticky="ns")
            self._settings_inner = ttk.Frame(self._settings_canvas)
            self._settings_inner.columnconfigure(0, weight=1)
            self._settings_inner.columnconfigure(1, weight=1)
            self._settings_window = self._settings_canvas.create_window(
                (0, 0), window=self._settings_inner, anchor="nw"
            )
            self._settings_inner.bind(
                "<Configure>",
                lambda _event: self._settings_canvas.configure(
                    scrollregion=self._settings_canvas.bbox("all")
                ),
            )
            self._settings_canvas.bind(
                "<Configure>",
                lambda event: self._settings_canvas.itemconfigure(
                    self._settings_window, width=event.width
                ),
            )
            self._settings_canvas.bind("<Enter>", self._bind_settings_wheel)
            self._settings_canvas.bind("<Leave>", self._unbind_settings_wheel)
            content = self._settings_inner

            # Keep the UART that will actually be used on the next open visible
            # while the engineer edits the individual profile cards below.  The
            # selected mode/device is supplied by the composition root so this
            # remains a view-only presentation concern.
            effective = ttk.LabelFrame(content, text="Effective UART on next open", padding=(8, 4))
            effective.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
            effective.columnconfigure(0, weight=1)
            self.effective_uart_summary = tk.StringVar()
            ttk.Label(
                effective,
                textvariable=self.effective_uart_summary,
                style="Effective.TLabel",
                justify="left",
                anchor="w",
                wraplength=1100,
            ).grid(row=0, column=0, sticky="ew")

            # General preferences -------------------------------------------------
            general = ttk.LabelFrame(content, text="General", padding=10)
            general.grid(row=1, column=0, sticky="new", padx=(0, 6), pady=(0, 0))
            general.columnconfigure(1, weight=1)
            self.theme = tk.StringVar(value="darkly")
            self.preferred_port = tk.StringVar()
            ttk.Label(general, text="Theme").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
            self.theme_combo = ttk.Combobox(
                general,
                textvariable=self.theme,
                values=("darkly", "cyborg", "flatly", "light", "system"),
                state="readonly",
                width=18,
            )
            self.theme_combo.grid(row=0, column=1, sticky="ew", pady=3)
            ttk.Label(general, text="Preferred COM port").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=3)
            ttk.Entry(general, textvariable=self.preferred_port, width=20).grid(
                row=1, column=1, sticky="ew", pady=3
            )

            # Passive capture ------------------------------------------------------
            capture = ttk.LabelFrame(content, text="Passive Capture (bus observation)", padding=10)
            capture.grid(row=1, column=1, sticky="new", padx=(6, 0), pady=(0, 0))
            capture.columnconfigure(1, weight=1)
            self.passive_baudrate = tk.StringVar(value="9600")
            self.passive_strategy = tk.StringVar(value="mixed_raw")
            self.pairing_window = tk.StringVar(value="0.250")
            ttk.Label(capture, text="Bus baud").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
            ttk.Combobox(
                capture,
                textvariable=self.passive_baudrate,
                values=("1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"),
                width=18,
            ).grid(row=0, column=1, sticky="ew", pady=3)
            ttk.Label(capture, text="Capture strategy").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=3)
            ttk.Combobox(
                capture,
                textvariable=self.passive_strategy,
                values=("mixed_raw",),
                state="readonly",
                width=18,
            ).grid(row=1, column=1, sticky="ew", pady=3)
            ttk.Label(capture, text="Response pairing window (s)").grid(
                row=2, column=0, sticky="w", padx=(0, 10), pady=3
            )
            ttk.Entry(capture, textvariable=self.pairing_window, width=20).grid(
                row=2, column=1, sticky="ew", pady=3
            )
            ttk.Label(
                capture,
                text="Capture UART settings are independent from device framing.",
                style="Muted.TLabel",
                wraplength=330,
            ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

            # Partlow shared bus ---------------------------------------------------
            partlow = ttk.LabelFrame(content, text="PARTLOW BUS (shared by PID 1–11)", padding=10)
            partlow.grid(row=2, column=0, sticky="new", padx=(0, 6), pady=(10, 0))
            partlow.columnconfigure(1, weight=1)
            self.partlow_baudrate = tk.StringVar(value="9600")
            self.partlow_framing = tk.StringVar(value="7E1")
            self.partlow_custom = tk.BooleanVar(value=False)
            ttk.Label(partlow, text="Baud").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
            self.partlow_baud_combo = ttk.Combobox(
                partlow,
                textvariable=self.partlow_baudrate,
                values=("1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"),
                width=18,
            )
            self.partlow_baud_combo.grid(row=0, column=1, sticky="ew", pady=3)
            ttk.Label(partlow, text="Framing").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=3)
            self.partlow_framing_combo = ttk.Combobox(
                partlow,
                textvariable=self.partlow_framing,
                values=("7E1", "Custom"),
                state="readonly",
                width=18,
            )
            self.partlow_framing_combo.grid(row=1, column=1, sticky="ew", pady=3)
            self.partlow_warning = tk.StringVar()
            ttk.Label(partlow, textvariable=self.partlow_warning, style="Warning.TLabel", wraplength=360).grid(
                row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0)
            )
            pid_frame = ttk.Frame(partlow)
            pid_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
            for column in range(6):
                pid_frame.columnconfigure(column, weight=1)
            self.partlow_addresses: dict[int, tk.StringVar] = {}
            for pid in range(1, 12):
                row, column = divmod(pid - 1, 6)
                cell = ttk.Frame(pid_frame)
                cell.grid(row=row, column=column, sticky="ew", padx=3, pady=2)
                ttk.Label(cell, text=f"PID {pid}").pack(side="left")
                variable = tk.StringVar(value=str(pid + 1))
                self.partlow_addresses[pid] = variable
                ttk.Entry(cell, textvariable=variable, width=5).pack(side="right")

            # MKS profile ----------------------------------------------------------
            mks = ttk.LabelFrame(content, text="MKS / GP GAUGE", padding=10)
            mks.grid(row=2, column=1, sticky="new", padx=(6, 0), pady=(10, 0))
            mks.columnconfigure(1, weight=1)
            self.mks_address = tk.StringVar(value="1")
            self.mks_baudrate = tk.StringVar(value="9600")
            self.mks_framing = tk.StringVar(value="8N1")
            ttk.Label(mks, text="Address").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
            ttk.Entry(mks, textvariable=self.mks_address, width=20).grid(row=0, column=1, sticky="ew", pady=3)
            ttk.Label(mks, text="Baud").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=3)
            ttk.Combobox(
                mks,
                textvariable=self.mks_baudrate,
                values=("300", "600", "1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"),
                width=18,
            ).grid(row=1, column=1, sticky="ew", pady=3)
            ttk.Label(mks, text="Framing").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=3)
            self.mks_framing_combo = ttk.Combobox(
                mks,
                textvariable=self.mks_framing,
                values=("8N1", "7E1", "7O1", "Custom"),
                state="readonly",
                width=18,
            )
            self.mks_framing_combo.grid(row=2, column=1, sticky="ew", pady=3)
            self.mks_warning = tk.StringVar()
            ttk.Label(mks, textvariable=self.mks_warning, style="Warning.TLabel", wraplength=360).grid(
                row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0)
            )

            # Advanced custom framing ---------------------------------------------
            advanced = ttk.LabelFrame(content, text="Advanced / Custom framing", padding=10)
            advanced.grid(row=3, column=0, columnspan=2, sticky="new", pady=(10, 0))
            for column in (1, 3, 5):
                advanced.columnconfigure(column, weight=1)
            self.custom_bytesize = tk.StringVar(value="7")
            self.custom_parity = tk.StringVar(value="E")
            self.custom_stopbits = tk.StringVar(value="1")
            ttk.Label(advanced, text="Data bits").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
            ttk.Combobox(advanced, textvariable=self.custom_bytesize, values=("7", "8"), state="readonly", width=8).grid(
                row=0, column=1, sticky="ew", padx=(0, 16), pady=3
            )
            ttk.Label(advanced, text="Parity").grid(row=0, column=2, sticky="w", padx=(0, 6), pady=3)
            ttk.Combobox(advanced, textvariable=self.custom_parity, values=("N", "E", "O"), state="readonly", width=8).grid(
                row=0, column=3, sticky="ew", padx=(0, 16), pady=3
            )
            ttk.Label(advanced, text="Stop bits").grid(row=0, column=4, sticky="w", padx=(0, 6), pady=3)
            ttk.Combobox(advanced, textvariable=self.custom_stopbits, values=("1", "2"), state="readonly", width=8).grid(
                row=0, column=5, sticky="ew", pady=3
            )
            self.custom_warning = tk.StringVar(
                value=(
                    "Advanced values apply only when a device card above is set to Custom; "
                    "otherwise they are ignored."
                )
            )
            ttk.Label(advanced, textvariable=self.custom_warning, style="Warning.TLabel", wraplength=900).grid(
                row=1, column=0, columnspan=6, sticky="ew", pady=(6, 0)
            )

            # General capture/display controls ------------------------------------
            general_settings = ttk.LabelFrame(content, text="Application behavior", padding=10)
            general_settings.grid(row=4, column=0, columnspan=2, sticky="new", pady=(10, 0))
            general_settings.columnconfigure(1, weight=1)
            self.timeout = tk.StringVar(value="1.0")
            self.raw_capacity = tk.StringVar(value="5000")
            self.timeline_window = tk.StringVar(value="30.0")
            self.show_hex = tk.BooleanVar(value=True)
            fields: tuple[tuple[str, tk.StringVar], ...] = (
                ("Response timeout (s)", self.timeout),
                ("Raw record capacity", self.raw_capacity),
                ("Timeline window (s)", self.timeline_window),
            )
            for row, (label, variable) in enumerate(fields):
                ttk.Label(general_settings, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
                ttk.Entry(general_settings, textvariable=variable, width=20).grid(row=row, column=1, sticky="ew", pady=3)
            ttk.Checkbutton(general_settings, text="Show hexadecimal bytes", variable=self.show_hex).grid(
                row=len(fields), column=0, columnspan=2, sticky="w", pady=(6, 0)
            )
            ttk.Label(
                content,
                text="Settings are saved automatically when the application closes and apply when the next session opens. "
                "Flow control is disabled. Adapter direction control, echo, termination, isolation, and polarity are unverified.",
                style="Muted.TLabel",
                justify="left",
                wraplength=980,
            ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 0))

            # Keep old attribute names available to small integrations/tests. The
            # app always calls ``serial_values(protocol)`` for new sessions.
            self.baudrate = self.partlow_baudrate
            self.bytesize = self.custom_bytesize
            self.parity = self.custom_parity
            self.stopbits = self.custom_stopbits
            for variable in (
                self.partlow_baudrate,
                self.partlow_framing,
                self.mks_baudrate,
                self.mks_framing,
                self.passive_baudrate,
                self.passive_strategy,
                self.custom_bytesize,
                self.custom_parity,
                self.custom_stopbits,
            ):
                variable.trace_add("write", self._update_framing_warnings)
                variable.trace_add("write", self._update_effective_uart_summary)
            self._update_framing_warnings()
            self._update_effective_uart_summary()

        def _bind_settings_wheel(self, _event: Any = None) -> None:
            self._settings_canvas.bind_all("<MouseWheel>", self._on_settings_wheel)
            self._settings_canvas.bind_all("<Button-4>", self._on_settings_wheel)
            self._settings_canvas.bind_all("<Button-5>", self._on_settings_wheel)

        def _unbind_settings_wheel(self, _event: Any = None) -> None:
            self._settings_canvas.unbind_all("<MouseWheel>")
            self._settings_canvas.unbind_all("<Button-4>")
            self._settings_canvas.unbind_all("<Button-5>")

        def _on_settings_wheel(self, event: Any) -> str:
            if getattr(event, "num", None) == 4:
                amount = -1
            elif getattr(event, "num", None) == 5:
                amount = 1
            else:
                delta = int(getattr(event, "delta", 0))
                amount = -1 if delta > 0 else 1
                if delta == 0:
                    return "break"
            self._settings_canvas.yview_scroll(amount, "units")
            return "break"

        @staticmethod
        def _parse_framing(value: str) -> tuple[int, str, float]:
            text = str(value).strip().upper()
            if len(text) != 3 or text[0] not in "78" or text[1] not in "NEO" or text[2] not in "12":
                raise ValueError(f"Unsupported serial framing: {value!r}")
            return int(text[0]), text[1], float(text[2])

        def _framing(self, selected: str) -> tuple[int, str, float]:
            if selected == "Custom":
                return int(self.custom_bytesize.get()), self.custom_parity.get(), float(self.custom_stopbits.get())
            return self._parse_framing(selected)

        def _update_framing_warnings(self, *_args: Any) -> None:
            partlow = self.partlow_framing.get()
            mks = self.mks_framing.get()
            custom = f"{self.custom_bytesize.get()}{self.custom_parity.get()}{self.custom_stopbits.get()}"
            self.partlow_warning.set(
                "Custom framing — not a documented device configuration."
                if partlow == "Custom" and custom != "7E1" else ""
            )
            self.mks_warning.set(
                "Custom framing — not a documented device configuration."
                if mks == "Custom" and custom not in {"8N1", "7E1", "7O1"} else ""
            )
            selected_custom = partlow == "Custom" or mks == "Custom"
            if selected_custom and ((partlow == "Custom" and custom != "7E1") or (mks == "Custom" and custom not in {"8N1", "7E1", "7O1"})):
                self.custom_warning.set(
                    f"Custom framing {custom} — not a documented device configuration. "
                    "Advanced values apply only to the device card set to Custom; "
                    "this changes this application's UART only."
                )
            else:
                self.custom_warning.set(
                    f"Advanced values ({custom}) apply only when a device card is set to Custom; "
                    "otherwise they are ignored. Device configuration is never written."
                )

        @staticmethod
        def _summary_framing(selected: str, custom: str) -> str:
            """Return the actual framing while retaining a Custom marker."""

            selected = str(selected).strip() or "?"
            return f"{custom} (Custom)" if selected == "Custom" else selected

        def set_context(self, mode: str, protocol: str) -> None:
            """Set the app's current mode/device for the effective-UART summary."""

            self._selected_mode = str(mode or "passive").strip().lower()
            self._selected_protocol = str(protocol or "partlow_p1160").strip().lower()
            self._update_effective_uart_summary()

        def _update_effective_uart_summary(self, *_args: Any) -> None:
            custom = f"{self.custom_bytesize.get()}{self.custom_parity.get()}{self.custom_stopbits.get()}"
            partlow = f"{self.partlow_baudrate.get().strip() or '?'} {self._summary_framing(self.partlow_framing.get(), custom)}"
            mks = f"{self.mks_baudrate.get().strip() or '?'} {self._summary_framing(self.mks_framing.get(), custom)}"
            passive_baud = self.passive_baudrate.get().strip() or "?"
            strategy = self.passive_strategy.get().strip() or "mixed_raw"
            passive = f"{passive_baud} baud · {strategy} (capture framing unverified)"
            mode_labels = {
                "passive": "Passive capture",
                "master": "Active master",
                "slave": "Active slave",
            }
            mode_label = mode_labels.get(self._selected_mode, "Selected session")
            if self._selected_mode == "passive":
                selected = f"{mode_label} → {passive}"
                profiles = f"Partlow shared bus: {partlow}  |  MKS/GP profile: {mks}"
            elif self._selected_protocol == "mks_275":
                selected = f"{mode_label} · MKS/GP profile → {mks}"
                profiles = f"Partlow shared bus: {partlow}  |  Passive capture: {passive}"
            else:
                selected = f"{mode_label} · Partlow shared bus → {partlow}"
                profiles = f"MKS/GP profile: {mks}  |  Passive capture: {passive}"
            self.effective_uart_summary.set(f"Selected: {selected}  |  {profiles}")

        def serial_values(self, protocol: str | None = None) -> dict[str, Any]:
            protocol = protocol or "partlow_p1160"
            if protocol == "mks_275":
                baud = self.mks_baudrate.get()
                framing = self.mks_framing.get()
            else:
                baud = self.partlow_baudrate.get()
                framing = self.partlow_framing.get()
            bytesize, parity, stopbits = self._framing(framing)
            return {
                "baudrate": int(baud),
                "bytesize": bytesize,
                "parity": parity,
                "stopbits": stopbits,
            }

        def set_serial_values(self, protocol: str, values: Mapping[str, Any]) -> None:
            framing = f"{int(values.get('bytesize', 8))}{str(values.get('parity', 'N')).upper()}{int(float(values.get('stopbits', 1)))}"
            if protocol == "mks_275":
                self.mks_baudrate.set(str(values.get("baudrate", 9600)))
                self.mks_framing.set(framing if framing in {"8N1", "7E1", "7O1"} else "Custom")
            else:
                self.partlow_baudrate.set(str(values.get("baudrate", 9600)))
                self.partlow_framing.set(framing if framing == "7E1" else "Custom")
                self.custom_bytesize.set(str(values.get("bytesize", 7)))
                self.custom_parity.set(str(values.get("parity", "E")))
                self.custom_stopbits.set(str(int(float(values.get("stopbits", 1)))))

        def partlow_address_values(self) -> dict[int, int]:
            result: dict[int, int] = {}
            for pid, variable in self.partlow_addresses.items():
                try:
                    result[pid] = int(variable.get())
                except (TypeError, ValueError):
                    result[pid] = pid + 1
            return result

        def mks_address_value(self) -> int:
            try:
                return int(self.mks_address.get())
            except (TypeError, ValueError):
                return 1

        def passive_values(self) -> dict[str, Any]:
            try:
                baudrate = int(self.passive_baudrate.get())
            except (TypeError, ValueError):
                baudrate = 9600
            try:
                pairing = float(self.pairing_window.get())
            except (TypeError, ValueError):
                pairing = 0.250
            return {"bus_baudrate": baudrate, "strategy": self.passive_strategy.get() or "mixed_raw", "response_pairing_window_s": pairing}

        def ui_values(self) -> dict[str, Any]:
            return {
                "theme": self.theme.get(),
                "show_hex": bool(self.show_hex.get()),
                "timeline_window_s": float(self.timeline_window.get()),
            }


    class RawTrafficView(BaseView):
        """A compact evidence-oriented raw byte table."""

        def __init__(self, parent: tk.Misc, **kwargs: Any) -> None:
            super().__init__(parent, **kwargs)
            self._heading("Raw Traffic", "bytes are retained as received")
            frame = ttk.Frame(self)
            frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
            columns = ("seq", "time", "direction", "ascii", "hex", "flags")
            self.table = ttk.Treeview(
                frame,
                columns=columns,
                show="headings",
                height=14,
                style="Traffic.Treeview",
            )
            self._all_columns = columns
            self.show_hex = True
            headings = {"seq": "#", "time": "Time", "direction": "Dir", "ascii": "ASCII", "hex": "Hex", "flags": "Flags"}
            widths = {"seq": 60, "time": 105, "direction": 55, "ascii": 250, "hex": 300, "flags": 170}
            for column in columns:
                self.table.heading(column, text=headings[column])
                self.table.column(column, width=widths[column], anchor="w", stretch=column in {"ascii", "hex"})
            yscroll = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview)
            xscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.table.xview)
            self.table.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
            field_background = ttk.Style().lookup("Traffic.Treeview", "fieldbackground")
            dark_table = str(field_background).lower() in {"#20262d", "#1f252b", "#212529"}
            self.table.tag_configure("RX", foreground="#8be9fd" if dark_table else "#006d77")
            self.table.tag_configure("TX", foreground="#ffcc80" if dark_table else "#9a4d00")
            self.table.grid(row=0, column=0, sticky="nsew")
            yscroll.grid(row=0, column=1, sticky="ns")
            xscroll.grid(row=1, column=0, sticky="ew")
            self.notice = tk.StringVar(value="No traffic captured")
            ttk.Label(self, textvariable=self.notice, style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(6, 0))

        def set_show_hex(self, enabled: bool) -> None:
            """Show or hide the hexadecimal column without losing captured data."""

            self.show_hex = bool(enabled)
            self.table["displaycolumns"] = self._all_columns if self.show_hex else tuple(
                column for column in self._all_columns if column != "hex"
            )

        def add_records(self, records: Iterable[Any], dropped_before: Any = None) -> int:
            count = 0
            for record in records:
                seq = _value(record, "seq", _value(record, "sequence", ""))
                direction = _text(_value(record, "direction", "")).upper()
                flags = _value(record, "flags", ())
                if not isinstance(flags, str):
                    flags = ", ".join(sorted(_text(flag) for flag in flags))
                self.table.insert(
                    "",
                    "end",
                    values=(seq, _when(record), direction, _ascii(_value(record, "data")), _hex(_value(record, "data")), flags),
                    tags=(direction,) if direction in {"RX", "TX"} else (),
                )
                count += 1
            children = self.table.get_children()
            if len(children) > 2000:
                self.table.delete(*children[: len(children) - 2000])
            if dropped_before not in (None, "", 0):
                self.notice.set(f"Capture gap: records before sequence {dropped_before} were dropped")
            elif count:
                self.notice.set(f"{len(self.table.get_children())} records shown")
            return count

        def clear(self) -> None:
            self.table.delete(*self.table.get_children())
            self.notice.set("No traffic captured")


    class TimelineView(BaseView):
        """Decoded frame/event timeline; intentionally not a chart."""

        def __init__(self, parent: tk.Misc, **kwargs: Any) -> None:
            super().__init__(parent, **kwargs)
            self._heading("Bus Timeline", "decoded frames and worker events")
            frame = ttk.Frame(self)
            frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
            columns = ("id", "time", "direction", "frame", "decoded", "status")
            self.table = ttk.Treeview(frame, columns=columns, show="headings", height=14)
            self.window_s = 30.0
            self._item_times: dict[str, int] = {}
            self._timeline_origin_ns: int | None = None
            headings = {"id": "#", "time": "Time", "direction": "Dir", "frame": "Frame", "decoded": "Decoded", "status": "Status"}
            widths = {"id": 60, "time": 105, "direction": 55, "frame": 270, "decoded": 270, "status": 180}
            for column in columns:
                self.table.heading(column, text=headings[column])
                self.table.column(column, width=widths[column], anchor="w", stretch=column in {"frame", "decoded"})
            yscroll = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview)
            self.table.configure(yscrollcommand=yscroll.set)
            self.table.grid(row=0, column=0, sticky="nsew")
            yscroll.grid(row=0, column=1, sticky="ns")
            self.notice = tk.StringVar(value="No decoded frames")
            ttk.Label(self, textvariable=self.notice, style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(6, 0))

        def set_window_s(self, value: float) -> None:
            """Set the retained display window and prune frames already outside it."""

            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return
            if parsed <= 0:
                return
            self.window_s = parsed
            latest = max(self._item_times.values(), default=None)
            if latest is not None:
                self._prune(latest)

        def _prune(self, latest_ns: int) -> None:
            cutoff = latest_ns - int(self.window_s * 1_000_000_000)
            expired = [item for item, timestamp in self._item_times.items() if timestamp < cutoff]
            if expired:
                self.table.delete(*expired)
                for item in expired:
                    self._item_times.pop(item, None)

        def add_frames(self, frames: Iterable[Any]) -> int:
            count = 0
            for frame in frames:
                decoded = _value(frame, "decoded", None)
                error = _value(frame, "error", "")
                decoded_text = _decoded_summary(decoded)
                status = f"error: {_text(error)}" if error else "ok"
                timestamp = _value(frame, "monotonic_ns", None)
                if isinstance(timestamp, int):
                    if self._timeline_origin_ns is None:
                        self._timeline_origin_ns = timestamp
                    time_text = f"+{(timestamp - self._timeline_origin_ns) / 1_000_000:.1f} ms"
                else:
                    time_text = _when(frame)
                item = self.table.insert(
                    "",
                    "end",
                    values=(
                        _value(frame, "frame_id", _value(frame, "id", "")),
                        time_text,
                        _text(_value(frame, "direction", "")).upper(),
                        _ascii(_value(frame, "raw_frame", _value(frame, "data", b""))),
                        decoded_text,
                        status,
                    ),
                )
                if isinstance(timestamp, int):
                    self._item_times[item] = timestamp
                    self._prune(timestamp)
                count += 1
            children = self.table.get_children()
            if len(children) > 2000:
                expired = children[: len(children) - 2000]
                self.table.delete(*expired)
                for item in expired:
                    self._item_times.pop(item, None)
            if count:
                self.notice.set(f"{len(self.table.get_children())} frames shown")
            return count

        def clear(self) -> None:
            self.table.delete(*self.table.get_children())
            self._item_times.clear()
            self._timeline_origin_ns = None
            self.notice.set("No decoded frames")
