# YES RS-485 engineering utility

Small Windows/Tkinter utility for inspecting and exercising the documented
ASCII protocols used by Partlow P1160 controllers and the MKS Series 275
RS-485 Mini-Convectron variant.  It is an engineering/debug tool, not a
control-system component.

## Safety first

The application has three explicit modes:

* **Passive Sniffer** is receive-only.  It does not send probes or replies.
* **Active Master** can transmit requests.  The PLC/controller must be
  physically disconnected, and the non-persisted safety acknowledgement must
  be checked every time a session is opened.
* **Active Slave** can reply to valid addressed requests.  Real sensors must
  be physically disconnected, and the non-persisted safety acknowledgement
  must be checked every time a session is opened.

Closing a session clears the safety acknowledgement.  The selected mode,
protocol, and serial framing are fixed for the lifetime of an open session.

The SH-U22C adapter is not identified by a verified manufacturer manual in
this repository.  Direction control, echo behavior, polarity, termination,
biasing, isolation, pinout, and even exact 7E1 support remain **unverified**.
Do not enable RTS/pyserial RS-485 direction control based only on the product
name.  Confirm the label, USB identity, wiring, and behavior on a safe bench
setup first.

## Run from a checkout

Python 3.11 or newer with Tkinter is required by the package on Windows.  Install the
small runtime dependencies and launch the module:

```powershell
py -m pip install -e .
py -m yes_rs485
```

The UI uses `ttkbootstrap` when available for its dark/light themes and falls
back to the platform's stock `ttk` theme when it is unavailable (including
minimal or headless environments).  Theme selection is persisted, and changes
apply immediately when the active Tk theme supports runtime switching.

If pyserial is absent, the UI remains importable and reports a clear message
when COM-port enumeration or a session is attempted.  A normal installation
includes it automatically.

## Serial defaults

* Partlow P1160 ASCII machine profile: 9,600 baud, 7 data bits, even parity,
  1 stop bit (7E1).  All eleven Partlow controllers share this bus profile;
  their PID addresses are persisted independently (initially 2 through 12).
* MKS/GP 275 machine profile: 9,600 baud, 8 data bits, no parity, 1 stop bit
  (8N1).  The documented alternatives are 7E1 and 7O1; the factory default
  documented by MKS is 19,200 baud 8N1, so the application's initial machine
  value must not be read as a claim that the physical gauge was reconfigured.

Passive capture is a separate profile and starts at 9,600 baud with the
`mixed_raw` strategy.  Its capture UART framing is not presented as the
physical device framing, and the SH-U22C 8N1 capture/parity-recovery behavior
remains unverified until tested on the actual adapter.

The Settings tab keeps these concerns separate: General preferences, Passive
Capture (bus baud and response pairing window), the shared PARTLOW BUS profile
with PID 1–11 address controls, and the independent MKS/GP GAUGE profile.  An
Advanced/Custom framing card accepts 7/8 data bits, None/Even/Odd parity, and
1/2 stop bits.  A non-blocking warning marks formats that are not documented;
these controls only change the application's UART settings and never write a
device configuration.

Flow control is disabled by default.  The application records raw bytes as
both escaped ASCII and hexadecimal, retaining split/coalesced reads as raw
traffic plus decoded frame events.  Host response timeouts are configurable;
they are not claims about a device's internal timing.

## Development notes

Tk owns the main thread and a single `SerialWorker` owns the serial handle.
Views never access pyserial directly: they submit immutable operations and
render worker notifications, raw records, and frame events.  Protocol details
and timing assumptions are documented in [`docs/research`](docs/research).

Useful checks from the checkout:

```powershell
py -m compileall src
py -m pytest
```

Use a loopback or known-good responder only after checking the converter's
electrical levels, A/B polarity, termination, and direction behavior.  A
single converter may not provide analyzer-grade visibility of both directions
in passive mode.
