# SH-U22C / Windows serial assumptions

Research date: 2026-10-08

## Scope and confidence

The checkout contains no application source, adapter manual, device photo, USB
descriptor, or Windows Device Manager export. A web search also found no
manufacturer datasheet or manual for the exact product name **SH-U22C**. The
name may be a typo or a vendor-specific rebrand. The similarly named DSD TECH
SH-U11C is a different, documented product; its specifications must not be
applied to an SH-U22C without confirming the label and USB identity:

- [DSD TECH SH-U11C official product page](https://www.deshide.com/product-details_1663389.html)
- [DSD TECH RS485 product list](https://www.deshide.com/RS485-Series.html)

Therefore the implementation must treat SH-U22C hardware details as
unverified. In particular, do not infer its chipset, connector pinout,
galvanic isolation, biasing, termination, maximum baud rate, or direction
control from the model name.

## Facts useful to the Windows/PySerial design

- Once a compatible USB-serial driver is installed, Windows exposes the
  adapter as a virtual `COMx` port. PySerial's
  [`list_ports.comports()`](https://pyserial.readthedocs.io/en/latest/tools.html)
  can enumerate the port and, for USB devices, may expose description, VID/PID,
  serial number, and location in `ListPortInfo`. The UI should show these
  fields and let the user select a port; it should not hard-code `COM3` or
  assume that a COM number remains stable after reconnecting.
- PySerial's [`Serial`](https://pyserial.readthedocs.io/en/latest/pyserial_api.html)
  configures the byte stream with `baudrate`, `bytesize`, `parity`, `stopbits`,
  read/write timeouts, and flow-control flags. The adapter should be treated as
  a transparent bridge: these settings belong to the attached controller's
  protocol, not to the USB converter. Use bytes, not text decoding, in the
  transport layer.
- Unless a target manual requires it, start with `xonxoff=False`,
  `rtscts=False`, and `dsrdtr=False`. There is no evidence that the SH-U22C
  exposes or needs modem-control handshaking.
- The two documented target profiles in this project already require different
  settings: Partlow P1160 ASCII is 7E1 (default 4800 baud), while the MKS 275
  default is 8N1 at 19,200 baud. The adapter must not cause the application to
  force 8N1, and the application must not emulate parity or 7-bit framing in
  user-space; configure the Windows serial port explicitly.

## RS-485 half-duplex and direction control

The expected bus is two-wire, half-duplex, but the exact adapter behavior is
unknown. USB-RS485 products commonly either perform automatic transmit/receive
turnaround in hardware or require a driver/RTS signal to control the
transceiver. Do not assume which applies here.

PySerial documents `Serial.rs485_mode` as platform/device dependent; on
Windows the supported mode is limited to RTS-on-transmit. See the
[PySerial RS-485 API notes](https://pyserial.readthedocs.io/en/latest/pyserial_api.html#rs485-support).
Do not enable `rs485_mode`, toggle RTS, or add arbitrary delays until the
actual adapter's driver and direction behavior have been identified. If the
adapter is auto-turnaround, software RTS changes could be ineffective or
harmful. If it is RTS-controlled, a dedicated, tested configuration is needed.

Regardless of adapter behavior, serialize one bus transaction at a time:
write one request, keep the receiver enabled, collect the complete response
or timeout, then allow the next request. Protocol-specific timing still
applies: the Partlow note requires a three-character quiet interval and line
release timing; the MKS manual states that its driver turns off about 80 us
after the response. The worker should therefore use the protocol's framing and
timeout rules and record actual raw bytes/timestamps rather than assuming that
`write()` returning means the RS-485 line has already gone idle.

The adapter's echo behavior is also unverified. A passive/sniffer mode using a
single converter may see local transmit echo, may suppress it, or may not
observe the other direction reliably. Expose this as a hardware limitation and
validate it with a known two-node test before promising complete bus capture.

## Physical wiring and deployment checks

Before enabling active traffic, verify all of the following on the physical
unit or its manufacturer documentation:

1. Terminal labels and polarity (`A/B`, `D+/D-`, or another convention).
2. Whether the interface is 2-wire RS-485 or a selectable RS-422/RS-485 unit.
3. Presence and switch/jumper state of 120-ohm termination and fail-safe bias.
4. Whether the USB and field sides are galvanically isolated.
5. Whether a signal reference/ground is required and whether any terminal
   supplies field power (never assume a USB converter's auxiliary terminal is
   safe to connect to a controller).
6. Driver identity and USB VID/PID as shown in Device Manager/PySerial.
7. Direction/echo behavior at the actual Partlow and MKS serial settings.

The application can safely provide COM-port enumeration, explicit serial
format fields, raw traffic capture, and diagnostics without knowing those
hardware properties. It should label isolation, termination, direction, and
polarity as **unverified** until the exact SH-U22C is identified.

## Recommended verification procedure

1. Photograph or transcribe the adapter's label and terminals; record its
   Device Manager Hardware IDs and the PySerial `ListPortInfo` fields.
2. With no field device attached, open the selected COM port and run a local
   loopback only if the adapter's wiring and electrical levels make that test
   safe; otherwise use a second known-good RS-485 converter.
3. Test both project profiles with a known responder. Confirm framing,
   response delimiter, turnaround, and whether transmitted bytes appear in the
   receive stream.
4. Only after that test should the worker choose native `rs485_mode`, RTS
   direction control, or any adapter-specific delay.

