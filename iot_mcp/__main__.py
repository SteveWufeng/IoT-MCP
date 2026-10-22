"""``python -m iot_mcp`` runs the server; see :mod:`iot_mcp.cli` for the flags."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
