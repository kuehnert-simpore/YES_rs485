"""Composition root for the YES RS-485 Tkinter utility.

The app owns widgets and user intent.  It never touches a pyserial object;
all I/O is delegated to ``SerialWorker``.  Imports of the worker and pyserial
are intentionally lazy so that ``python -m yes_rs485`` can show a useful
diagnostic on a machine where the optional serial dependency is not installed.
"""

from __future__ import annotations

import inspect
import sys
import warnings
from typing import Any, Iterable, Mapping

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # pragma: no cover
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]

try:  # ttkbootstrap is a small optional enhancement; stock ttk remains valid.
    import ttkbootstrap as tb
except ImportError:  # pragma: no cover - exercised in minimal/headless installs
    tb = None  # type: ignore[assignment]

from .views import (
    MasterView,
    PassiveView,
    RawTrafficView,
    SettingsView,
    SlaveView,
    TimelineView,
)


_PROTOCOL_LABELS = {
    "partlow_p1160": "Partlow P1160",
    "mks_275": "MKS 275",
}
_LABEL_TO_PROTOCOL = {label: value for value, label in _PROTOCOL_LABELS.items()}
_MODE_LABELS = {"passive": "Passive Sniffer", "master": "Active Master", "slave": "Active Slave"}


def _value(item: Any, name: str, default: Any = "") -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _plain(value: Any) -> Any:
    """Return an enum's wire value while leaving normal data unchanged."""

    return getattr(value, "value", value)


def _find_model(name: str) -> Any:
    try:
        from .. import models

        return getattr(models, name, None)
    except ImportError:
        return None


def _enum(name: str, value: str) -> Any:
    cls = _find_model(name)
    if cls is None:
        return value
    try:
        return cls(value)
    except (TypeError, ValueError):
        try:
            return getattr(cls, value.upper())
        except (AttributeError, TypeError):
            return value


def _construct(cls: Any, values: Mapping[str, Any]) -> Any:
    """Construct a dataclass/model using only fields its constructor accepts."""

    if cls is None:
        return dict(values)
    try:
        signature = inspect.signature(cls)
        accepted = {
            name: value
            for name, value in values.items()
            if name in signature.parameters
        }
        return cls(**accepted)
    except (TypeError, ValueError):
        # A few small model classes may accept a mapping rather than kwargs.
        try:
            return cls(values)
        except (TypeError, ValueError):
            return dict(values)


def _safe_float(value: str, default: float) -> float:
    try:
        parsed = float(value)
        if parsed <= 0:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        return default


def _safe_int(value: str, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
        if parsed < minimum:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        return default


if tk is None:  # pragma: no cover - permits import where Tk is unavailable

    class YesRs485App:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("Tkinter is not available in this Python installation")

    def main() -> int:
        print("YES RS-485 requires a Python installation with Tkinter.", file=sys.stderr)
        return 2

else:

    class YesRs485App(tk.Tk):
        """Main window and the only UI-to-worker integration point."""

        def __init__(self, worker: Any | None = None, config: Any | None = None) -> None:
            super().__init__()
            self.title("YES RS-485 Engineering Utility")
            self.minsize(980, 680)
            self.geometry("1180x780")
            self.protocol("WM_DELETE_WINDOW", self._on_exit)
            self._worker = worker
            self._config = config
            self._config_load_failed = False
            self._worker_error = ""
            self._session_id: Any = None
            self._session_running = False
            self._session_state = "CLOSED"
            self._last_traffic_seq = 0
            self._last_frame_id = 0
            self._polling_id: str | None = None
            self._closing = False

            self._load_optional_config()
            self._setup_style()
            self._build_controls()
            self._build_views()
            self._apply_loaded_config()
            self._apply_mode_view()
            self._refresh_ports(show_errors=False)
            self._polling_id = self.after(100, self._poll_worker)

        def _setup_style(self) -> None:
            requested = _plain(getattr(getattr(self._config, "ui", None), "theme", "darkly"))
            if requested in {"dark", "system", "default", ""}:
                requested = "darkly" if requested != "light" else "light"
            if requested not in {"darkly", "cyborg", "flatly", "light"}:
                requested = "darkly"
            self._theme_name = requested
            # ttkbootstrap names its light theme ``flatly``; keep ``light`` as
            # the user-facing preference while translating it for the engine.
            theme_for_engine = "flatly" if requested == "light" else requested
            self._bootstrap_style = None
            if tb is not None:
                try:
                    self._bootstrap_style = tb.Style(master=self, theme=theme_for_engine)
                except TypeError:
                    try:
                        self._bootstrap_style = tb.Style(theme=theme_for_engine)
                    except (tk.TclError, TypeError, RuntimeError):
                        self._bootstrap_style = None
                except (tk.TclError, RuntimeError):
                    self._bootstrap_style = None
            style = self._bootstrap_style or ttk.Style(self)
            if self._bootstrap_style is None:
                try:
                    style.theme_use("clam" if requested in {"darkly", "cyborg"} else "vista")
                except tk.TclError:
                    pass
            style.configure("Title.TLabel", font=("Segoe UI", 14, "bold"))
            style.configure("Status.TLabel", foreground="#67b7ff")
            style.configure("Muted.TLabel", foreground="#8d99a6")
            style.configure("Warning.TLabel", foreground="#ffca62", padding=6)
            style.configure(
                "StatusBar.TFrame",
                borderwidth=1,
                relief="solid",
                padding=(8, 4),
            )
            style.configure("Badge.Passive.TLabel", foreground="#67d7c0", font=("Segoe UI", 10, "bold"), padding=(8, 4))
            style.configure("Badge.Master.TLabel", foreground="#ffca62", font=("Segoe UI", 10, "bold"), padding=(8, 4))
            style.configure("Badge.Slave.TLabel", foreground="#ff8f8f", font=("Segoe UI", 10, "bold"), padding=(8, 4))
            if requested in {"darkly", "cyborg"}:
                table_background = "#20262d"
                table_field = "#20262d"
                table_foreground = "#edf2f7"
                table_heading = "#343d47"
                selected_background = "#285f9e"
                rx_foreground = "#8be9fd"
                tx_foreground = "#ffcc80"
            else:
                table_background = "#ffffff"
                table_field = "#ffffff"
                table_foreground = "#20242a"
                table_heading = "#e7ebf0"
                selected_background = "#b9d7f5"
                rx_foreground = "#006d77"
                tx_foreground = "#9a4d00"
            style.configure(
                "Treeview",
                background=table_background,
                fieldbackground=table_field,
                foreground=table_foreground,
                rowheight=25,
            )
            style.map(
                "Treeview",
                background=[("selected", selected_background)],
                foreground=[("selected", table_foreground)],
            )
            style.configure(
                "Treeview.Heading",
                background=table_heading,
                foreground=table_foreground,
                font=("Segoe UI", 9, "bold"),
            )
            style.configure("Traffic.Treeview", font=("Consolas", 10))
            self._table_colors = {"RX": rx_foreground, "TX": tx_foreground}
            self._style = style
            self._configure_polished_styles(requested, table_foreground)

        def _configure_polished_styles(self, theme: str, table_foreground: str) -> None:
            """Apply theme-aware layout polish after setup or a theme switch."""

            dark = theme in {"darkly", "cyborg"}
            notebook_background = "#1d2329" if dark else "#f5f7fa"
            tab_background = "#2b333c" if dark else "#e6ebf0"
            tab_foreground = "#c8d4e0" if dark else "#34404c"
            selected_tab = "#285f9e" if dark else "#cfe3f7"
            selected_tab_foreground = "#ffffff" if dark else "#17324d"
            notebook_outline = "#46515d" if dark else "#c8d0d8"
            # Larger padding gives the tab strip a calmer rhythm and a usable
            # hit target while preserving the same six destinations.
            self._style.configure(
                "TNotebook",
                background=notebook_background,
                borderwidth=1,
                relief="solid",
                padding=3,
            )
            self._style.configure(
                "TNotebook.Tab",
                background=tab_background,
                foreground=tab_foreground,
                borderwidth=0,
                padding=(16, 8),
                font=("Segoe UI", 10, "bold"),
            )
            self._style.map(
                "TNotebook.Tab",
                background=[
                    ("selected", selected_tab),
                    ("active", selected_tab),
                    ("!active", tab_background),
                ],
                foreground=[
                    ("selected", selected_tab_foreground),
                    ("active", selected_tab_foreground),
                    ("!active", tab_foreground),
                ],
            )
            # These colors are recognized by the stock themes; ttkbootstrap
            # simply falls back to its own panel element where unavailable.
            try:
                self._style.configure(
                    "TNotebook",
                    lightcolor=notebook_outline,
                    darkcolor=notebook_outline,
                )
            except tk.TclError:
                pass
            self._style.configure(
                "StatusBar.TFrame",
                background=notebook_background,
                borderwidth=1,
                relief="solid",
                padding=(8, 4),
            )
            self._style.configure(
                "Effective.TLabel",
                foreground=table_foreground,
                background=notebook_background,
                padding=(8, 4),
            )

        def _switch_theme(self, theme: str) -> None:
            """Apply a theme immediately when the available style supports it."""

            requested = str(theme or "darkly").strip().lower()
            if requested in {"dark", "system", "default"}:
                requested = "darkly"
            if requested not in {"darkly", "cyborg", "flatly", "light"}:
                requested = "darkly"
            theme_for_engine = "flatly" if requested == "light" else requested
            try:
                self._style.theme_use(theme_for_engine)
                self._theme_name = requested
            except tk.TclError:
                # A stock ttk interpreter only has platform themes.  Keep the
                # app usable and persist the requested preference for a future
                # run with ttkbootstrap installed.
                try:
                    self._style.theme_use("clam" if requested in {"darkly", "cyborg"} else "vista")
                    self._theme_name = requested
                except tk.TclError:
                    pass
            # Theme switches can restore theme defaults for ttk elements; keep
            # the notebook/tab treatment and summary panel consistent with the
            # initial theme selection.
            table_foreground = "#edf2f7" if requested in {"darkly", "cyborg"} else "#20242a"
            self._configure_polished_styles(requested, table_foreground)

        def _load_optional_config(self) -> None:
            if self._config is not None:
                return
            try:
                from ..config import load_config

                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    result = load_config()
                for item in caught:
                    warnings.warn(item.message, item.category, stacklevel=2)
                    if issubclass(item.category, RuntimeWarning):
                        # Do not overwrite an invalid file automatically when
                        # the window closes; the user may need to recover it.
                        self._config_load_failed = True
                self._config = result[0] if isinstance(result, tuple) else result
            except (ImportError, OSError, ValueError, TypeError):
                # Configuration is a convenience; the utility remains usable with
                # defaults if a profile is malformed or unavailable.
                self._config = None

        def _apply_loaded_config(self) -> None:
            """Apply persisted preferences without restoring any live safety state."""

            config = self._config
            if config is None:
                return
            protocol = _plain(getattr(config, "selected_protocol", "partlow_p1160"))
            if protocol in _PROTOCOL_LABELS:
                self.protocol_label.set(_PROTOCOL_LABELS[protocol])
            self._apply_protocol_profile()

            preferred = getattr(config, "preferred_port", None)
            if preferred is not None:
                device = getattr(preferred, "device", "")
                if device:
                    self.port.set(device)
                    self.settings_view.preferred_port.set(device)

            self.settings_view.timeout.set(str(getattr(config, "response_timeout_s", 1.0)))
            self.settings_view.raw_capacity.set(str(getattr(config, "raw_capacity", 5000)))
            ui = getattr(config, "ui", None)
            if ui is not None:
                show_hex = bool(getattr(ui, "show_hex", True))
                timeline_window = float(getattr(ui, "timeline_window_s", 30.0))
                self.settings_view.show_hex.set(show_hex)
                self.settings_view.timeline_window.set(str(timeline_window))
                saved_theme = str(getattr(ui, "theme", "darkly")).strip().lower()
                # Normalize legacy model values to the concrete picker labels
                # used by ttkbootstrap while retaining their intended look.
                if saved_theme in {"dark", "system", "default", ""}:
                    saved_theme = "darkly"
                elif saved_theme == "light":
                    saved_theme = "light"
                if saved_theme not in {"darkly", "cyborg", "flatly", "light"}:
                    saved_theme = "darkly"
                self.settings_view.theme.set(saved_theme)
                self.raw_view.set_show_hex(show_hex)
                self.timeline_view.set_window_s(timeline_window)

            partlow_serial = getattr(config, "partlow_bus_serial", getattr(config, "partlow_serial", None))
            if partlow_serial is not None:
                self.settings_view.set_serial_values(
                    "partlow_p1160",
                    {
                        "baudrate": getattr(partlow_serial, "baudrate", 9600),
                        "bytesize": getattr(partlow_serial, "bytesize", 7),
                        "parity": getattr(partlow_serial, "parity", "E"),
                        "stopbits": getattr(partlow_serial, "stopbits", 1),
                    },
                )
            for pid, address in dict(getattr(config, "partlow_addresses", {}) or {}).items():
                try:
                    self.settings_view.partlow_addresses[int(pid)].set(str(address))
                except (KeyError, TypeError, ValueError):
                    pass
            passive = getattr(config, "passive_capture", None)
            if passive is not None:
                self.settings_view.passive_baudrate.set(str(getattr(passive, "bus_baudrate", 9600)))
                self.settings_view.passive_strategy.set(str(getattr(passive, "strategy", "mixed_raw")))
                self.settings_view.pairing_window.set(str(getattr(passive, "response_pairing_window_s", 0.250)))

            profiles = getattr(config, "protocol_profiles", {})
            mks_profile = None
            for key, candidate in dict(profiles).items():
                if _plain(key) == "mks_275":
                    mks_profile = candidate
                    break
            if mks_profile is not None:
                serial = getattr(mks_profile, "serial", None)
                if serial is not None:
                    self.settings_view.set_serial_values(
                        "mks_275",
                        {
                            "baudrate": getattr(serial, "baudrate", 9600),
                            "bytesize": getattr(serial, "bytesize", 8),
                            "parity": getattr(serial, "parity", "N"),
                            "stopbits": getattr(serial, "stopbits", 1),
                        },
                    )
                self.settings_view.mks_address.set(str(getattr(mks_profile, "device_address", 1)))

            protocol_profiles = getattr(config, "slave_profiles", {})
            for profile in protocol_profiles.values():
                if _plain(getattr(profile, "protocol", None)) != protocol:
                    continue
                self.slave_view.slave_address.set(str(getattr(profile, "address", 1)))
                values = dict(getattr(profile, "values", {}) or {})
                for name, variable in (
                    ("process_value", self.slave_view.process_value),
                    ("setpoint", self.slave_view.setpoint),
                    ("pressure", self.slave_view.pressure),
                    ("units", self.slave_view.units),
                ):
                    if name in values:
                        variable.set(str(values[name]))
                break

        def _build_controls(self) -> None:
            outer = ttk.Frame(self, padding=(10, 8, 10, 0))
            outer.pack(fill="x")
            outer.columnconfigure(5, weight=1)
            outer.columnconfigure(10, weight=1)
            ttk.Label(outer, text="Mode").grid(row=0, column=0, sticky="w", padx=(0, 6))
            self.mode = tk.StringVar(value="passive")
            self.mode_combo = ttk.Combobox(
                outer,
                textvariable=self.mode,
                values=tuple(_MODE_LABELS),
                state="readonly",
                width=18,
            )
            # Show the stable key in the model while rendering a readable label
            # in the drop-down by using a separate label variable below.
            self.mode_label = tk.StringVar(value=_MODE_LABELS["passive"])
            self.mode_combo.configure(textvariable=self.mode_label, values=tuple(_MODE_LABELS.values()))
            self.mode_combo.bind("<<ComboboxSelected>>", self._on_mode_changed)
            self.mode_combo.grid(row=0, column=1, sticky="w", padx=(0, 16))
            self.mode_badge = tk.StringVar(value="RX ONLY")
            self.mode_badge_widget = ttk.Label(outer, textvariable=self.mode_badge, style="Badge.Passive.TLabel")
            self.mode_badge_widget.grid(
                row=0, column=10, sticky="e", padx=(8, 12)
            )

            ttk.Label(outer, text="Protocol").grid(row=0, column=2, sticky="w", padx=(0, 6))
            self.protocol_label = tk.StringVar(value=_PROTOCOL_LABELS["partlow_p1160"])
            self.protocol_combo = ttk.Combobox(
                outer,
                textvariable=self.protocol_label,
                values=tuple(_PROTOCOL_LABELS.values()),
                state="readonly",
                width=16,
            )
            self.protocol_combo.bind("<<ComboboxSelected>>", self._on_protocol_changed)
            self.protocol_combo.grid(row=0, column=3, sticky="w", padx=(0, 16))

            ttk.Label(outer, text="COM port").grid(row=0, column=4, sticky="w", padx=(0, 6))
            self.port = tk.StringVar()
            self.port_combo = ttk.Combobox(outer, textvariable=self.port, width=18)
            self.port_combo.grid(row=0, column=5, sticky="ew", padx=(0, 6))
            self.session_status = tk.StringVar(value="CLOSED | Passive | No COM | Partlow P1160")
            ttk.Button(outer, text="Refresh", command=lambda: self._refresh_ports(show_errors=True)).grid(
                row=0, column=6, sticky="w", padx=(0, 8)
            )
            self.arm = tk.BooleanVar(value=False)
            self.arm_check = ttk.Checkbutton(outer, text="I understand the safety warning", variable=self.arm)
            self.arm_check.grid(row=0, column=7, sticky="w", padx=(0, 8))
            self.open_button = ttk.Button(outer, text="Open", command=self._open_session)
            self.open_button.grid(row=0, column=8, sticky="w", padx=(0, 4))
            self.close_button = ttk.Button(outer, text="Close", command=self._close_session, state="disabled")
            self.close_button.grid(row=0, column=9, sticky="w")

            # Keep the compact status indicator in sync even when a selection
            # is changed by keyboard input or restored from configuration.
            for variable in (self.mode_label, self.protocol_label, self.port):
                variable.trace_add("write", self._on_selection_changed)

            # Keep the complete session identity on its own row.  The toolbar
            # needs room for the port selector and safety controls, while the
            # status must remain readable for long COM/device names.
            status_row = ttk.Frame(self, style="StatusBar.TFrame", padding=(8, 4))
            status_row.pack(fill="x", padx=10, pady=(2, 0))
            ttk.Label(status_row, text="Session", style="Muted.TLabel").pack(
                side="left", padx=(0, 12)
            )
            self.session_status_widget = ttk.Label(
                status_row,
                textvariable=self.session_status,
                style="Status.TLabel",
                anchor="w",
            )
            self.session_status_widget.pack(side="left", fill="x", expand=True)

            self.banner = tk.StringVar(value="Passive mode: no application writes are permitted.")
            ttk.Label(self, textvariable=self.banner, style="Warning.TLabel", wraplength=1100).pack(
                fill="x", padx=10, pady=(6, 4)
            )

        def _build_views(self) -> None:
            self.notebook = ttk.Notebook(self)
            self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))
            self.passive_view = PassiveView(self.notebook)
            self.master_view = MasterView(self.notebook, on_submit=self._submit_master)
            self.slave_view = SlaveView(self.notebook, on_apply=self._apply_slave_profile)
            self.settings_view = SettingsView(self.notebook)
            self.settings_view.theme.trace_add(
                "write", lambda *_args: self._switch_theme(self.settings_view.theme.get())
            )
            self.raw_view = RawTrafficView(self.notebook)
            self.timeline_view = TimelineView(self.notebook)
            self.notebook.add(self.passive_view, text="Passive Sniffer")
            self.notebook.add(self.master_view, text="Active Master")
            self.notebook.add(self.slave_view, text="Active Slave")
            self.notebook.add(self.settings_view, text="Settings")
            self.notebook.add(self.raw_view, text="Raw Traffic")
            self.notebook.add(self.timeline_view, text="Bus Timeline")

        def _mode_key(self) -> str:
            label = self.mode_label.get()
            for key, candidate in _MODE_LABELS.items():
                if candidate == label:
                    return key
            return "passive"

        def _protocol_key(self) -> str:
            return _LABEL_TO_PROTOCOL.get(self.protocol_label.get(), "partlow_p1160")

        def _on_selection_changed(self, *_args: Any) -> None:
            self._update_session_status()

        def _update_session_status(self) -> None:
            mode = self.mode_label.get().replace(" Sniffer", "") or "Passive"
            port = self.port.get().strip() or "No COM"
            protocol = self.protocol_label.get().strip() or "No protocol"
            self.session_status.set(f"{self._session_state} | {mode} | {port} | {protocol}")
            if hasattr(self, "settings_view"):
                self.settings_view.set_context(self._mode_key(), self._protocol_key())

        def _set_session_state(self, state: str) -> None:
            self._session_state = state.upper()
            self._update_session_status()

        def _on_mode_changed(self, _event: Any = None) -> None:
            self._apply_mode_view()
            self._update_session_status()

        def _apply_mode_view(self) -> None:
            mode = self._mode_key()
            self.notebook.select({"passive": self.passive_view, "master": self.master_view, "slave": self.slave_view}[mode])
            if mode == "passive":
                self.mode_badge.set("RX ONLY")
                self.mode_badge_widget.configure(style="Badge.Passive.TLabel")
                self.banner.set("Passive mode: RX only. No application writes, probes, or replies are permitted.")
                self.arm_check.configure(state="disabled")
                self.master_view.set_enabled(False)
            elif mode == "master":
                self.mode_badge.set("ACTIVE MASTER")
                self.mode_badge_widget.configure(style="Badge.Master.TLabel")
                self.banner.set("WARNING — Active Master can transmit. Disconnect the PLC/controller before opening.")
                self.arm_check.configure(state="normal")
                self.master_view.set_enabled(self._session_running)
            else:
                self.mode_badge.set("ACTIVE SLAVE")
                self.mode_badge_widget.configure(style="Badge.Slave.TLabel")
                self.banner.set("WARNING — Active Slave can reply. Disconnect the real sensors before opening.")
                self.arm_check.configure(state="normal")
                self.master_view.set_enabled(False)
            self._update_session_status()

        def _on_protocol_changed(self, _event: Any = None) -> None:
            self._apply_protocol_profile()
            self._update_session_status()

        def _apply_protocol_profile(self) -> None:
            protocol = self._protocol_key()
            if hasattr(self, "slave_view"):
                self.slave_view.set_protocol(protocol)
            profile = None
            config = self._config
            if config is not None:
                protocol_enum = _enum("ProtocolId", protocol)
                profiles = getattr(config, "protocol_profiles", {})
                profile = profiles.get(protocol_enum)
                if profile is None:
                    profile = next(
                        (
                            candidate
                            for key, candidate in profiles.items()
                            if _plain(key) == protocol
                        ),
                        None,
                    )
            if protocol == "partlow_p1160":
                serial = getattr(self._config, "partlow_bus_serial", getattr(self._config, "partlow_serial", None))
                if serial is None and profile is not None:
                    serial = getattr(profile, "serial", None)
                values = {
                    "baudrate": getattr(serial, "baudrate", 9600),
                    "bytesize": getattr(serial, "bytesize", 7),
                    "parity": getattr(serial, "parity", "E"),
                    "stopbits": getattr(serial, "stopbits", 1),
                }
                self.settings_view.set_serial_values(protocol, values)
                addresses = dict(getattr(self._config, "partlow_addresses", {}) or {}) if self._config else {}
                address = addresses.get(1, getattr(profile, "device_address", 2) if profile else 2)
                if hasattr(self, "master_view"):
                    self.master_view.address.set(str(address))
            else:
                serial = getattr(profile, "serial", None)
                values = {
                    "baudrate": getattr(serial, "baudrate", 9600),
                    "bytesize": getattr(serial, "bytesize", 8),
                    "parity": getattr(serial, "parity", "N"),
                    "stopbits": getattr(serial, "stopbits", 1),
                }
                self.settings_view.set_serial_values(protocol, values)
                if hasattr(self, "master_view"):
                    self.master_view.address.set(
                        str(getattr(profile, "device_address", self.settings_view.mks_address_value() or 1))
                    )

        def _refresh_ports(self, show_errors: bool = True) -> None:
            try:
                from serial.tools import list_ports

                ports = list(list_ports.comports())
            except ImportError:
                self._worker_error = "pyserial is not installed; install the project dependencies to enumerate COM ports."
                if show_errors:
                    self._show_error(self._worker_error)
                return
            except OSError as exc:
                self._show_error(f"Unable to enumerate COM ports: {exc}")
                return
            values = []
            for item in ports:
                description = getattr(item, "description", None) or "Unknown device"
                vid = getattr(item, "vid", None)
                pid = getattr(item, "pid", None)
                serial_number = getattr(item, "serial_number", None)
                details = description
                if vid is not None and pid is not None:
                    details += f" (VID {vid:04X}:PID {pid:04X})"
                if serial_number:
                    details += f" [{serial_number}]"
                values.append((getattr(item, "device", str(item)), details))
            self.port_combo["values"] = tuple(device for device, _description in values)
            if values and not self.port.get():
                self.port.set(values[0][0])
            self._update_session_status()
            if not values and show_errors:
                self.passive_view.set_status("No COM ports found. Connect the adapter, then Refresh.")

        def _build_spec(self) -> Any:
            if self._mode_key() == "passive":
                # Passive capture is deliberately distinct from a device
                # profile.  The validated SH-U22C capture path is represented
                # as raw 8N1 bytes while the device cards remain 7E1/8N1/etc.
                capture = self.settings_view.passive_values()
                settings = {
                    "baudrate": capture["bus_baudrate"],
                    "bytesize": 8,
                    "parity": "N",
                    "stopbits": 1.0,
                }
            else:
                settings = self.settings_view.serial_values(self._protocol_key())
            if not self.port.get().strip():
                raise ValueError("Select a COM port first")
            serial_settings = _construct(
                _find_model("SerialSettings"),
                {
                    "port": self.port.get().strip(),
                    **settings,
                    "xonxoff": False,
                    "rtscts": False,
                    "dsrdtr": False,
                },
            )
            mode = _enum("BusMode", self._mode_key())
            protocol = _enum("ProtocolId", self._protocol_key())
            profile = self._build_slave_profile() if self._mode_key() == "slave" else None
            return _construct(
                _find_model("SessionSpec"),
                {
                    "mode": mode,
                    "protocol": protocol,
                    "serial": serial_settings,
                    "serial_settings": serial_settings,
                    "response_timeout_s": _safe_float(self.settings_view.timeout.get(), 1.0),
                    "raw_capacity": _safe_int(self.settings_view.raw_capacity.get(), 5000),
                    "slave_profile": profile,
                },
            )

        def _build_slave_profile(self) -> Any:
            values = self.slave_view.profile()
            address = _safe_int(values["address"], 1)
            # SlaveProfile intentionally keeps protocol-specific state in its
            # immutable ``values`` mapping.  Preserve the strings entered by
            # the engineer rather than silently rounding display precision.
            numeric = {**values, "address": address, "values": dict(values)}
            # Keep the string forms available for protocol adapters that preserve
            # display precision, while offering common numeric aliases.
            try:
                numeric["process_value_float"] = float(values["process_value"])
            except ValueError:
                pass
            try:
                numeric["setpoint_float"] = float(values["setpoint"])
            except ValueError:
                pass
            try:
                numeric["pressure_float"] = float(values["pressure"])
            except ValueError:
                pass
            numeric["protocol"] = _enum("ProtocolId", self._protocol_key())
            return _construct(_find_model("SlaveProfile"), numeric)

        def _arm_for_mode(self) -> Any:
            mode = self._mode_key()
            if mode == "master":
                return _enum("SafetyArm", "plc_disconnected")
            if mode == "slave":
                return _enum("SafetyArm", "sensors_disconnected")
            return None

        def _ensure_worker(self) -> Any | None:
            if self._worker is not None:
                return self._worker
            try:
                from ..serial_worker import SerialWorker

                self._worker = SerialWorker()
                self._worker.start()
                return self._worker
            except ImportError:
                self._worker_error = "SerialWorker is unavailable; install the package and its dependencies first."
            except Exception as exc:
                self._worker_error = f"Unable to start SerialWorker: {exc}"
            self._set_session_state("FAULTED")
            self._show_error(self._worker_error)
            return None

        def _open_session(self) -> None:
            mode = self._mode_key()
            if mode != "passive" and not self.arm.get():
                self._show_error(
                    "Opening an active session requires the non-persisted safety acknowledgement."
                )
                return
            try:
                spec = self._build_spec()
            except (ValueError, TypeError) as exc:
                self._show_error(str(exc))
                return
            worker = self._ensure_worker()
            if worker is None:
                return
            self._set_session_state("OPENING")
            try:
                if hasattr(worker, "start"):
                    worker.start()
                self._session_id = worker.open(spec, arm=self._arm_for_mode())
            except Exception as exc:
                self._set_session_state("FAULTED")
                self._show_error(f"Could not open the session: {exc}")
                return
            self._session_running = False
            self.master_view.set_enabled(False)
            self.open_button.configure(state="disabled")
            self.close_button.configure(state="normal")
            self.mode_combo.configure(state="disabled")
            self.protocol_combo.configure(state="disabled")
            self.port_combo.configure(state="disabled")
            self.arm_check.configure(state="disabled")
            self._set_mode_status("Opening serial session…")

        def _close_session(self) -> None:
            worker = self._worker
            if worker is not None and self._session_id is not None:
                try:
                    worker.close()
                except Exception as exc:
                    self._set_mode_status(f"Close request failed: {exc}")
            self._reset_session_controls()
            self._set_mode_status("Port closed")

        def _reset_session_controls(self) -> None:
            """Return controls to the safe, arm-cleared idle state.

            A serial fault or an open rejection closes the handle in the
            worker without going through the user's Close button.  Keeping
            the old command id and disabled controls in that case makes the
            UI look armed/open even though no session exists.
            """
            self._session_id = None
            self._session_running = False
            self._set_session_state("CLOSED")
            self.arm.set(False)  # never carry an active arm across close/reopen
            self.open_button.configure(state="normal")
            self.close_button.configure(state="disabled")
            self.mode_combo.configure(state="readonly")
            self.protocol_combo.configure(state="readonly")
            self.port_combo.configure(state="normal")
            self.master_view.set_enabled(False)
            self._apply_mode_view()

        def _build_master_operation(self) -> Any:
            data = self.master_view.operation()
            address = _safe_int(data["address"], 1)
            values: dict[str, Any] = {
                **data,
                "kind": data["operation"],
                "operation_type": data["operation"],
                "address": address,
                "device_address": address,
                "parameter": data["parameter"],
                "value": data["value"],
                "payload": data["value"],
            }
            cls = _find_model("MasterOperation")
            if cls is None:
                return values
            operation = _construct(cls, values)
            if isinstance(operation, dict):
                return values
            return operation

        def _submit_master(self) -> None:
            if self._session_id is None or not self._session_running or self._mode_key() != "master":
                self.master_view.set_status("Open an armed Active Master session first.")
                return
            worker = self._worker
            if worker is None:
                return
            try:
                transaction_id = worker.submit(self._build_master_operation())
                self.master_view.set_status(f"Request queued: {transaction_id}")
            except Exception as exc:
                self.master_view.set_status(f"Request rejected: {exc}")

        def _apply_slave_profile(self) -> None:
            if self._session_id is None or self._mode_key() != "slave":
                self.slave_view.set_status("Open an armed Active Slave session before applying a profile.")
                return
            worker = self._worker
            if worker is None:
                return
            try:
                command_id = worker.replace_slave_profile(self._build_slave_profile())
                self.slave_view.set_status(f"Profile update queued: {command_id}")
            except Exception as exc:
                self.slave_view.set_status(f"Profile rejected: {exc}")

        def _set_mode_status(self, text: str) -> None:
            mode = self._mode_key()
            if mode == "passive":
                self.passive_view.set_status(text)
            elif mode == "master":
                self.master_view.set_status(text)
                self.master_view.set_enabled(self._session_running)
            else:
                self.slave_view.set_status(text)

        def _poll_worker(self) -> None:
            if self._closing:
                return
            worker = self._worker
            if worker is not None:
                try:
                    self._consume_notifications(worker.drain_notifications(limit=200))
                    batch = worker.drain_traffic(self._last_traffic_seq, limit=300)
                    records = _value(batch, "records", batch if isinstance(batch, (list, tuple)) else ())
                    self.raw_view.add_records(
                        records,
                        _value(batch, "dropped_before_seq", _value(batch, "oldest_seq", None)),
                    )
                    for record in records:
                        sequence = _value(record, "seq", _value(record, "sequence", self._last_traffic_seq))
                        try:
                            self._last_traffic_seq = max(self._last_traffic_seq, int(sequence))
                        except (TypeError, ValueError):
                            pass
                    batch = worker.drain_frames(self._last_frame_id, limit=300)
                    frames = _value(batch, "frames", batch if isinstance(batch, (list, tuple)) else ())
                    self.timeline_view.add_frames(frames)
                    for frame in frames:
                        frame_id = _value(frame, "frame_id", _value(frame, "id", self._last_frame_id))
                        try:
                            self._last_frame_id = max(self._last_frame_id, int(frame_id))
                        except (TypeError, ValueError):
                            pass
                except Exception as exc:
                    # A polling error must not kill Tk's after loop; surface it
                    # in the active mode view and keep trying lifecycle updates.
                    self._set_session_state("FAULTED")
                    self._set_mode_status(f"Worker polling error: {exc}")
            self._polling_id = self.after(100, self._poll_worker)

        def _consume_notifications(self, notifications: Iterable[Any] | None) -> None:
            for notification in notifications or ():
                name = type(notification).__name__
                state = _plain(_value(notification, "new_state", _value(notification, "state", "")))
                detail = _value(
                    notification,
                    "detail",
                    _value(
                        notification,
                        "reason",
                        _value(notification, "exception_summary", ""),
                    ),
                )
                if name.lower().endswith("faulted"):
                    self._reset_session_controls()
                    self._set_session_state("FAULTED")
                    self._set_mode_status(f"Serial worker fault: {detail or notification}")
                elif "Rejected" in name or "Failed" in name:
                    self._set_mode_status(f"Worker rejected request: {detail or notification}")
                    # Only an open command rejection invalidates the pending
                    # session controls.  A rejected master operation must not
                    # tear down an otherwise healthy session.
                    command_id = _value(notification, "command_id", None)
                    if command_id is not None and command_id == self._session_id:
                        self._reset_session_controls()
                elif "StateChanged" in name:
                    state_text = str(state).lower()
                    self._session_running = "running" in state_text
                    if "fault" in state_text:
                        self._set_session_state("FAULTED")
                    elif "opening" in state_text:
                        self._set_session_state("OPENING")
                    elif self._session_running:
                        self._set_session_state("RUNNING")
                    elif self._session_state != "FAULTED":
                        self._set_session_state("CLOSED")
                    self._set_mode_status(f"Session state: {state or notification}")
                    if self._session_running:
                        self.open_button.configure(state="disabled")
                        self.close_button.configure(state="normal")
                        if self._mode_key() == "master":
                            self.master_view.set_enabled(True)
                    elif self._mode_key() == "master":
                        self.master_view.set_enabled(False)
                elif "Dropped" in name:
                    self._set_mode_status(f"{name}: {_value(notification, 'total_count', notification)}")

        def _snapshot_config(self) -> Any:
            """Build a persisted snapshot from current controls and preferences."""

            from ..config import default_config
            from ..models import (
                AppConfig,
                PassiveCaptureSettings,
                PortIdentity,
                ProtocolId,
                ProtocolProfile,
                SerialFormat,
                SlaveProfile,
                UiPreferences,
            )

            existing = self._config if self._config is not None else default_config()
            protocol = ProtocolId(self._protocol_key())
            protocol_profiles = dict(getattr(existing, "protocol_profiles", {}))
            partlow = SerialFormat(**self.settings_view.serial_values("partlow_p1160"))
            mks = SerialFormat(**self.settings_view.serial_values("mks_275"))
            partlow_addresses: dict[int, int] = {}
            for pid, address in self.settings_view.partlow_address_values().items():
                if 1 <= pid <= 11 and 1 <= address <= 99:
                    partlow_addresses[pid] = address
            if len(partlow_addresses) != 11:
                partlow_addresses = {pid: pid + 1 for pid in range(1, 12)}
            mks_address = self.settings_view.mks_address_value()
            if not 0 <= mks_address <= 15:
                mks_address = 1
            protocol_profiles[ProtocolId.PARTLOW_P1160] = ProtocolProfile(partlow, partlow_addresses[1])
            protocol_profiles[ProtocolId.MKS_275] = ProtocolProfile(mks, mks_address)

            preferred = getattr(existing, "preferred_port", None)
            port_name = self.settings_view.preferred_port.get().strip() or self.port.get().strip()
            if port_name:
                if preferred is not None and getattr(preferred, "device", None) == port_name:
                    preferred = PortIdentity(
                        port_name,
                        getattr(preferred, "vid", None),
                        getattr(preferred, "pid", None),
                        getattr(preferred, "serial_number", None),
                        getattr(preferred, "description", None),
                    )
                else:
                    preferred = PortIdentity(port_name)

            slave_profiles = dict(getattr(existing, "slave_profiles", {}))
            matching_slave = next(
                (
                    (name, profile)
                    for name, profile in slave_profiles.items()
                    if _plain(getattr(profile, "protocol", None)) == protocol.value
                ),
                None,
            )
            if matching_slave is not None:
                name, old_slave = matching_slave
                slave_address = getattr(old_slave, "address", 1)
                try:
                    candidate_address = int(self.slave_view.slave_address.get())
                    if 0 <= candidate_address <= 255:
                        slave_address = candidate_address
                except (TypeError, ValueError):
                    pass
                slave_profiles[name] = SlaveProfile(
                    name=getattr(old_slave, "name", name),
                    protocol=protocol,
                    address=slave_address,
                    values=self.slave_view.profile(),
                )

            existing_ui = getattr(existing, "ui", UiPreferences())
            try:
                ui_values = self.settings_view.ui_values()
                theme = str(ui_values.get("theme", getattr(existing_ui, "theme", "darkly")))
                try:
                    ui = UiPreferences(
                    theme=theme,
                    show_hex=bool(ui_values["show_hex"]),
                    timeline_window_s=float(ui_values["timeline_window_s"]),
                    )
                except ValueError:
                    # Older model versions only accepted system/light/dark;
                    # keep the app save path usable until they are upgraded.
                    ui = UiPreferences(
                        theme="dark" if theme in {"darkly", "cyborg"} else "light",
                        show_hex=bool(ui_values["show_hex"]),
                        timeline_window_s=float(ui_values["timeline_window_s"]),
                    )
            except (TypeError, ValueError, KeyError):
                ui = existing_ui
            passive_values = self.settings_view.passive_values()
            try:
                passive = PassiveCaptureSettings(**passive_values)
            except (TypeError, ValueError):
                passive = getattr(existing, "passive_capture", PassiveCaptureSettings())
            return AppConfig(
                schema_version=getattr(existing, "schema_version", 1),
                selected_protocol=protocol,
                preferred_port=preferred,
                protocol_profiles=protocol_profiles,
                slave_profiles=slave_profiles,
                response_timeout_s=_safe_float(
                    self.settings_view.timeout.get(), getattr(existing, "response_timeout_s", 1.0)
                ),
                raw_capacity=_safe_int(
                    self.settings_view.raw_capacity.get(), getattr(existing, "raw_capacity", 5000)
                ),
                ui=ui,
                partlow_bus_serial=partlow,
                partlow_addresses=partlow_addresses,
                passive_capture=passive,
            )

        def _save_config(self) -> None:
            if self._config_load_failed:
                return
            try:
                from ..config import save_config

                snapshot = self._snapshot_config()
                save_config(snapshot)
                self._config = snapshot
            except (ImportError, OSError, TypeError, ValueError) as exc:
                # Configuration is a convenience; a read-only profile or an
                # invalid in-progress field must not prevent the app closing.
                print(f"Could not save YES_RS485 configuration: {exc}", file=sys.stderr)

        def _show_error(self, text: str) -> None:
            if messagebox is not None:
                messagebox.showerror("YES RS-485", text, parent=self)
            else:  # pragma: no cover
                self._set_mode_status(text)

        def _on_exit(self) -> None:
            self._closing = True
            if self._polling_id:
                try:
                    self.after_cancel(self._polling_id)
                except tk.TclError:
                    pass
            try:
                self._close_session()
                if self._worker is not None and hasattr(self._worker, "stop"):
                    self._worker.stop()
                self._save_config()
            finally:
                self.destroy()


    def main() -> int:
        app = YesRs485App()
        app.mainloop()
        return 0


__all__ = ["YesRs485App", "main"]
