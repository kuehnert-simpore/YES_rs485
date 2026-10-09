"""YES RS-485 engineering utility.

The package deliberately keeps the graphical shell small.  Serial ownership,
protocol framing, and safety checks live in :mod:`yes_rs485.serial_worker` and
the protocol modules; the UI only submits commands and renders events.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
