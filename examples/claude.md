# Connecting Claude to an IoT-MCP server

Replace `192.168.0.54` with the address the server printed (or the address
`iot-mcp-client discover` found). The path is always `/mcp`.

The quickstart server uses one shared bearer token. Put it in Claude's environment before
starting or configuring the client. Do not put it in the URL: IoT-MCP only accepts the query
form when the operator explicitly enabled `--allow-query-token`.

```sh
export IOT_MCP_TOKEN="$(cat ~/.config/iot-mcp/token)"
```

If the server was started without authentication, omit the `headers` entries shown below.

## Claude Code

Native Streamable HTTP support, so no bridge is needed. The shell expands `$IOT_MCP_TOKEN`,
and Claude Code stores the resulting header in its local configuration:

```sh
claude mcp add --transport http iot-os http://192.168.0.54:8000/mcp \
  --header "Authorization: Bearer $IOT_MCP_TOKEN"
```

Check it, and look for the core OS tools plus any operator-enabled `browser_*` tools:

```sh
claude mcp list
claude mcp get iot-os
```

`claude mcp add` writes to your user config by default. To commit the connection with a
project, add `--scope project`, which maintains a `.mcp.json` next to the repo root. Claude
Code expands `${IOT_MCP_TOKEN}` from the environment when it loads the project file:

```json
{
  "mcpServers": {
    "iot-os": {
      "type": "http",
      "url": "http://192.168.0.54:8000/mcp",
      "headers": {
        "Authorization": "Bearer ${IOT_MCP_TOKEN}"
      }
    }
  }
}
```

Keep the token out of the committed file. If Claude reports `Needs authentication` or
`Failed to connect` with HTTP 401, check that the variable is set in the shell that starts
Claude Code; an unset variable is not expanded.

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
      "url": "http://192.168.0.54:8000/mcp",
      "headers": {
        "Authorization": "Bearer <your IoT-MCP token>"
      }
    }
  }
}
```

Desktop does not reliably inherit a token from your shell, so put the actual token in the
local config file if you use this form. Give the file restrictive permissions and treat it as
a secret.

If it only offers a stdio transport (older Desktop builds), bridge it with `mcp-remote`,
which turns the local stdio pipe into an HTTP client. This form keeps the token in the
desktop configuration's environment block and avoids spaces inside `args`, which some hosts
mangle:

```json
{
  "mcpServers": {
    "iot-os": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://192.168.0.54:8000/mcp",
        "--allow-http",
        "--header",
        "Authorization:${IOT_MCP_AUTH_HEADER}"
      ],
      "env": {
        "IOT_MCP_AUTH_HEADER": "Bearer <your IoT-MCP token>"
      }
    }
  }
}
```

`--allow-http` is required because the server speaks plain HTTP. It exists to stop people
pointing an MCP client at `http://` by accident; see the security section of the README
before using it anywhere you do not control the wire. HTTP plus a bearer token protects
against casual LAN use but not an attacker who can observe the network — use a tunnel or
TLS reverse proxy across untrusted networks.

## If the tools do not appear

1. From the machine running Claude, run
   `IOT_MCP_TOKEN_FILE=~/.config/iot-mcp/token examples/smoke-test.sh http://192.168.0.54:8000/mcp`.
   That isolates network, authentication, and protocol problems from client configuration.
2. HTTP 401 means the server is reachable and rejecting the credential. Check the server's
   token, restart Claude after changing the environment/config, and make sure a proxy is not
   stripping `Authorization`.
3. A `421 Misdirected Request` / "Invalid Host header" means the server is validating the
   `Host` header against the address you used. Either connect by the name it expects or
   start the server with `--allow-host 192.168.0.54` (or `--expose`, which turns the check
   off and says so loudly).
4. The server binds `127.0.0.1` by default, so a remote client gets connection-refused, not
   an MCP error. That needs `--expose` on the server.
