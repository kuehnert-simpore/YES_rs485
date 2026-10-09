# Partlow P1160 ASCII protocol — implementation handoff

## Source and scope

Primary source: Partlow Plus Series Controller Full Manual, 59321 Issue 5 (March
2005), Section 13 “ASCII Communications”, pp. 98–102, and the P1160/P1800/P1400
communications parameter table, pp. 50–54. Public copy:
<https://www.grievecorp.com/wp-content/uploads/2019/12/PARTLOW-PLUS-SERIES-CONTROLLER-FULL-MANUAL.pdf>

This note is limited to the P1160 controller application layer. The manual says
ASCII is a legacy option and recommends Modbus for new designs; the P1160 table
also says bit parameters are not applicable to ASCII.

## Physical layer and addressing

- Half-duplex RS-485; the master initiates every exchange and the addressed
  controller replies.
- ASCII settings documented for this model family: 1200, 2400, 4800 (default),
  9600, or 19,200 bit/s; even parity; 7 data bits and 1 stop bit (7E1).
- Controller address is configured in Configuration Mode, 1–99 (default 1).
  Address 1–9 may be one digit (`7`) or zero-padded (`07`). A controller ignores
  frames for another address.
- Wait at least 3 character times after the last received character before
  transmitting; release the line within 3 character times after the last
  transmitted character. The manual gives 1.5 ms (19,200), 3 ms (9,600), 6 ms
  (4,800), 12 ms (2,400), and 24 ms (1,200).

The published P1160 ASCII frames contain no CRC/BCC/checksum field. Framing is
ASCII text terminated by `*` (0x2A); no CR/LF is specified and spaces are
forbidden. Start character `L` (0x4C) identifies controllers (`R`, 0x52, is for
profilers and is not needed for P1160).

## Common frame grammar

All fields are ASCII characters. `N` is the 1–99 slave address, `P` is the
single-character parameter identifier, and `DATA` is the five-character numeric
field described below.

| Type | Master request | Successful reply | Use |
|---|---|---|---|
| 1 | `L N ? ? *` | `L N ? A *` | Presence/active probe; no reply means inactive. |
| 2 | `L N P C *` | `L N P DATA A *` | Query or +/- adjustment. `C` is `?` (read), `+` (increment), or `-` (decrement). |
| 3 | `L N P # DATA *` | `L N P DATA I *` | Stage an absolute write; `#` is 0x23 and `I` means ready. |
| 4 | `L N P I *` | `L N P DATA A *` | Commit the immediately preceding valid Type 3 write. |

Remove the spaces shown in the grammar. Type 3 does not change the controller
immediately; the master must follow with Type 4 for the same slave/parameter.
Type 4 without an immediately preceding Type 3 is ignored.

`DATA` is five ASCII digits. The final digit encodes sign and decimal places:

| DATA suffix | Meaning |
|---|---|
| `0` | positive, no decimal places (`+abcd`) |
| `1` | positive, one decimal (`+abc.d`) |
| `2` | positive, two decimals (`+ab.cd`) |
| `3` | positive, three decimals (`+a.bcd`) |
| `5` | negative, no decimal places (`-abcd`) |
| `6` | negative, one decimal (`-abc.d`) |
| `7` | negative, two decimals (`-ab.cd`) |
| `8` | negative, three decimals (`-a.bcd`) |

The first four characters (`abcd`) carry the digits; the final character carries
the sign/decimal-format code. Type 2 replies contain a five-character `DATA`
field. A negative acknowledgement uses `N` instead of `A`; its `DATA` is
indeterminate.

## P1160 application identifiers

The P1160/P1800/P1400 table (manual pp. 50–54) maps these controller parameters
to ASCII identifiers. “Type 2” means readable/queryable; “Type 3/4” means
absolute writes are supported through the two-step handshake. Read-only rows
must not be written.

| Identifier | Parameter | Access / message types |
|---|---|---|
| `M` | Process variable (PV) | RO, Type 2 |
| `S` | Selected setpoint | R/W (RO when remote SP selected), Type 2 and Type 3/4 |
| `W` | Output power | RO unless manual; Type 2 and Type 3/4 |
| `V` | Deviation (PV − SP) | RO, Type 2 |
| `U` | Secondary proportional band | R/W, Type 2 and Type 3/4 |
| `P` | Primary proportional band | R/W, Type 2 and Type 3/4 |
| `1` | Direct/reverse acting | R/W, Type 2 and Type 3/4 |
| `I` | Automatic reset / loop-alarm time | R/W, Type 2 and Type 3/4 |
| `D` | Rate (derivative time) | R/W, Type 2 and Type 3/4 |
| `N` | Output 1 cycle time | R/W, Type 2 and Type 3/4 |
| `H`, `G` | Scale lower / upper limit | R/W, Type 2 and Type 3/4 |
| `C`, `E` | Alarm 1 / Alarm 2 value | R/W, Type 2 and Type 3/4 |
| `J` | Manual reset / bias | R/W, Type 2 and Type 3/4 |
| `K` | Overlap/deadband | R/W, Type 2 and Type 3/4 |
| `F` | On/off differential | R/W, Type 2 and Type 3/4 |
| `Q` | Decimal-point position | R/W, Type 2 and Type 3/4 (may be RO for non-linear input) |
| `O` | Output 2 cycle time | R/W, Type 2 and Type 3/4 |
| `B` | Primary output power limit | R/W, Type 2 and Type 3/4 |
| `A`, `T` | Setpoint upper / lower limit | R/W, Type 2 and Type 3/4 |
| `^` | Setpoint ramp rate | R/W, Type 2 and Type 3/4 |
| `m` | Input filter time constant | R/W, Type 2 and Type 3/4 |
| `v` | Process-value offset | R/W, Type 2 and Type 3/4 |
| `[` , `\\` | Retransmit output maximum / minimum | R/W, Type 2 and Type 3/4 |
| `~` | Remote-setpoint offset | R/W, Type 2 and Type 3/4 |

Additional useful P1160 rows in the same table:

- `L` (Type 2, RO) is the controller status word. The documented status bits
  include alarm 1 (bit 0), alarm 2 (bit 1), self-tune (bit 2), change indicator
  (bit 3), communications-write enable (bit 4), auto/manual control enable
  (bit 5), pre-tune (bit 7), and loop alarm (bit 8); the manual's bit polarity
  is parameter-specific and should be preserved when decoding.
- `]` (Type 2, RO) is the scan table. Its reply packs actual setpoint, PV,
  primary power, secondary power, and controller status as five-character
  fields: `L{N}]25aaaaabbbbbcccccdddddeeeeeA*` (the `25` is the documented
  dual-output data-length marker; single-output variants may use their model's
  corresponding length marker).
- `Z` is the controller-command parameter and only accepts Type 3/4. Valid
  five-digit DATA values are `00010` manual on, `00020` automatic, `00030`
  self-tune on, `00040` self-tune off, `00050` request pre-tune, `00060` abort
  pre-tune, `00130` loop alarm on, and `00140` loop alarm off.

The table contains some parameters without an ASCII identifier (for example
actual setpoint and setpoint-select rows); do not invent identifiers for those.

## Errors and state-machine implications

- Parity error, syntax error, timeout, or Type 4 without a preceding Type 3 is
  ignored with no response; the receiver waits for the next start character.
- A correctly framed request that cannot be fulfilled (unsupported/read-only
  parameter, invalid value, or out-of-range value) receives a negative
  acknowledgement (`...N*`) with indeterminate DATA.
- For a Type 2 PV/deviation read outside range, the documented reply contains
  `<??>0` for over-range or `<??>5` for under-range before the normal `A*`.
  Sensor-break PV is documented as “n/a” in the P1160 table.
- No response timeout duration is specified in this manual. Treat timeout as a
  configurable host-side policy, and distinguish it from a valid negative ACK.

## Provenance pointers

- Manual PDF pp. 98–99 / extracted PDF pages 115–116: physical layer, address,
  message grammar, field definitions, numeric DATA encoding.
- pp. 100–101 / extracted PDF pages 117–118: Type 1–4 request/reply behavior,
  negative acknowledgements, PV range sentinel values.
- p. 102 / extracted PDF page 118: ignored-error conditions.
- pp. 50–54 / extracted PDF pages 67–71: P1160 parameter identifiers, access,
  ranges, controller status, scan table, and command values.
