"""MCP tool plug-in for the agent loop.

Discovers tools from the MCP servers configured in ``config.MCP_SERVERS``
and exposes them as Ollama-format tool schemas named
``mcp__<server>__<tool>``.

Plug-in convention (agreed with the backend-core worker): tools.py does
``from mcp_tools import mcp_schemas, call_mcp_tool`` inside
try/except ImportError, so this module must expose exactly those names.
``mcp_schemas()`` returns [] fast when no servers are configured (no
subprocess spawned) and never raises: broken servers are reported to
stderr and skipped, never breaking the others.
"""

import sys

import config
from mcp_client import MCPError, MCPServer

# name -> started MCPServer; built once by _discover(), cleared by reset.
_SERVERS = {}
# Cached Ollama-format schemas (None = not discovered yet).
_SCHEMAS = None


def _discover():
    """Start every configured server, list its tools, cache schemas.

    One broken server never breaks the others: its error goes to stderr
    and discovery continues with the next server.
    """
    global _SERVERS, _SCHEMAS
    # Stop anything from a previous discovery round before replacing it.
    for server in list(_SERVERS.values()):
        try:
            server.stop()
        except Exception:
            pass
    servers = {}
    schemas = []
    entries = config.MCP_SERVERS or []
    if not isinstance(entries, list):
        entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            print(
                "sandbox-agent: MCP server entry is not an object, "
                f"skipped: {entry!r}",
                file=sys.stderr,
            )
            continue
        name = entry.get("name")
        command = entry.get("command")
        args = entry.get("args") or []
        if not name or not command:
            print(
                "sandbox-agent: MCP server entry missing name/command, "
                f"skipped: {entry!r}",
                file=sys.stderr,
            )
            continue
        try:
            server = MCPServer(name, command, args)
            server.start(timeout=10)
            tools = server.list_tools(timeout=10)
            servers[name] = server
            for tool in tools:
                if not isinstance(tool, dict) or not tool.get("name"):
                    continue
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": f"mcp__{name}__{tool['name']}",
                            "description": tool.get("description") or "",
                            "parameters": tool.get("inputSchema")
                            or {"type": "object", "properties": {}},
                        },
                    }
                )
        except Exception as exc:  # MCPError, timeouts, spawn failures, ...
            print(
                f"sandbox-agent: MCP server '{name}' unavailable: {exc}",
                file=sys.stderr,
            )
    _SERVERS = servers
    _SCHEMAS = schemas
    return schemas


def _ensure_discovered():
    if _SCHEMAS is None:
        _discover()
    return _SCHEMAS


def mcp_schemas():
    """Ollama-format tool schemas for every MCP tool.

    [] when no servers are configured (returns fast, spawns nothing).
    Never raises.
    """
    try:
        return list(_ensure_discovered())
    except Exception as exc:
        print(f"sandbox-agent: MCP discovery failed: {exc}", file=sys.stderr)
        return []


def call_mcp_tool(name, args):
    """Dispatch an ``mcp__<server>__<tool>`` call.

    Never raises — failures come back as "ERROR: ..." strings, like every
    other tool. Unknown tools and crashed/timeout servers are errors too.
    """
    try:
        return _dispatch(name, args)
    except Exception as exc:
        return f"ERROR: MCP dispatch failed for '{name}': {exc}"


def _dispatch(name, args):
    if not isinstance(name, str) or not name.startswith("mcp__"):
        return f"ERROR: unknown MCP tool '{name}'"
    server_name, sep, tool_name = name[len("mcp__"):].partition("__")
    if not sep or not server_name or not tool_name:
        return f"ERROR: unknown MCP tool '{name}'"
    # Lazy start: rediscover (and respawn servers) if the cache was reset.
    _ensure_discovered()
    server = _SERVERS.get(server_name)
    known = {s["function"]["name"] for s in (_SCHEMAS or [])}
    if server is None or name not in known:
        return f"ERROR: unknown MCP tool '{name}'"
    try:
        return server.call_tool(tool_name, args or {}, timeout=30)
    except (MCPError, Exception) as exc:
        return f"ERROR: MCP tool '{tool_name}' failed: {exc}"


def reset_mcp_cache():
    """Stop all MCP servers and clear cached discovery (used by tests)."""
    global _SERVERS, _SCHEMAS
    for server in list(_SERVERS.values()):
        try:
            server.stop()
        except Exception:
            pass
    _SERVERS = {}
    _SCHEMAS = None
