"""IoT-MCP: OS automation exposed as MCP tools over Streamable HTTP with mDNS discovery.

Computer B runs :func:`iot_mcp.app.build_server` behind :mod:`iot_mcp.cli`; computer A finds it
with :mod:`iot_mcp.discovery` and drives it through the MCP SDK client.

The layering is strict, and it is the reason this package is testable:

``drivers/``  →  knows nothing about MCP; returns JSON dicts or raises :class:`DriverError`
``app.py``    →  thin tool wrappers that delegate to a driver and nothing else
``cli.py``    →  wiring: transport, bind address, discovery advertisement
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
