# Connecting Claude to an IoT-MCP server

Replace `192.168.0.54` with the address the server printed (or the address
`iot-mcp-client discover` found). The path is always `/mcp`.

## Claude Code

Native Streamable HTTP support, so no bridge is needed:

```sh
claude mcp add --transport http iot-os http://192.168.0.54:8000/mcp
```

Check it, and look for the eleven tools:

```sh
claude mcp list
claude mcp get iot-os
```

`claude mcp add` writes to your user config by default. To commit the connection with a
project, add `--scope project`, which maintains a `.mcp.json` next to the repo root:

```json
{
  "mcpServers": {
    "iot-os": {
      "type": "http",
      "url": "http://192.168.0.54:8000/mcp"
    }
  }
}
```

## Claude Desktop

Desktop takes a JSON file, restarted after editing:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

If your build accepts a remote server directly, this is all you need:

```json
{
  "mcpServers": {
    "iot-os": {
      "type": "http",
      "url": "http://192.168.0.54:8000/mcp"
    }
  }
}
```

If it only offers a stdio transport (older Desktop builds), bridge it with `mcp-remote`,
which turns the local stdio pipe into an HTTP client:

```json
{
  "mcpServers": {
    "iot-os": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://192.168.0.54:8000/mcp", "--allow-http"]
    }
  }
}
```

`--allow-http` is required because the server speaks plain HTTP. It exists to stop people
pointing an MCP client at `http://` by accident; see the security section of the README
before using it anywhere you do not control the wire.

## If the tools do not appear

1. From the machine running Claude, run `examples/smoke-test.sh http://192.168.0.54:8000/mcp`.
   That isolates network and protocol problems from client configuration.
2. A `421 Misdirected Request` / "Invalid Host header" means the server is validating the
   `Host` header against the address you used. Either connect by the name it expects or
   start the server with `--allow-host 192.168.0.54` (or `--expose`, which turns the check
   off and says so loudly).
3. The server binds `127.0.0.1` by default, so a remote client gets connection-refused, not
   an MCP error. That needs `--expose` on the server.
