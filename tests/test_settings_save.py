"""Small headless checks for the Settings save/apply lifecycle."""

from types import SimpleNamespace

from yes_rs485.ui.app import YesRs485App


class _Status:
    def __init__(self):
        self.values = []

    def set_save_status(self, value):
        self.values.append(value)


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def test_save_closed_persists_and_stays_closed(monkeypatch):
    persisted = []
    monkeypatch.setattr("yes_rs485.config.save_config", persisted.append)
    app = SimpleNamespace(
        _pending_settings_reopen=None,
        _session_state="CLOSED",
        _session_id=None,
        settings_view=_Status(),
        _snapshot_config=lambda: "candidate",
        _apply_saved_config=lambda config: setattr(app, "applied", config),
    )

    YesRs485App._save_settings(app)

    assert persisted == ["candidate"]
    assert app.applied == "candidate"
    assert app._session_id is None
    assert app.settings_view.values[-1] == "Saved"


def test_save_open_closes_then_reopens_asynchronously(monkeypatch):
    monkeypatch.setattr("yes_rs485.config.save_config", lambda _config: None)
    calls = []

    class Worker:
        def close(self):
            calls.append("close")

        def open(self, spec):
            calls.append(("open", spec))
            return "new-session"

    worker = Worker()
    app = SimpleNamespace(
        _pending_settings_reopen=None,
        _session_state="RUNNING",
        _session_id="old-session",
        _operational_mode="passive",
        _worker=worker,
        port=_Var("COM7"),
        settings_view=_Status(),
        _snapshot_config=lambda: "candidate",
        _apply_saved_config=lambda _config: None,
        _set_session_state=lambda state: setattr(app, "_session_state", state),
        _sync_session_controls=lambda: None,
        _build_spec=lambda: "new-spec",
        _ensure_worker=lambda: worker,
        _set_mode_status=lambda _text: None,
    )

    YesRs485App._save_settings(app)

    assert calls == ["close"]
    assert app._session_state == "CLOSING"
    assert app._pending_settings_reopen["phase"] == "closing"
    assert app.settings_view.values[-1] == "Saved · connection restarting..."

    YesRs485App._reopen_after_settings_save(app)

    assert calls == ["close", ("open", "new-spec")]
    assert app._session_id == "new-session"
    assert app._pending_settings_reopen["phase"] == "opening"


def test_invalid_save_does_not_touch_live_session_or_file(monkeypatch):
    persisted = []
    monkeypatch.setattr("yes_rs485.config.save_config", persisted.append)
    app = SimpleNamespace(
        _pending_settings_reopen=None,
        _session_state="RUNNING",
        _session_id="live-session",
        settings_view=_Status(),
        _snapshot_config=lambda: (_ for _ in ()).throw(ValueError("bad timeout")),
    )

    YesRs485App._save_settings(app)

    assert persisted == []
    assert app._session_state == "RUNNING"
    assert app._session_id == "live-session"
    assert app.settings_view.values[-1] == "Save failed: bad timeout"
