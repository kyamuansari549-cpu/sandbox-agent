#!/usr/bin/env python3
"""Toy MCP server over stdio (newline-delimited JSON-RPC 2.0).

The documented example for SandboxAgent's MCP support. Stdlib only —
no dependencies to install.

Run it with::

    python examples/mcp_echo_server.py

Exposes a single tool, ``echo``: "Echoes back the input text", with
inputSchema ``{type: object, properties: {text: {type: string}},
required: [text]}``. Replies to ``tools/call`` with
``content: [{type: "text", text: "Echo: <text>"}]``.

Handles: ``initialize`` (replies with protocolVersion + serverInfo),
``notifications/initialized`` (no reply), ``tools/list``, ``tools/call``.
Unknown methods get JSON-RPC error -32601 (method not found).
"""

import json
import sys


def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _reply(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code, message):
    msg = {"jsonrpc": "2.0", "error": {"code": code, "message": message}}
    if rid is not None:
        msg["id"] = rid
    _send(msg)


def _handle(msg):
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        _reply(
            rid,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mcp-echo", "version": "1.0"},
            },
        )
    elif method == "notifications/initialized":
        pass  # notification: no reply
    elif method == "tools/list":
        _reply(
            rid,
            {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echoes back the input text",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ]
            },
        )
    elif method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") != "echo":
            _error(rid, -32602, f"unknown tool: {params.get('name')}")
            return
        text = (params.get("arguments") or {}).get("text", "")
        _reply(rid, {"content": [{"type": "text", "text": f"Echo: {text}"}]})
    else:
        _error(rid, -32601, f"method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue  # malformed input: skip, keep serving
        if isinstance(msg, dict):
            _handle(msg)


if __name__ == "__main__":
    main()
