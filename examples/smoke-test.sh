#!/usr/bin/env bash
# smoke-test.sh — drive an IoT-MCP server over raw HTTP with no MCP client involved.
#
# Every other client here needs a working MCP stack. When a server misbehaves you want the
# smallest thing that still speaks the protocol, so that "the server is down", "the server
# is reachable but rejecting my Host header" and "my client is misconfigured" are three
# different answers instead of one.
#
# Protocol notes (Streamable HTTP, as implemented by the MCP Python SDK):
#   * Everything is a POST to the single /mcp endpoint.
#   * Accept must offer both application/json and text/event-stream; the server answers with
#     an SSE-framed body, so each reply is read back out of a "data: {...}" line.
#   * The initialize response carries an `mcp-session-id` header, which must be echoed on
#     every later request.
#   * The server will not serve tools until it has seen notifications/initialized.
#
# Usage: examples/smoke-test.sh [URL]        default: http://127.0.0.1:8000/mcp
set -uo pipefail

URL="${1:-http://127.0.0.1:8000/mcp}"
TIMEOUT=15

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required" >&2
  exit 2
fi

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# post <json> — one JSON-RPC message to the endpoint. Prints "<headers>\n\n<body>" split into
# two files so the caller can read both the status line and the SSE payload.
HEADERS_FILE="$(mktemp)"
BODY_FILE="$(mktemp)"
trap 'rm -f "$HEADERS_FILE" "$BODY_FILE"' EXIT

post() {
  local payload="$1"
  shift
  curl -sS --max-time "$TIMEOUT" -D "$HEADERS_FILE" -o "$BODY_FILE" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    "$@" \
    -X POST "$URL" --data "$payload"
}

# Extract the JSON payload from an SSE body (or use the body as-is if it is bare JSON).
payload() {
  sed -n 's/^data: //p' "$BODY_FILE" | tail -1
  if [[ ! -s "$BODY_FILE" ]]; then
    return
  fi
  if ! grep -q '^data: ' "$BODY_FILE"; then
    cat "$BODY_FILE"
  fi
}

echo "== target: $URL"

# 1. initialize ----------------------------------------------------------------
post '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
  "protocolVersion":"2025-06-18","capabilities":{},
  "clientInfo":{"name":"iot-mcp-smoke-test","version":"0"}}}' ||
  fail "could not reach $URL (is 'iot-mcp' running, and is the port right?)"

STATUS=$(awk 'NR==1 {print $2}' "$HEADERS_FILE")
[[ "$STATUS" == "200" ]] || fail "initialize returned HTTP $STATUS: $(head -c 300 "$BODY_FILE")"

INIT=$(payload)
SESSION=$(sed -n 's/^[Mm]cp-[Ss]ession-[Ii]d: *//p' "$HEADERS_FILE" | tr -d '\r' | tail -1)
[[ -n "$INIT" ]] || fail "empty initialize body"
echo "ok: initialize -> $(echo "$INIT" | head -c 200)"
[[ -n "$SESSION" ]] || fail "no mcp-session-id header; requests cannot be associated"
echo "ok: session $SESSION"

# 2. notifications/initialized -------------------------------------------------
post '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  -H "mcp-session-id: $SESSION" >/dev/null || fail "could not send initialized"

# 3. tools/list ----------------------------------------------------------------
post '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  -H "mcp-session-id: $SESSION" || fail "tools/list request failed"

TOOLS=$(payload | tr ',' '\n' | sed -n 's/.*"name":"\([^"]*\)".*/\1/p' | sort -u | paste -sd' ' -)
[[ -n "$TOOLS" ]] || fail "tools/list returned no tools: $(head -c 300 "$BODY_FILE")"
echo "ok: tools -> $TOOLS"

# 4. one real call -------------------------------------------------------------
# list_actions is read-only on every platform, which makes it the safe probe.
post '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_actions","arguments":{}}}' \
  -H "mcp-session-id: $SESSION" || fail "tools/call request failed"

CALL=$(payload)
echo "$CALL" | grep -q '"ok":true' || fail "list_actions did not report ok: $(head -c 300 "$CALL")"
echo "ok: list_actions -> $(echo "$CALL" | head -c 200)"

echo
echo "PASS: $URL speaks MCP over Streamable HTTP"
