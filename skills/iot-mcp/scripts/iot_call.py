#!/usr/bin/env python3
"""iot_call.py — minimal MCP client for an IoT-MCP server over Streamable HTTP.

Stdlib only. Use it when the agent has no MCP client attached, or when you want a
single reproducible request to debug a server with.

    ./iot_call.py tools                       # dump every tool + its input schema
    ./iot_call.py capabilities                # what this machine can actually do
    ./iot_call.py call list_windows           # read-only call
    ./iot_call.py call open_url '{"url":"https://example.com"}'
    ./iot_call.py call close_app '{"name":"foot"}' --confirm   # red tier needs --confirm

Exit codes: 0 ok, 1 transport/RPC error, 2 usage, 3 refused without --confirm.
"""

import argparse
import json
import subprocess
import sys
import tempfile

# Tiered by what they do to a real desktop. Keep in sync with SKILL.md.
READ_ONLY = {
    "list_actions", "list_apps", "list_windows", "media_status",
}
MUTATING = {
    "open_app", "open_url",                 # starts things; returns immediately
    "media_play_pause", "media_next", "media_previous",
}
DESTRUCTIVE = {
    "close_app", "focus_window",            # kills a process tree / steals focus
}
CONFIRM_REQUIRED = MUTATING | DESTRUCTIVE

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "iot-call.py", "version": "1.0.0"}


class Conn:
    """One MCP Streamable HTTP session. Every message is a POST to the same URL."""

    def __init__(self, url, timeout=30):
        self.url = url
        self.timeout = timeout
        self.session_id = None
        self._hdr = tempfile.NamedTemporaryFile(delete=False)
        self._body = tempfile.NamedTemporaryFile(delete=False)

    def post(self, message):
        cmd = [
            "curl", "-sS", "-m", str(self.timeout),
            "-D", self._hdr.name, "-o", self._body.name,
            "-H", "Content-Type: application/json",
            "-H", "Accept: application/json, text/event-stream",
        ]
        if self.session_id:
            cmd += ["-H", "mcp-session-id: " + self.session_id]
        # --data-binary, not --data: --data strips newlines from the payload.
        cmd += ["-X", "POST", self.url, "--data-binary", json.dumps(message)]
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode != 0:
            raise RuntimeError(
                "curl failed (%d): %s\nIs the server running, and is the port open in the "
                "firewall? See SKILL.md 'Cannot connect'." % (done.returncode, done.stderr.strip())
            )
        raw = open(self._body.name).read()
        for line in open(self._hdr.name):
            if line.lower().startswith("mcp-session-id:"):
                self.session_id = line.split(":", 1)[1].strip()
        return self._decode(raw)

    @staticmethod
    def _decode(raw):
        """Servers answer with an SSE-framed body; the JSON is on a 'data:' line."""
        data = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
        text = data[-1] if data else raw.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError("non-JSON response: %s" % text[:400])

    def open(self):
        init = self.post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        })
        if "error" in init:
            raise RuntimeError("initialize failed: %s" % json.dumps(init["error"]))
        if not self.session_id:
            raise RuntimeError("no mcp-session-id header; the server cannot associate requests")
        # The server will not serve tools until it has seen this notification.
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return init

    def call(self, method, params=None, _id=2):
        r = self.post({"jsonrpc": "2.0", "id": _id, "method": method,
                       "params": params or {}})
        if "error" in r:
            raise RuntimeError("RPC error: %s" % json.dumps(r["error"], indent=2))
        return r["result"]


def unwrap(result):
    """tools/call wraps the real payload as JSON inside content[].text. Unpack it."""
    if not isinstance(result, dict):
        return result
    out = []
    for block in result.get("content", []):
        if block.get("type") != "text":
            out.append(block)
            continue
        try:
            out.append(json.loads(block["text"]))
        except json.JSONDecodeError:
            out.append(block["text"])
    if result.get("isError"):
        print("!! tool returned isError: true", file=sys.stderr)
    return out[0] if len(out) == 1 else out


def show(value):
    print(json.dumps(value, indent=2) if not isinstance(value, str) else value)


def main():
    ap = argparse.ArgumentParser(prog="iot_call.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000/mcp",
                    help="MCP endpoint; the path is always /mcp")
    ap.add_argument("--timeout", type=int, default=30)
    sub = ap.add_subparsers(dest="mode", required=True)

    sub.add_parser("tools", help="list every tool with its input schema")
    sub.add_parser("capabilities", help="report supported automation capabilities")
    p = sub.add_parser("call", help="call one tool")
    p.add_argument("tool")
    p.add_argument("args", nargs="?", default="{}",
                   help="JSON object of arguments, e.g. '{\"name\":\"foot\"}'")
    p.add_argument("--confirm", action="store_true",
                   help="acknowledge that this tool changes the remote machine")
    ns = ap.parse_args()

    if ns.mode == "call" and ns.tool in CONFIRM_REQUIRED and not ns.confirm:
        tier = "DESTRUCTIVE" if ns.tool in DESTRUCTIVE else "mutating"
        print(
            "refused: %s is a %s tool — it changes the state of a real desktop someone "
            "may be using.\nRe-read its description with `tools`, get the human's explicit "
            "go-ahead, then re-run with --confirm." % (ns.tool, tier), file=sys.stderr)
        return 3

    try:
        c = Conn(ns.url, ns.timeout)
        c.open()
        if ns.mode == "tools":
            for t in sorted(c.call("tools/list")["tools"], key=lambda x: x["name"]):
                schema = t.get("inputSchema", {})
                props = schema.get("properties", {})
                required = set(schema.get("required", []))
                args = ", ".join(
                    "%s%s:%s" % (k, "*" if k in required else "", v.get("type", "?"))
                    for k, v in props.items()) or "(no arguments)"
                tier = ("read-only" if t["name"] in READ_ONLY
                        else "DESTRUCTIVE" if t["name"] in DESTRUCTIVE else "mutating")
                print("%s  [%s]\n    args: %s\n    %s\n"
                      % (t["name"], tier, args,
                         (t.get("description") or "(no description)").strip()))
        elif ns.mode == "capabilities":
            show(unwrap(c.call("tools/call", {"name": "list_actions", "arguments": {}})))
        else:
            try:
                arguments = json.loads(ns.args)
            except json.JSONDecodeError as exc:
                print("arguments must be a valid JSON object: %s" % exc, file=sys.stderr)
                return 2
            if not isinstance(arguments, dict):
                print("arguments must be a JSON object", file=sys.stderr)
                return 2
            show(unwrap(c.call("tools/call", {"name": ns.tool, "arguments": arguments})))
        return 0
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
