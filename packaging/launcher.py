"""PyInstaller launcher that preserves the normal package entry point."""

from yes_rs485.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
