# YES RS-485 engineering utility

Small Windows/Tkinter utility for inspecting and exercising one RS-485 bus
containing the documented ASCII protocols used by Partlow P1160 controllers
and the MKS Series 275 RS-485 Mini-Convectron variant.  It is an
engineering/debug tool, not a control-system component.

## Whole-bus operating model

One open session represents the complete bus.  The canonical inventory is
twelve devices: the MKS/GP gauge (`GP`) and eleven Partlow controllers
(`PID 1` through `PID 11`).  Each device has its own protocol, address, serial
format, and (for slave emulation) fake values.  There is no global protocol
selector and opening a mode does not require a separate arm.

The operational tabs own the current bus mode.  Switching between Passive
Sniffer, Active Master, and Active Slave updates the running worker and keeps
the same serial handle; any in-flight work is cancelled and framing state is
reset at the mode boundary.

* **Passive Sniffer** is receive-only.  It recognizes both Partlow and MKS
  frames and never sends probes, replies, or other application writes.  The
  one UART uses the selected listen framing, so it cannot hear 7E1 and 8N1 at
  the same time.
* **Active Master** can transmit to a selected device.  Use **Poll** for one
  device, **Poll All Once** for the twelve-device inventory, or **Continuous
  Scan** to repeat the inventory.  The worker applies the target device's
  protocol and serial profile at each transaction boundary.
* **Active Slave** listens for requests and replies only to valid addressed
  requests.  It provides twelve independent fake profiles, one for each
  canonical device, and sends no unsolicited frames.  It uses the same one
  selected listen UART as Passive Sniffer.

Active Master can transmit and Active Slave can reply; disconnect real
controllers/sensors during bench work as appropriate.  This is an operating
caution, not a persisted arm or an additional session gate.

The SH-U22C adapter is not identified by a verified manufacturer manual in
this repository.  Direction control, echo behavior, polarity, termination,
biasing, isolation, pinout, and even exact 7E1 support remain **unverified**.
Do not enable RTS/pyserial RS-485 direction control based only on the product
name.  Confirm the label, USB identity, wiring, and behavior on a safe bench
setup first.

## Python development

Python 3.11 or newer with Tkinter is required by the package on Windows.  Install the
small runtime dependencies and launch the module:

```powershell
# Run these commands from the repository root (the directory containing pyproject.toml).
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
python -m yes_rs485
```

For development checks, install the test runner into the same environment:

```powershell
python -m pip install pytest
python -m pytest -q
```

If PowerShell blocks activation for the current user, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then activate the
environment again.  If `python -m yes_rs485` reports `No module named
yes_rs485`, the editable install was not completed; make sure the command was
run as `python -m pip install -e .` with the final `.` while `.venv` is active.

The UI uses `ttkbootstrap` when available for its dark/light themes and falls
back to the platform's stock `ttk` theme when it is unavailable (including
minimal or headless environments).  Theme selection is persisted, and changes
apply immediately when the active Tk theme supports runtime switching.

If pyserial is absent, the UI remains importable and reports a clear message
when COM-port enumeration or a session is attempted.  A normal installation
includes it automatically.

## Quick start — Windows EXE

The repository can produce `YES_RS485.exe`, a self-contained Windows one-file
build with a GUI subsystem. To build and run it:

1. Clone or download this repository.
2. If building locally, install Python 3.11 or newer. Python is needed to
   build the EXE, not to run the finished EXE.
3. From the repository root, double-click `BUILD_EXE.bat`.
4. Wait for the batch file to display `BUILD COMPLETE`.
5. Find `YES_RS485.exe` in the repository root.
6. Double-click `YES_RS485.exe`.

The first build may take longer because it creates the isolated `.build-venv`
environment and installs the build dependencies. The generated EXE does not
require Python, a virtual environment, or a checkout on the target machine.
The USB/serial adapter's Windows driver is still required.

## Rebuilding the EXE

After source-code changes, double-click `BUILD_EXE.bat` again. The script
rebuilds the one-file executable in temporary `.build` output and replaces the
old root `YES_RS485.exe` only after the new build succeeds. If a build fails,
the previous root executable is left in place and the temporary build files are
retained for inspection.

For a manual build from the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[build]"
.\BUILD_EXE.bat
```

`BUILD_EXE.bat` invokes the tracked `packaging/YES_RS485.spec`. Its tiny
packaging launcher imports the same `yes_rs485.__main__.main()` entry point
used by normal Python execution; it does not duplicate application startup.
PyInstaller's intermediate files and staged
executable are written to a unique `%TEMP%\YES_RS485_build_*\dist` directory.
The root `YES_RS485.exe` is replaced only after PyInstaller succeeds and the
complete staged file has been copied successfully.  The temporary directory
is retained so a failed build can be inspected.  The spec explicitly includes
ttkbootstrap theme assets and pyserial's dynamic `serial.tools` modules; no
application source modules are changed by the packaging step.

The generated executable is ignored by Git. Do not commit the binary or the
temporary build directory. Run `YES_RS485.exe` from the repository root after
the build, or copy it to the intended test folder first. The EXE launches the
same `yes_rs485.__main__` application entry point as `python -m yes_rs485`.

## Shared configuration between Python and EXE

The Python launch and the packaged EXE intentionally use the same per-user
configuration file:

```text
%APPDATA%\YES_RS485\config.json
```

If Windows does not provide `APPDATA`, the application falls back to
`%USERPROFILE%\AppData\Roaming\YES_RS485\config.json`.  The file is not bundled
inside the executable and is not relative to the executable's folder.  This
means a setting saved while developing with `python -m yes_rs485` is visible
to the EXE, and vice versa, for the same Windows user account.  Keep a backup
before testing experimental framing or address changes, and avoid saving
settings from two running copies at the same time.  The file is schema-versioned
and validated on load; an invalid file is reported and the application uses
defaults rather than silently accepting malformed values.

## Unsigned Windows builds

Locally rebuilt executables are unsigned.  Windows SmartScreen, Defender, or
an email/download scanner may therefore warn about `YES_RS485.exe`, especially
when it was downloaded rather than built locally.  Treat that warning as a
provenance check: obtain the executable from a trusted source, compare its
checksum when one is supplied, or build it yourself from this checkout.  This
project does not provide a signing certificate or claim that an unsigned
binary has passed Microsoft's reputation checks.

## Serial defaults

* Partlow P1160 ASCII: 9,600 baud, 7 data bits, even parity, 1 stop bit
  (7E1).  PID 1–11 share this bus profile; their addresses are independent
  (defaults 2 through 12).
* MKS/GP Series 275: 9,600 baud, 8 data bits, no parity, 1 stop bit (8N1).
  The documented alternatives are 7E1 and 7O1.  MKS documentation also lists
  a 19,200 baud 8N1 factory setting; the application's 9,600 default is not a
  claim that a physical gauge was reconfigured.

Passive capture and Active Slave share one explicit listen profile, which
defaults to 9,600 baud 7E1.  The selected profile is applied to the single
UART while listening; Master retunes that same handle to each device profile
when it polls.  The SH-U22C adapter's direction control, echo behavior,
polarity, termination, biasing, isolation, pinout, and exact 7E1 support
remain **unverified** until tested on the actual unit.

The Settings tab keeps these concerns separate: General preferences, the
Passive Capture/Slave listen profile and pairing window, the shared PARTLOW
BUS profile with PID 1–11 address controls, and the independent MKS/GP GAUGE
profile.  An Advanced/Custom framing card accepts 7/8 data bits, None/Even/
Odd parity, and 1/2 stop bits.  A non-blocking warning marks formats that are
not documented; these controls only change the application's UART settings
and never write a device configuration.

Use **SAVE SETTINGS** to validate and persist changes immediately.  With an
open session, the application asynchronously closes and reopens the same COM
port in the current mode so new serial settings take effect without restarting
the application; with a closed port, it remains closed.

Flow control is disabled by default.  The application records raw bytes as
both escaped ASCII and hexadecimal, retaining split/coalesced reads as raw
traffic plus decoded frame events.  Host response timeouts are configurable;
they are not claims about a device's internal timing.

## Development notes

Tk owns the main thread and a single `SerialWorker` owns the serial handle.
Views never access pyserial directly: they submit immutable mode, poll, and
fake-value operations and render worker notifications, raw records, and frame
events.  Protocol details and timing assumptions are documented in
[`docs/research`](docs/research).

Configuration is schema 2.  It persists the complete twelve-device `devices`
array, the preferred port, the passive/listen UART profile, timing values, and
UI preferences.  It does not persist a selected global protocol, a single
protocol profile, a safety arm, or an open-port state.

Useful checks from the checkout:

```powershell
python -m compileall src
python -m pytest
```

Use a loopback or known-good responder only after checking the converter's
electrical levels, A/B polarity, termination, and direction behavior.  A
single converter may not provide analyzer-grade visibility of both directions
in passive mode.
