"""Composition root for the whole-bus YES RS-485 Tk application."""

from __future__ import annotations

import sys
import time
import warnings
from collections import deque
import math
from typing import Any, Iterable, Mapping

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # pragma: no cover
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

try:
    import ttkbootstrap as tb
except ImportError:  # pragma: no cover
    tb = None  # type: ignore[assignment]

from .views import MasterView, PassiveView, RawTrafficView, SettingsView, SlaveView, TimelineView


_MODE_LABELS = {"passive": "Passive Sniffer", "master": "Active Master", "slave": "Active Slave"}


def _value(item: Any, name: str, default: Any = "") -> Any:
    if item is None:
        return default
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _plain(value: Any) -> Any:
    return getattr(value, "value", value)


def _enum(name: str, value: Any) -> Any:
    try:
        from .. import models
        return getattr(models, name)(value)
    except (ImportError, AttributeError, TypeError, ValueError):
        return value


def _safe_float(value: Any, default: float) -> float:
    try:
        result = float(value)
        return result if result > 0 else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        result = int(value)
        return result if result >= minimum else default
    except (TypeError, ValueError):
        return default


def _required_float(value: Any, label: str, *, minimum: float, strictly_greater: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not math.isfinite(result) or (result <= minimum if strictly_greater else result < minimum):
        comparison = ">" if strictly_greater else ">="
        raise ValueError(f"{label} must be {comparison} {minimum}")
    return result


def _required_int(value: Any, label: str, *, minimum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if isinstance(value, bool) or result < minimum or str(value).strip() != str(result):
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return result


def _coalesce_latest(items: Iterable[Any], key: Any) -> dict[Any, Any]:
    """Keep the newest item for each key without touching Tk widgets."""
    latest: dict[Any, Any] = {}
    for item in items:
        item_key = key(item)
        if item_key is not None:
            latest[item_key] = item
    return latest


def _frame_role(decoded: Any, direction: Any) -> str:
    """Classify a decoded frame as request/response for card evidence.

    Passive capture receives both sides of a physical exchange, so transport
    direction alone is not enough. Protocol adapters provide stable ``kind``
    values (``type2_request``, ``type2_response``, ``request``, ``response``);
    unknown/legacy values deliberately fall back to RX/TX direction.
    """

    kind = str(_plain(_value(decoded, "kind", ""))).strip().lower()
    if kind == "request" or kind.endswith("_request"):
        return "request"
    if kind == "response" or kind.endswith("_response"):
        return "response"
    direction_value = str(_plain(direction)).strip().lower()
    return "request" if direction_value in {"rx", "receive", "received"} else "response"


def _recover_frame_address(protocol: Any, raw: Any, known_addresses: Iterable[int]) -> int | None:
    """Recover an address only when the wire prefix identifies it safely.

    A parser error intentionally leaves ``decoded`` unset, but many malformed
    frames still retain their protocol prefix and address.  This helper uses
    only the fixed-width address portion of each protocol and intersects it
    with the configured inventory.  Ambiguous or unknown prefixes return
    ``None`` so callers cannot turn diagnostic traffic into a broadcast.
    """

    protocol_name = str(_plain(protocol)).strip().lower()
    try:
        wire = bytes(raw or b"")
    except (TypeError, ValueError):
        return None
    configured = {int(address) for address in known_addresses if isinstance(address, int) and not isinstance(address, bool)}
    if protocol_name == "partlow_p1160":
        if len(wire) < 3 or wire[:1] != b"L" or wire[-1:] != b"*":
            return None
        body = wire[1:-1]
        candidates: set[int] = set()
        for width in (1, 2):
            prefix = body[:width]
            if len(prefix) != width or not prefix.isdigit():
                continue
            address = int(prefix)
            if 1 <= address <= 99 and address in configured:
                candidates.add(address)
        return next(iter(candidates)) if len(candidates) == 1 else None
    if protocol_name == "mks_275":
        if len(wire) < 4 or wire[:1] not in {b"#", b"*", b"?"}:
            return None
        try:
            address = int(wire[1:3].decode("ascii"), 16)
        except (UnicodeDecodeError, ValueError):
            return None
        return address if address in configured else None
    return None


def _frame_device_id(frame: Any, devices: Iterable[Any], pending_device_id: Any = None, pending_protocol: Any = None) -> str | None:
    """Resolve one card for a frame, or ``None`` for raw/timeline-only data.

    Valid decoded addresses and worker-provided associations are authoritative.
    Parse errors may use a safely recoverable address.  Master fallback is
    limited to an explicitly pending device on the expected protocol; there is
    deliberately no protocol-wide fallback.
    """

    inventory = tuple(devices)
    protocol = _plain(_value(frame, "protocol", ""))
    protocol_name = str(protocol).strip().lower()
    known_ids = {str(_plain(_value(device, "device_id", ""))).lower() for device in inventory}
    supplied = _value(frame, "device_id", None)
    if supplied is not None:
        supplied_key = str(_plain(supplied)).lower()
        if supplied_key in known_ids:
            return supplied_key

    decoded = _value(frame, "decoded", None)
    address = _value(decoded, "address", None)
    if address is None:
        error = _value(frame, "error", None)
        raw = _value(error, "raw", None) if error is not None else None
        address = _recover_frame_address(protocol, raw or _value(frame, "raw_frame", b""), (
            _value(device, "address", -1)
            for device in inventory
            if str(_plain(_value(device, "protocol", ""))).strip().lower() == protocol_name
        ))
    if address is not None:
        for device in inventory:
            if (
                str(_plain(_value(device, "protocol", ""))).strip().lower() == protocol_name
                and _value(device, "address", None) == address
            ):
                return str(_plain(_value(device, "device_id", ""))).lower()

    # In Active Master the worker may attach the transaction target to a
    # malformed frame.  This UI-side fallback supports lightweight worker
    # doubles too, but only for the expected protocol and one known device.
    if _value(frame, "error", None) is not None and pending_device_id is not None:
        expected = str(_plain(pending_protocol)).strip().lower()
        if expected and expected == protocol_name:
            pending_key = str(_plain(pending_device_id)).lower()
            if pending_key in known_ids:
                return pending_key
    return None


if tk is None:  # pragma: no cover

    class YesRs485App:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("Tkinter is not available in this Python installation")

    def main() -> int:
        print("YES RS-485 requires a Python installation with Tkinter.", file=sys.stderr)
        return 2

else:

    class YesRs485App(tk.Tk):
        """Main window; operational notebook tabs are the mode authority."""

        def __init__(self, worker: Any | None = None, config: Any | None = None) -> None:
            super().__init__()
            self.title("YES RS-485 Engineering Utility")
            self.geometry("1240x820")
            self.minsize(980, 680)
            self.protocol("WM_DELETE_WINDOW", self._on_exit)
            self._worker = worker
            self._config = config
            self._config_load_failed = False
            self._session_id: Any = None
            self._session_running = False
            self._session_state = "CLOSED"
            self._worker_mode: str | None = None
            self._operational_mode = "passive"
            self._last_traffic_seq = 0
            self._last_frame_id = 0
            self._polling_id: Any = None
            self._closing = False
            self._runtime: dict[str, Any] = {}
            self._runtime_by_mode: dict[str, dict[str, Any]] = {}
            self._presentation_by_mode: dict[str, dict[str, dict[str, Any]]] = {}
            self._device_presentation: dict[str, dict[str, Any]] = {}
            self._raw_display_cache: deque[Any] = deque(maxlen=1000)
            self._timeline_display_cache: deque[Any] = deque(maxlen=500)
            self._raw_dropped_before_seq: Any = None
            self._aux_render_after: Any = None
            self._ui_perf_enabled = False
            self._pending_settings_reopen: dict[str, Any] | None = None
            self._load_optional_config()
            self._ensure_config()
            self._devices = tuple(self._config.devices)
            self._initialize_mode_runtime()
            self._setup_style()
            self._build_controls()
            self._build_views()
            self._apply_loaded_config()
            self._apply_mode_view()
            self._refresh_ports(False)
            self._polling_id = self.after(100, self._poll_worker)

        def _ensure_config(self) -> None:
            if self._config is not None:
                return
            from ..config import default_config
            self._config = default_config()

        @staticmethod
        def _mode_key(mode: Any) -> str:
            return str(_plain(mode or "passive")).strip().lower()

        def _new_presentation(self) -> dict[str, dict[str, Any]]:
            return {
                d.device_id.value: {
                    "values": dict(d.fake_values),
                    "online": False,
                    "observed": False,
                    "last_request": "Request —",
                    "last_response": "Response —",
                    "last_request_at": None,
                    "last_response_at": None,
                    "last_error": "",
                    "request_generation": 0,
                    "pending_request_generation": None,
                    "pending_request_deadline": None,
                    "result": None,
                    "result_error": False,
                }
                for d in self._devices
            }

        def _initialize_mode_runtime(self) -> None:
            self._runtime_by_mode = {mode: {d.device_id.value: dict(d.fake_values) for d in self._devices} for mode in ("passive", "master", "slave")}
            self._presentation_by_mode = {mode: self._new_presentation() for mode in ("passive", "master", "slave")}
            self._runtime = self._runtime_by_mode["passive"]
            self._device_presentation = self._presentation_by_mode["passive"]

        def _presentation_cache(self, mode: Any = None) -> dict[str, dict[str, Any]]:
            current_mode = mode if mode is not None else self.__dict__.get("_operational_mode", "passive")
            key = self._mode_key(current_mode)
            caches = self.__dict__.get("_presentation_by_mode")
            if caches is not None and key in caches:
                return caches[key]
            return self.__dict__.get("_device_presentation", {})

        def _load_optional_config(self) -> None:
            if self._config is not None:
                return
            try:
                from ..config import load_config
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    self._config = load_config()
                self._config_load_failed = any(issubclass(item.category, RuntimeWarning) for item in caught)
            except (ImportError, OSError, TypeError, ValueError):
                self._config = None

        def _setup_style(self) -> None:
            requested = str(_plain(getattr(getattr(self._config, "ui", None), "theme", "darkly"))).lower()
            requested = "darkly" if requested in {"", "dark", "system", "default"} else requested
            if requested not in {"darkly", "cyborg", "flatly", "light"}:
                requested = "darkly"
            self._theme_name = requested
            engine_theme = "flatly" if requested == "light" else requested
            style = None
            if tb is not None:
                try:
                    style = tb.Style(master=self, theme=engine_theme)
                except (TypeError, tk.TclError, RuntimeError):
                    try:
                        style = tb.Style(theme=engine_theme)
                    except (TypeError, tk.TclError, RuntimeError):
                        style = None
            self._style = style or ttk.Style(self)
            if style is None:
                try:
                    self._style.theme_use("clam")
                except tk.TclError:
                    pass
            self._style.configure("Title.TLabel", font=("Segoe UI", 14, "bold"))
            self._style.configure("Device.TLabelframe", relief="solid", borderwidth=1)
            self._style.configure("Device.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
            self._style.configure("CardValues.TLabel", foreground="#8d99a6", font=("Segoe UI", 8))
            self._style.configure("CardWarning.TLabel", foreground="#ffca62")
            self._style.configure("TNotebook.Tab", padding=(12, 7), font=("Segoe UI", 10, "bold"))
            self._style.map("TNotebook.Tab", foreground=[("selected", "#ffffff")], background=[("selected", "#286090"), ("active", "#3c5368")])
            self._style.configure("Status.TLabel", foreground="#67b7ff")
            self._style.configure("Muted.TLabel", foreground="#8d99a6")
            self._style.configure("Warning.TLabel", foreground="#ffca62", padding=4)
            self._style.configure("Badge.Passive.TLabel", foreground="#67d7c0", font=("Segoe UI", 10, "bold"), padding=(8, 4))
            self._style.configure("Badge.Master.TLabel", foreground="#ffca62", font=("Segoe UI", 10, "bold"), padding=(8, 4))
            self._style.configure("Badge.Slave.TLabel", foreground="#ff8f8f", font=("Segoe UI", 10, "bold"), padding=(8, 4))
            dark = requested in {"darkly", "cyborg"}
            self._style.configure("Treeview", background="#20262d" if dark else "#fff", fieldbackground="#20262d" if dark else "#fff", foreground="#edf2f7" if dark else "#20242a", rowheight=25)
            self._style.configure("Traffic.Treeview", font=("Consolas", 10))

        def _build_controls(self) -> None:
            outer = ttk.Frame(self, padding=(10, 8, 10, 0)); outer.pack(fill="x"); outer.columnconfigure(2, weight=1)
            self.mode_badge = tk.StringVar(value="PASSIVE · RX ONLY")
            self.mode_badge_widget = ttk.Label(outer, textvariable=self.mode_badge, style="Badge.Passive.TLabel"); self.mode_badge_widget.grid(row=0, column=0, sticky="w", padx=(0, 14))
            ttk.Label(outer, text="COM port").grid(row=0, column=1, sticky="w", padx=(0, 6))
            self.port = tk.StringVar(); self.port_combo = ttk.Combobox(outer, textvariable=self.port, width=20); self.port_combo.grid(row=0, column=2, sticky="ew", padx=(0, 6))
            ttk.Button(outer, text="Refresh", command=lambda: self._refresh_ports(True)).grid(row=0, column=3, padx=(0, 7))
            self.open_button = ttk.Button(outer, text="Open", command=self._open_session); self.open_button.grid(row=0, column=4, padx=(0, 4))
            self.close_button = ttk.Button(outer, text="Close", command=self._close_session, state="disabled"); self.close_button.grid(row=0, column=5)
            status_row = ttk.Frame(self, padding=(8, 4)); status_row.pack(fill="x", padx=10, pady=(2, 0)); ttk.Label(status_row, text="Session", style="Muted.TLabel").pack(side="left", padx=(0, 12)); self.session_status = tk.StringVar(value="CLOSED | Passive Sniffer | No COM"); ttk.Label(status_row, textvariable=self.session_status, style="Status.TLabel").pack(side="left", fill="x", expand=True)
            self.banner = tk.StringVar(value="Passive mode: RX only. No application writes, probes, or replies are permitted."); ttk.Label(self, textvariable=self.banner, style="Warning.TLabel", wraplength=1150).pack(fill="x", padx=10, pady=(5, 4))
            self.port.trace_add("write", self._on_selection_changed)

        def _build_views(self) -> None:
            self.notebook = ttk.Notebook(self); self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))
            self.passive_view = PassiveView(self.notebook, self._devices)
            self.master_view = MasterView(self.notebook, self._devices, on_poll=self._poll_device, on_poll_all=self._poll_all, on_continuous=self._continuous_poll)
            self.slave_view = SlaveView(self.notebook, self._devices, on_apply=self._apply_fake_values)
            self.settings_view = SettingsView(self.notebook, self._devices, on_save=self._save_settings)
            self.settings_view.theme.trace_add("write", lambda *_args: self._switch_theme(self.settings_view.theme.get()))
            self.raw_view = RawTrafficView(self.notebook); self.timeline_view = TimelineView(self.notebook)
            self.notebook.add(self.passive_view, text="MODE · PASSIVE"); self.notebook.add(self.master_view, text="MODE · ACTIVE MASTER"); self.notebook.add(self.slave_view, text="MODE · ACTIVE SLAVE"); self.notebook.add(self.settings_view, text="VIEW · SETTINGS"); self.notebook.add(self.raw_view, text="VIEW · RAW TRAFFIC"); self.notebook.add(self.timeline_view, text="VIEW · BUS TIMELINE")
            self.master_view.set_enabled(False); self.slave_view.set_enabled(False)
            self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        def _apply_loaded_config(self) -> None:
            config = self._config
            preferred = getattr(config, "preferred_port", None)
            if preferred is not None and getattr(preferred, "device", ""):
                self.port.set(preferred.device); self.settings_view.preferred_port.set(preferred.device)
            self.settings_view.timeout.set(str(getattr(config, "response_timeout_s", 1.0))); self.settings_view.raw_capacity.set(str(getattr(config, "raw_capacity", 10000))); self.settings_view.cycle_delay.set(str(getattr(config, "continuous_cycle_delay_s", 0.250)))
            self.settings_view.set_device_addresses(self._devices)
            passive = getattr(config, "passive_capture", None)
            if passive is not None:
                serial = getattr(passive, "serial", passive); self.settings_view.set_serial_values("passive", {"baudrate": getattr(serial, "baudrate", 9600), "bytesize": getattr(serial, "bytesize", 7), "parity": getattr(serial, "parity", "E"), "stopbits": getattr(serial, "stopbits", 1)}); self.settings_view.pairing_window.set(str(getattr(passive, "response_pairing_window_s", 0.250)))
            for protocol, serial in (("mks_275", next((d.serial for d in self._devices if _plain(d.protocol) == "mks_275"), None)), ("partlow_p1160", next((d.serial for d in self._devices if _plain(d.protocol) == "partlow_p1160"), None))):
                if serial is not None: self.settings_view.set_serial_values(protocol, {"baudrate": serial.baudrate, "bytesize": serial.bytesize, "parity": serial.parity, "stopbits": serial.stopbits})
            ui = getattr(config, "ui", None)
            if ui is not None:
                self.settings_view.theme.set(str(getattr(ui, "theme", "darkly"))); self.settings_view.show_hex.set(bool(getattr(ui, "show_hex", True))); self.settings_view.timeline_window.set(str(getattr(ui, "timeline_window_s", 30.0))); self.raw_view.set_show_hex(self.settings_view.show_hex.get()); self.timeline_view.set_window_s(float(self.settings_view.timeline_window.get()))
            self.settings_view.set_save_status("Saved")

        def _switch_theme(self, theme: str) -> None:
            requested = str(theme or "darkly").lower(); requested = "flatly" if requested == "light" else requested
            try: self._style.theme_use(requested); self._theme_name = "light" if requested == "flatly" else requested
            except tk.TclError: pass

        def _mode_for_tab(self) -> str | None:
            current = self.notebook.select()
            return {str(self.passive_view): "passive", str(self.master_view): "master", str(self.slave_view): "slave"}.get(current)

        def _visible_operational_view(self) -> Any | None:
            mode = self._mode_for_tab()
            return {"passive": self.passive_view, "master": self.master_view, "slave": self.slave_view}.get(mode)

        def _tab_is_visible(self, widget: Any) -> bool:
            return str(self.notebook.select()) == str(widget)

        def _sync_visible_devices(self, device_ids: Iterable[str] | None = None) -> None:
            """Render canonical device state only on the selected dashboard."""
            view = self._visible_operational_view()
            if view is None:
                return
            cache = self._presentation_cache(self._mode_for_tab() or self._operational_mode)
            keys = device_ids if device_ids is not None else cache.keys()
            for key in keys:
                state = cache.get(str(key).lower())
                if state is not None:
                    view.sync_device_state(str(key), state)

        def _queue_auxiliary_render(self) -> None:
            """Populate a newly selected traffic view outside the tab callback."""
            if not (self._tab_is_visible(self.raw_view) or self._tab_is_visible(self.timeline_view)):
                return
            if self._aux_render_after is not None:
                return
            self._aux_render_after = self.after_idle(self._render_visible_auxiliary)

        def _render_visible_auxiliary(self) -> None:
            self._aux_render_after = None
            if self._tab_is_visible(self.raw_view):
                records = tuple(self._raw_display_cache)
                # The cache retains the most recent 1000 records, but the
                # first expose should stay lightweight.  Once the table has
                # rows, add_records' sequence cursor admits only new records.
                if not self.raw_view.has_rows():
                    records = records[-300:]
                self.raw_view.add_records(records, self._raw_dropped_before_seq)
            elif self._tab_is_visible(self.timeline_view):
                frames = tuple(self._timeline_display_cache)
                if not self.timeline_view.has_rows():
                    frames = frames[-250:]
                self.timeline_view.add_frames(frames)

        def _on_tab_changed(self, _event: Any = None) -> None:
            started = time.perf_counter() if self._ui_perf_enabled else 0.0
            mode = self._mode_for_tab()
            if mode is None:
                self._apply_mode_view()
                self._queue_auxiliary_render()
                if self._ui_perf_enabled:
                    print(f"[UI-PERF] tab {self.notebook.tab(self.notebook.select(), 'text')}: {(time.perf_counter() - started) * 1000.0:.1f} ms")
                return
            changed = mode != self._operational_mode; self._operational_mode = mode; self._apply_mode_view()
            self._sync_visible_devices()
            if changed and self._session_running and self._worker is not None:
                try: self._worker.set_mode(_enum("BusMode", mode)); self._set_mode_status(f"Switching to {_MODE_LABELS[mode]}…")
                except Exception as exc: self._set_mode_status(f"Mode switch rejected: {exc}")
            if self._ui_perf_enabled:
                print(f"[UI-PERF] tab {_MODE_LABELS.get(mode, mode)}: {(time.perf_counter() - started) * 1000.0:.1f} ms")

        def _apply_mode_view(self) -> None:
            mode = self._operational_mode
            labels = {"passive": ("PASSIVE · RX ONLY", "Badge.Passive.TLabel", "Passive mode: RX only. No application writes, probes, or replies are permitted. Capture uses one selected UART framing."), "master": ("ACTIVE MASTER", "Badge.Master.TLabel", "WARNING — Active Master can transmit. Disconnect the PLC/controller before opening."), "slave": ("ACTIVE SLAVE", "Badge.Slave.TLabel", "WARNING — Active Slave can reply. Disconnect the real sensors before opening. Listen uses one selected UART framing; one UART cannot hear 7E1 and 8N1 simultaneously.")}
            badge, style, banner = labels[mode]; self.mode_badge.set(badge); self.mode_badge_widget.configure(style=style); self.banner.set(banner); self._update_session_status()
            # Hidden dashboard card trees do not need per-cycle state/control
            # writes.  Their controls are synchronized when that page is
            # selected, while the currently visible operational page remains
            # immediately accurate.
            visible = self._visible_operational_view()
            if visible is self.master_view:
                self.master_view.set_enabled(self._session_running)
            elif visible is self.slave_view:
                self.slave_view.set_enabled(self._session_running)
            self._sync_view_session_status()

        def _on_selection_changed(self, *_args: Any) -> None: self._update_session_status()
        def _update_session_status(self) -> None: self.session_status.set(f"{self._session_state} | {_MODE_LABELS[self._operational_mode]} | {self.port.get().strip() or 'No COM'}"); self.settings_view.set_context(self._operational_mode)
        def _set_session_state(self, state: Any) -> None:
            self._session_state = str(_plain(state)).strip().lower().upper()
            self._update_session_status()
            self._sync_view_session_status()

        def _sync_view_session_status(self) -> None:
            state = self._session_state.lower()
            if state == "running":
                statuses = {
                    "passive": "Port open · Passive session running · no traffic yet",
                    "master": "Port open · Master session ready",
                    "slave": "Port open · Slave emulator ready",
                }
            elif state == "opening":
                statuses = {key: "Opening serial session…" for key in _MODE_LABELS}
            elif state == "closing":
                statuses = {key: "Closing serial session…" for key in _MODE_LABELS}
            elif state in {"faulted"}:
                statuses = {key: "Port unavailable · session faulted" for key in _MODE_LABELS}
            else:
                statuses = {
                    "passive": "Port closed · no traffic captured",
                    "master": "Port closed · open an Active Master session to poll",
                    "slave": "Port closed · open an Active Slave session to apply values",
                }
            mode = self._mode_for_tab()
            visible = self._visible_operational_view()
            if mode is not None and visible is not None:
                visible.set_status(statuses[mode])

        def _sync_session_controls(self) -> None:
            state = self._session_state.lower()
            busy = state in {"opening", "running", "closing"}
            self._session_running = state == "running"
            self.open_button.configure(state="disabled" if busy else "normal")
            self.close_button.configure(state="normal" if busy else "disabled")
            self.port_combo.configure(state="disabled" if busy else "normal")
            self._apply_mode_view()
        def _set_mode_status(self, text: str) -> None:
            view = self._visible_operational_view()
            if view is not None:
                view.set_status(text)

        def _refresh_ports(self, show_errors: bool = True) -> None:
            try:
                from serial.tools import list_ports
                values = [getattr(item, "device", str(item)) for item in list_ports.comports()]
            except (ImportError, OSError) as exc:
                if show_errors: self._show_error(f"Unable to enumerate COM ports: {exc}")
                return
            self.port_combo["values"] = tuple(values)
            if values and not self.port.get(): self.port.set(values[0])
            if not values and show_errors: self._set_mode_status("No COM ports found. Connect the adapter, then Refresh.")

        def _build_devices(self) -> tuple[Any, ...]:
            from ..models import DeviceConfig, DeviceId, ProtocolId, SerialFormat, CANONICAL_DEVICE_IDS
            addresses = self.settings_view.partlow_address_values(); gp_address = self.settings_view.mks_address_value(); slave_values = self.slave_view.fake_values(); result = []
            for original_id in CANONICAL_DEVICE_IDS:
                key = original_id.value; protocol = ProtocolId.MKS_275 if key == "gp" else ProtocolId.PARTLOW_P1160; source = next(d for d in self._devices if d.device_id is original_id); address = gp_address if key == "gp" else addresses.get(int(key.split("_")[-1]), source.address); serial_values = self.settings_view.serial_values(protocol.value); serial = SerialFormat(**serial_values); values = dict(source.fake_values); values.update(slave_values.get(key, {})); result.append(DeviceConfig(DeviceId(key), protocol, address, serial, values))
            return tuple(result)

        def _build_spec(self) -> Any:
            from ..models import PassiveCaptureSettings, SerialFormat, SessionSpec
            if not self.port.get().strip(): raise ValueError("Select a COM port first")
            devices = self._build_devices(); mode = _enum("BusMode", self._operational_mode)
            # Passive and slave roles are RX/listen roles on one UART.  Use the
            # explicit capture profile for both; unlike master mode, neither
            # role can change framing per device while receiving.
            if self._operational_mode in {"passive", "slave"}:
                listen = SerialFormat(**self.settings_view.passive_values()["serial"])
            else:
                listen = devices[0].serial
            return SessionSpec(mode=mode, port=self.port.get().strip(), devices=devices, listen_serial=listen, response_timeout_s=_safe_float(self.settings_view.timeout.get(), 1.0), raw_capacity=_safe_int(self.settings_view.raw_capacity.get(), 10000), continuous_cycle_delay_s=max(0.0, float(self.settings_view.cycle_delay.get() or 0.250)))

        def _ensure_worker(self) -> Any | None:
            if self._worker is not None: return self._worker
            try:
                from ..serial_worker import SerialWorker
                self._worker = SerialWorker(); self._worker.start(); return self._worker
            except Exception as exc:
                self._set_session_state("FAULTED"); self._show_error(f"Unable to start SerialWorker: {exc}"); return None

        def _open_session(self) -> None:
            try: spec = self._build_spec()
            except (TypeError, ValueError) as exc: self._show_error(str(exc)); return
            worker = self._ensure_worker()
            if worker is None: return
            try:
                worker.start(); self._session_id = worker.open(spec)
            except Exception as exc:
                self._set_session_state("FAULTED"); self._show_error(f"Could not open the session: {exc}"); return
            self._set_session_state("OPENING"); self._sync_session_controls(); self._set_mode_status("Opening serial session…")

        def _close_session(self) -> None:
            if self._pending_settings_reopen is not None:
                self._set_mode_status("Settings save is restarting the serial session…")
                return
            if self._worker is not None and self._session_id is not None:
                try: self._worker.close()
                except Exception as exc:
                    self._set_mode_status(f"Close request failed: {exc}")
                    self._reset_session_controls()
                    return
                # SerialWorker reports CLOSING and then IDLE. Keep the UI in
                # that state until the worker confirms the transition so the
                # header, buttons, and view footers cannot disagree.
                self._set_session_state("CLOSING")
                self._sync_session_controls()
                return
            self._reset_session_controls()
            self._set_mode_status("Port closed")

        def _reset_session_controls(self) -> None:
            self._session_id = None; self._session_running = False; self._worker_mode = None
            for cache in self._presentation_by_mode.values():
                for presentation in cache.values():
                    presentation["online"] = False
            self._set_session_state("CLOSED"); self._sync_session_controls(); self._sync_visible_devices()

        def _apply_saved_config(self, config: Any) -> None:
            """Apply a validated, persisted config without rebuilding the UI.

            Runtime observations are intentionally retained for devices whose
            identity and wire configuration did not change.  A changed
            address/protocol/UART mapping gets a fresh presentation snapshot
            so old evidence cannot be mistaken for observations of the new
            device mapping.
            """
            old_devices = {d.device_id.value: d for d in self._devices}
            self._config = config
            self._devices = tuple(config.devices)
            refreshed = self._new_presentation()
            for mode in ("passive", "master", "slave"):
                cache = self._presentation_by_mode.setdefault(mode, {})
                runtime = self._runtime_by_mode.setdefault(mode, {})
                for device in self._devices:
                    key = device.device_id.value
                    previous = old_devices.get(key)
                    changed = previous is None or (
                        previous.address != device.address
                        or previous.protocol != device.protocol
                        or previous.serial != device.serial
                    )
                    if changed:
                        cache[key] = dict(refreshed[key])
                        runtime[key] = dict(device.fake_values)
                    else:
                        cache.setdefault(key, dict(refreshed[key]))
                        runtime.setdefault(key, dict(device.fake_values))

            self.settings_view.set_device_addresses(self._devices)
            for view in (self.passive_view, self.master_view, self.slave_view):
                apply_devices = getattr(view, "apply_devices", None)
                if apply_devices is not None:
                    apply_devices(self._devices)
            ui = getattr(config, "ui", None)
            if ui is not None:
                self.settings_view.theme.set(str(getattr(ui, "theme", self.settings_view.theme.get())))
                self.settings_view.show_hex.set(bool(getattr(ui, "show_hex", self.settings_view.show_hex.get())))
                self.settings_view.timeline_window.set(str(getattr(ui, "timeline_window_s", self.settings_view.timeline_window.get())))
                self.raw_view.set_show_hex(self.settings_view.show_hex.get())
                self.timeline_view.set_window_s(float(self.settings_view.timeline_window.get()))
            self._sync_visible_devices()

        def _reopen_after_settings_save(self) -> None:
            """Open the replacement session after the worker reports IDLE."""
            pending = self._pending_settings_reopen
            if pending is None:
                return
            pending["phase"] = "opening"
            self.port.set(str(pending["port"]))
            self._operational_mode = str(pending["mode"])
            try:
                spec = self._build_spec()
                worker = self._ensure_worker()
                if worker is None:
                    raise RuntimeError("SerialWorker is unavailable")
                self._session_id = worker.open(spec)
                self._set_session_state("OPENING")
                self._sync_session_controls()
                self._set_mode_status("Saved · connection restarting...")
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                self._pending_settings_reopen = None
                self._session_id = None
                self._set_session_state("FAULTED")
                self._sync_session_controls()
                self.settings_view.set_save_status(f"Save failed: reopen failed: {exc}")

        def _save_settings(self) -> None:
            """Validate, persist, and optionally restart only the serial session."""
            if self._pending_settings_reopen is not None:
                self.settings_view.set_save_status("Save failed: connection restart already in progress")
                return
            self.settings_view.set_save_status("Saving...")
            try:
                # Build the complete candidate before touching the live
                # worker.  Validation failures therefore leave a good session
                # and the existing file untouched.
                candidate = self._snapshot_config()
                from ..config import save_config
                save_config(candidate)
            except (ImportError, OSError, TypeError, ValueError) as exc:
                self.settings_view.set_save_status(f"Save failed: {exc}")
                return

            self._apply_saved_config(candidate)
            state = self._session_state.lower()
            # CLOSING is already a terminal user-requested transition.  Do
            # not turn a save during that brief window into a reopen that was
            # never requested by the user; the session should stay closed.
            session_open = self._session_id is not None and state in {"opening", "running"}
            if not session_open:
                self._pending_settings_reopen = None
                self.settings_view.set_save_status("Saved")
                return
            current_port = self.port.get().strip()
            current_mode = self._operational_mode
            self._pending_settings_reopen = {"mode": current_mode, "port": current_port, "phase": "closing"}
            try:
                continuous = getattr(getattr(self, "master_view", None), "continuous", None)
                if self._worker is not None and continuous is not None and bool(continuous.get()):
                    self._worker.stop_continuous_poll()
                    self.master_view.set_continuous(False)
                if self._worker is None:
                    raise RuntimeError("SerialWorker is unavailable")
                self.settings_view.set_save_status("Saved · connection restarting...")
                self._worker.close()
                self._set_session_state("CLOSING")
                self._sync_session_controls()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                self._pending_settings_reopen = None
                self.settings_view.set_save_status(f"Save failed: connection close failed: {exc}")


        def _poll_device(self, device_id: str) -> None:
            if not self._session_running or self._operational_mode != "master" or self._worker is None: self.master_view.set_status("Open an Active Master session first."); return
            try: self.master_view.set_status(f"Poll queued for {device_id}: {self._worker.poll(_enum('DeviceId', device_id))}")
            except Exception as exc: self.master_view.set_status(f"Poll rejected: {exc}")

        def _poll_all(self) -> None:
            if self._session_running and self._operational_mode == "master" and self._worker is not None:
                try: self.master_view.set_status(f"Poll-all queued: {self._worker.poll_all_once()}")
                except Exception as exc: self.master_view.set_status(f"Poll-all rejected: {exc}")
            else: self.master_view.set_status("Open an Active Master session first.")

        def _continuous_poll(self, active: bool) -> None:
            if self._worker is None or not self._session_running or self._operational_mode != "master":
                self.master_view.set_status("Open an Active Master session first.")
                self.master_view.set_continuous(False)
                return
            try: self._worker.start_continuous_poll() if active else self._worker.stop_continuous_poll()
            except Exception as exc:
                self.master_view.set_continuous(False)
                self.master_view.set_status(f"Continuous scan rejected: {exc}")

        def _apply_fake_values(self, device_id: str, values: Mapping[str, Any]) -> None:
            if self._worker is None or not self._session_running or self._operational_mode != "slave": self.slave_view.set_status("Open an Active Slave session first."); return
            try: self._worker.replace_fake_values(_enum("DeviceId", device_id), values); self.slave_view.set_status(f"Values queued for {device_id}")
            except Exception as exc: self.slave_view.set_status(f"Values rejected: {exc}")

        def _poll_worker(self) -> None:
            if self._closing: return
            if self._worker is not None:
                try:
                    self._consume_notifications(self._worker.drain_notifications(limit=300))
                    batch = self._worker.drain_traffic(self._last_traffic_seq, limit=400)
                    records = tuple(_value(batch, "records", ()) or ())
                    if records:
                        self._raw_display_cache.extend(records)
                    dropped_before = _value(batch, "dropped_before_seq", None)
                    if dropped_before not in (None, "", 0):
                        self._raw_dropped_before_seq = dropped_before
                    self._last_traffic_seq = max([self._last_traffic_seq] + [int(_value(r, "seq", 0)) for r in records])
                    if self._tab_is_visible(self.raw_view):
                        self.raw_view.add_records(records, dropped_before)

                    batch = self._worker.drain_frames(self._last_frame_id, limit=400)
                    frames = tuple(_value(batch, "frames", ()) or ())
                    if frames:
                        self._timeline_display_cache.extend(frames)
                    self._last_frame_id = max([self._last_frame_id] + [int(_value(f, "frame_id", 0)) for f in frames])
                    if self._tab_is_visible(self.timeline_view):
                        self.timeline_view.add_frames(frames)
                    self._consume_frames(frames)
                except Exception as exc: self._set_mode_status(f"Worker polling error: {exc}")
            self._polling_id = self.after(100, self._poll_worker)

        def _record_evidence(self, device_id: Any, evidence: str, *, mode: Any = None, event_time: Any = None, request: bool = False, response: bool = False, error: bool = False) -> str | None:
            key = str(_plain(device_id)).lower()
            cache = self._presentation_cache(mode)
            state = cache.get(key)
            if state is None:
                return None
            state["last_event_monotonic"] = time.monotonic()
            state["last_error"] = state.get("last_error", "")
            if request:
                generation = int(state.get("request_generation", 0)) + 1
                state["request_generation"] = generation
                state["pending_request_generation"] = generation
                state["pending_request_deadline"] = time.monotonic() + 0.900
                state["last_request"] = f"Request {evidence}"
                state["last_request_at"] = event_time
                state["last_error"] = ""
            if response:
                state["last_response"] = f"Response {evidence}"
                state["last_response_at"] = event_time
                state["pending_request_generation"] = None
                state["pending_request_deadline"] = None
                state["last_error"] = ""
            if error:
                state["last_error"] = f"Error {evidence}"
                state["pending_request_generation"] = None
                state["pending_request_deadline"] = None
            state["observed"] = True
            return key

        def _consume_frames(self, frames: Iterable[Any]) -> None:
            touched_by_mode: dict[str, set[str]] = {}
            for frame in frames:
                decoded = _value(frame, "decoded", None); error = _value(frame, "error", None)
                target = _frame_device_id(frame, self._devices)
                if target is None:
                    # Unknown-address malformed/ambiguous traffic remains
                    # visible in Raw Traffic and Bus Timeline only.
                    continue
                text = _value(decoded, "kind", "frame") if decoded else ("malformed" if error else "frame")
                direction = _plain(_value(frame, "direction", "rx"))
                role = _frame_role(decoded, direction)
                mode = self._mode_key(_value(frame, "mode", "passive"))
                if error is not None and mode == "passive" and role == "request":
                    pending = self._presentation_cache(mode).get(target, {}).get("pending_request_generation")
                    if pending is not None:
                        # An undecodable RX frame has no grammar-based role.
                        # If this device is awaiting a passive request reply,
                        # treat the attributable malformed frame as that
                        # response so its event timestamp is preserved.
                        role = "response"
                recorded = self._record_evidence(
                    target,
                    str(text),
                    mode=mode,
                    event_time=_value(frame, "wall_time_utc", None),
                    request=role == "request",
                    response=role == "response",
                    error=bool(error),
                )
                if recorded is not None:
                    touched_by_mode.setdefault(mode, set()).add(recorded)
            current_mode = self._mode_key(self._mode_for_tab() or self._operational_mode)
            self._sync_visible_devices(touched_by_mode.get(current_mode, set()))

        def _consume_notifications(self, notifications: Iterable[Any] | None) -> None:
            touched_by_mode: dict[str, set[str]] = {}
            events = tuple(notifications or ())
            latest_device_states = _coalesce_latest(
                events,
                lambda item: (self._mode_key(_value(item, "mode", "passive")), str(_plain(_value(item, "device_id", ""))).lower())
                if type(item).__name__ == "DeviceStateChanged"
                else None,
            )
            for notification in events:
                name = type(notification).__name__; detail = _value(notification, "detail", _value(notification, "reason", _value(notification, "exception_summary", "")))
                if name == "StateChanged":
                    old_state = str(_plain(_value(notification, "old_state", ""))).strip().lower()
                    state = str(_plain(_value(notification, "new_state", ""))).strip().lower()
                    self._set_session_state(state)
                    self._sync_session_controls()
                    if state == "running" and self._pending_settings_reopen is not None and self._pending_settings_reopen.get("phase") == "opening":
                        self._pending_settings_reopen = None
                        self.settings_view.set_save_status("Saved · connection reopened")
                    if state in {"faulted", "stopped"} and self._pending_settings_reopen is not None:
                        self._pending_settings_reopen = None
                        self.settings_view.set_save_status(f"Save failed: reopen failed: {detail or state}")
                    # Ignore the worker's initial NEW→IDLE notification when
                    # an open command is already queued.  Only a terminal
                    # transition (or a failed OPENING transition) retires the
                    # command/session identity used by Close.
                    if state in {"stopped", "faulted"} or (state == "idle" and old_state in {"opening", "closing", "faulted"}):
                        self._session_id = None
                        for cache in self._presentation_by_mode.values():
                            for presentation in cache.values():
                                presentation["online"] = False
                        self._sync_visible_devices()
                        if state == "idle" and old_state == "closing" and self._pending_settings_reopen is not None:
                            self._reopen_after_settings_save()
                elif name == "ModeChanged":
                    self._worker_mode = str(_plain(_value(notification, "new_mode", ""))); self._set_mode_status(f"Mode active: {_MODE_LABELS.get(self._worker_mode, self._worker_mode)}")
                elif name == "ModeChangeFailed": self._set_mode_status(f"Mode switch failed: {detail}")
                elif name == "DeviceStateChanged":
                    # Applied below once per device after the notification
                    # batch, so hidden card trees never see intermediate
                    # snapshots from the same UI cycle.
                    continue
                elif name == "TransactionStarted":
                    target = _value(notification, "device_id", ""); key = str(_plain(target)).lower(); presentation = self._presentation_cache("master").get(key)
                    if presentation is not None:
                        presentation["result"] = "Request in flight…"; presentation["result_error"] = False; touched_by_mode.setdefault("master", set()).add(key)
                    if self._mode_for_tab() == "master": self.master_view.set_target(target, "request in flight")
                elif name == "TransactionCompleted":
                    target = _value(notification, "device_id", ""); key = str(_plain(target)).lower(); presentation = self._presentation_cache("master").get(key)
                    if presentation is not None:
                        presentation["result"] = f"Response {_value(notification, 'result', 'ok')}"; presentation["result_error"] = False; touched_by_mode.setdefault("master", set()).add(key)
                    if self._mode_for_tab() == "master": self.master_view.set_target(target, "reply received")
                elif name == "TransactionFailed":
                    target = _value(notification, "device_id", ""); key = str(_plain(target)).lower(); failure = detail or _value(notification, "failure_kind", "failed"); presentation = self._presentation_cache("master").get(key)
                    if presentation is not None:
                        presentation["result"] = f"No response: {failure}"; presentation["result_error"] = True; touched_by_mode.setdefault("master", set()).add(key)
                    if self._mode_for_tab() == "master": self.master_view.set_target(target, f"failure: {failure}")
                elif name == "PollCycleStarted":
                    if self._mode_for_tab() == "master": self.master_view.set_cycle(f"started ({_value(notification, 'total', 12)} devices)")
                elif name == "PollCycleCompleted":
                    failed = _value(notification, "failed", 0)
                    if self._mode_for_tab() == "master":
                        self.master_view.set_cycle(f"complete: {_value(notification, 'succeeded', 0)} ok / {failed} failed")
                        self.master_view.set_target("—", f"cycle failures: {failed}")
                elif name == "ContinuousPollChanged":
                    if self._mode_for_tab() == "master": self.master_view.set_continuous(bool(_value(notification, "active", False)))
                elif name == "SlaveRequestIgnored":
                    protocol = _plain(_value(notification, "protocol", "")); address = _value(notification, "address", None)
                    target = next((d.device_id.value for d in self._devices if _plain(d.protocol) == protocol and d.address == address), None)
                    if target:
                        recorded = self._record_evidence(target, str(detail or "request ignored"), mode="slave", error=True)
                        if recorded is not None: touched_by_mode.setdefault("slave", set()).add(recorded)
                    self._set_mode_status(f"Slave request ignored: {detail}")
                elif name == "Faulted":
                    if self._pending_settings_reopen is not None:
                        self._pending_settings_reopen = None
                        self.settings_view.set_save_status(f"Save failed: reopen failed: {detail or 'serial worker fault'}")
                    self._reset_session_controls(); self._set_session_state("FAULTED"); self._set_mode_status(f"Serial worker fault: {detail}")
                elif name in {"CommandRejected", "CommandAccepted"} and detail: self._set_mode_status(str(detail))
            for key, notification in latest_device_states.items():
                mode, device_key = key
                snapshot = _value(notification, "state_snapshot", None)
                self._runtime_by_mode.setdefault(mode, {})[device_key] = snapshot
                presentation = self._presentation_cache(mode).get(device_key)
                if presentation is not None:
                    presentation["values"] = dict(_value(snapshot, "values", {}) or {})
                    presentation["online"] = self._session_running
                    presentation["observed"] = True
                    touched_by_mode.setdefault(mode, set()).add(device_key)
            current_mode = self._mode_key(self._mode_for_tab() or self._operational_mode)
            self._sync_visible_devices(touched_by_mode.get(current_mode, set()))

        def _snapshot_config(self) -> Any:
            from ..models import AppConfig, DeviceConfig, PassiveCaptureSettings, PortIdentity, UiPreferences, SerialFormat
            devices = self._build_devices(); preferred = getattr(self._config, "preferred_port", None); port_name = self.settings_view.preferred_port.get().strip() or self.port.get().strip()
            if port_name: preferred = PortIdentity(port_name, getattr(preferred, "vid", None), getattr(preferred, "pid", None), getattr(preferred, "serial_number", None), getattr(preferred, "description", None))
            passive_values = self.settings_view.passive_values(); passive = PassiveCaptureSettings(serial=SerialFormat(**passive_values["serial"]), response_pairing_window_s=_required_float(passive_values["response_pairing_window_s"], "Pairing window", minimum=0.0, strictly_greater=True))
            ui_values = self.settings_view.ui_values(); ui = UiPreferences(theme=ui_values["theme"], show_hex=ui_values["show_hex"], timeline_window_s=_required_float(ui_values["timeline_window_s"], "Timeline window", minimum=0.0, strictly_greater=True))
            return AppConfig(schema_version=2, preferred_port=preferred, devices=devices, passive_capture=passive, response_timeout_s=_required_float(self.settings_view.timeout.get(), "Response timeout", minimum=0.0, strictly_greater=True), raw_capacity=_required_int(self.settings_view.raw_capacity.get(), "Raw capacity", minimum=1), continuous_cycle_delay_s=_required_float(self.settings_view.cycle_delay.get(), "Cycle delay", minimum=0.0), ui=ui)

        def _save_config(self) -> None:
            if self._config_load_failed: return
            try:
                from ..config import save_config
                self._config = self._snapshot_config(); save_config(self._config)
            except (ImportError, OSError, TypeError, ValueError) as exc: print(f"Could not save YES_RS485 configuration: {exc}", file=sys.stderr)

        def _show_error(self, text: str) -> None:
            if messagebox is not None: messagebox.showerror("YES RS-485", text, parent=self)
            else: self._set_mode_status(text)

        def _on_exit(self) -> None:
            self._closing = True
            if self._polling_id:
                try: self.after_cancel(self._polling_id)
                except tk.TclError: pass
            try:
                self._close_session()
                if self._worker is not None and hasattr(self._worker, "stop"): self._worker.stop()
                self._save_config()
            finally: self.destroy()


    def main() -> int:
        app = YesRs485App(); app.mainloop(); return 0


__all__ = ["YesRs485App", "main"]
