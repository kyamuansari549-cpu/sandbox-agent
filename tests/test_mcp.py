"""Feature 10 tests: minimal MCP stdio client, discovery/dispatch, echo server.

Real subprocesses are spawned (the toy echo server and tiny inline
scripts) — no network, no Ollama, no Docker. Every spawned server is
stopped in try/finally, and mcp_tools' cache is reset after each test so
no subprocess leaks between tests.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_mod
import mcp_tools as mcp_tools_mod
from mcp_client import MCPError, MCPServer

ECHO_SERVER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples",
    "mcp_echo_server.py",
)
PYTHON = sys.executable


@pytest.fixture(autouse=True)
def _clean_mcp_cache():
    mcp_tools_mod.reset_mcp_cache()
    yield
    mcp_tools_mod.reset_mcp_cache()


# ---------------------------------------------------------------------------
# mcp_client: framing, handshake, tools/list, tools/call


def test_echo_handshake_list_and_call():
    server = MCPServer("echo", PYTHON, [ECHO_SERVER])
    try:
        server.start(timeout=10)  # initialize handshake must not raise
        tools = server.list_tools(timeout=10)
        echo = next(t for t in tools if t["name"] == "echo")
        assert echo["description"] == "Echoes back the input text"
        assert echo["inputSchema"]["required"] == ["text"]
        out = server.call_tool("echo", {"text": "hi"}, timeout=10)
        assert "hi" in out
    finally:
        server.stop()


def test_call_unknown_tool_raises_jsonrpc_error():
    # A JSON-RPC-level "error" field (protocol error) raises MCPError;
    # tool-level failures use result["isError"] instead (tested below).
    server = MCPServer("echo", PYTHON, [ECHO_SERVER])
    try:
        server.start(timeout=10)
        with pytest.raises(MCPError) as excinfo:
            server.call_tool("nonexistent", {}, timeout=10)
        assert "-32602" in str(excinfo.value)
    finally:
        server.stop()


def test_call_tool_iserror_prefix():
    # A result with isError=true is a tool failure, not a protocol error:
    # the text parts are returned prefixed with "MCP tool error: ".
    script = (
        "import sys, json\n"
        "def send(o):\n"
        "    sys.stdout.write(json.dumps(o) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)\n"
        "    if m.get('method') == 'initialize':\n"
        "        send({'jsonrpc': '2.0', 'id': m['id'],"
        " 'result': {'protocolVersion': '2024-11-05', 'capabilities': {},"
        " 'serverInfo': {'name': 'n', 'version': '1'}}})\n"
        "    elif m.get('method') == 'tools/call':\n"
        "        send({'jsonrpc': '2.0', 'id': m['id'], 'result': {'isError': True,"
        " 'content': [{'type': 'text', 'text': 'boom went wrong'}]}})\n"
    )
    server = MCPServer("failer", PYTHON, ["-c", script])
    try:
        server.start(timeout=10)
        out = server.call_tool("anything", {}, timeout=10)
        assert out == "MCP tool error: boom went wrong"
    finally:
        server.stop()


def test_malformed_line_then_eof_raises():
    # Script prints garbage (skipped gracefully) then exits -> EOF -> MCPError.
    server = MCPServer(
        "garbage", PYTHON, ["-c", "print('this is not json', flush=True)"]
    )
    with pytest.raises(MCPError):
        server.start(timeout=5)
    server.stop()  # already stopped inside start(); harmless


def test_initialize_timeout():
    server = MCPServer("sleeper", PYTHON, ["-c", "import time; time.sleep(30)"])
    started = time.monotonic()
    try:
        with pytest.raises(MCPError):
            server.start(timeout=1)
    finally:
        server.stop()
    assert time.monotonic() - started < 10  # failed fast, didn't hang


def test_server_notifications_are_ignored():
    # A server may send notifications (no id) before/after its responses;
    # the client must skip them and still match requests by id.
    script = (
        "import sys, json\n"
        "def send(o):\n"
        "    sys.stdout.write(json.dumps(o) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)\n"
        "    if m.get('method') == 'initialize':\n"
        "        send({'jsonrpc': '2.0', 'method': 'notifications/message',"
        " 'params': {'level': 'info', 'data': 'hello'}})\n"
        "        send({'jsonrpc': '2.0', 'id': m['id'],"
        " 'result': {'protocolVersion': '2024-11-05', 'capabilities': {},"
        " 'serverInfo': {'name': 'n', 'version': '1'}}})\n"
        "    elif m.get('method') == 'tools/list':\n"
        "        send({'jsonrpc': '2.0', 'id': m['id'], 'result': {'tools': []}})\n"
    )
    server = MCPServer("notifier", PYTHON, ["-c", script])
    try:
        server.start(timeout=10)  # must not choke on the notification
        assert server.list_tools(timeout=10) == []
    finally:
        server.stop()


def test_stop_is_idempotent():
    server = MCPServer("echo", PYTHON, [ECHO_SERVER])
    server.start(timeout=10)
    server.stop()
    server.stop()  # second stop must not raise


# ---------------------------------------------------------------------------
# mcp_tools: discovery, dispatch, caching, failure isolation


def test_mcp_schemas_empty_config_is_fast_and_spawn_free(monkeypatch):
    monkeypatch.setattr(config_mod, "MCP_SERVERS", [])
    started = time.monotonic()
    schemas = mcp_tools_mod.mcp_schemas()
    assert schemas == []
    assert time.monotonic() - started < 2  # no subprocess spawned


def test_mcp_schemas_never_raises_on_degenerate_config(monkeypatch):
    monkeypatch.setattr(config_mod, "MCP_SERVERS", "not-a-list")
    assert mcp_tools_mod.mcp_schemas() == []


def test_mcp_schemas_and_call_echo(monkeypatch):
    monkeypatch.setattr(
        config_mod,
        "MCP_SERVERS",
        [{"name": "echo", "command": PYTHON, "args": [ECHO_SERVER]}],
    )
    schemas = mcp_tools_mod.mcp_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "mcp__echo__echo" in names
    # schema matches the TOOL_SCHEMAS format tools.py uses
    schema = next(s for s in schemas if s["function"]["name"] == "mcp__echo__echo")
    assert schema["type"] == "function"
    assert schema["function"]["parameters"]["type"] == "object"
    assert "text" in schema["function"]["parameters"]["properties"]
    out = mcp_tools_mod.call_mcp_tool("mcp__echo__echo", {"text": "yo"})
    assert "yo" in out


def test_broken_server_does_not_break_others(monkeypatch, capsys):
    monkeypatch.setattr(
        config_mod,
        "MCP_SERVERS",
        [
            {"name": "broken", "command": "definitely-not-a-real-command", "args": []},
            {"name": "echo", "command": PYTHON, "args": [ECHO_SERVER]},
        ],
    )
    schemas = mcp_tools_mod.mcp_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "mcp__echo__echo" in names
    assert not any(n.startswith("mcp__broken__") for n in names)
    assert "broken" in capsys.readouterr().err  # reported to stderr


def test_call_unknown_tool(monkeypatch):
    monkeypatch.setattr(config_mod, "MCP_SERVERS", [])
    out = mcp_tools_mod.call_mcp_tool("mcp__nope__nope", {})
    assert out == "ERROR: unknown MCP tool 'mcp__nope__nope'"
    out = mcp_tools_mod.call_mcp_tool("not_mcp_at_all", {})
    assert out.startswith("ERROR: unknown MCP tool")


def test_schemas_cached_across_calls(monkeypatch):
    # Discovery spawns servers once; the second mcp_schemas() reuses cache.
    monkeypatch.setattr(
        config_mod,
        "MCP_SERVERS",
        [{"name": "echo", "command": PYTHON, "args": [ECHO_SERVER]}],
    )
    first = mcp_tools_mod.mcp_schemas()
    second = mcp_tools_mod.mcp_schemas()
    assert [s["function"]["name"] for s in first] == [
        s["function"]["name"] for s in second
    ]
    assert len(mcp_tools_mod._SERVERS) == 1


def test_reset_mcp_cache_stops_servers(monkeypatch):
    monkeypatch.setattr(
        config_mod,
        "MCP_SERVERS",
        [{"name": "echo", "command": PYTHON, "args": [ECHO_SERVER]}],
    )
    mcp_tools_mod.mcp_schemas()
    assert mcp_tools_mod._SERVERS, "discovery should have started a server"
    proc = next(iter(mcp_tools_mod._SERVERS.values()))._proc
    mcp_tools_mod.reset_mcp_cache()
    assert mcp_tools_mod._SERVERS == {}
    assert proc.poll() is not None  # subprocess is gone
