"""Minimal MCP (Model Context Protocol) client: JSON-RPC 2.0 over stdio.

Transport is newline-delimited JSON (NDJSON): one JSON-RPC message per
line, written to the server's stdin and read back from its stdout, exactly
as the MCP stdio transport specifies.

Server -> client traffic that is NOT a response to one of our requests
(e.g. pings, log notifications like ``notifications/message``) is ignored:
``_request`` only accepts the response whose ``"id"`` matches the request
it sent, skipping everything else (including malformed lines). One bad
server can therefore never corrupt another server's conversation.

Stdlib only: subprocess, json, threading, queue.
"""

import json
import queue
import subprocess
import threading
import time

PROTOCOL_VERSION = "2024-11-05"


class MCPError(Exception):
    """Any MCP transport or protocol failure."""


_EOF = object()  # sentinel: the server closed its stdout


class MCPServer:
    """One MCP server subprocess.

    Requests are serialised with an internal lock, so one instance is safe
    to use from the agent loop even if tools are called from threads.
    """

    def __init__(self, name, command, args=()):
        self.name = name
        self.command = command
        self.args = tuple(args)
        self._id = 0
        self._proc = None
        self._lines = None  # queue.Queue fed by the reader thread
        self._lock = threading.Lock()
        self._started = False

    # ------------------------------------------------------------------
    # lifecycle

    def start(self, timeout=10):
        """Spawn the server subprocess and run the MCP initialize handshake.

        Sends the ``initialize`` request, waits for its response (raising
        MCPError on timeout or an ``"error"`` field), then sends the
        ``notifications/initialized`` notification. Raises MCPError on any
        failure; the half-started subprocess is stopped before raising.
        """
        if self._started:
            return
        try:
            proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line-buffered text pipes
            )
        except Exception as exc:
            raise MCPError(f"could not spawn MCP server '{self.name}': {exc}")
        self._proc = proc
        self._lines = queue.Queue()
        threading.Thread(
            target=self._drain_stderr, daemon=True,
            name=f"mcp-{self.name}-stderr",
        ).start()
        threading.Thread(
            target=self._read_stdout, daemon=True,
            name=f"mcp-{self.name}-reader",
        ).start()
        self._started = True
        try:
            self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "sandbox-agent", "version": "1.0"},
                },
                timeout=timeout,
            )
            self._notify("notifications/initialized")
        except Exception:
            self.stop()
            raise

    def stop(self):
        """Terminate the server subprocess. Best-effort, never raises."""
        self._started = False
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # EOF: lets a well-behaved server exit
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # protocol

    def list_tools(self, timeout=10):
        """Return the server's tool list: [{name, description, inputSchema}]."""
        result = self._request("tools/list", None, timeout=timeout)
        if not isinstance(result, dict):
            raise MCPError(
                f"server '{self.name}': bad tools/list result: {result!r}"
            )
        tools = result.get("tools", [])
        if not isinstance(tools, list):
            raise MCPError(
                f"server '{self.name}': tools/list 'tools' is not a list"
            )
        return tools

    def call_tool(self, tool_name, arguments, timeout=30):
        """Call a tool; return the concatenated ``content[]`` text parts.

        If the result carries ``"isError"``, the returned string is prefixed
        with ``"MCP tool error: "``.
        """
        result = self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
            timeout=timeout,
        )
        if not isinstance(result, dict):
            raise MCPError(
                f"server '{self.name}': bad tools/call result: {result!r}"
            )
        parts = []
        content = result.get("content") or []
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
        text = "".join(parts)
        if result.get("isError"):
            return "MCP tool error: " + (text or "(no message)")
        return text

    # ------------------------------------------------------------------
    # plumbing

    def _read_stdout(self):
        """Reader thread: feeds one stdout line per message into the queue."""
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if line:
                    self._lines.put(line)
        except Exception:
            pass
        finally:
            self._lines.put(_EOF)

    def _drain_stderr(self):
        """Drain stderr so a chatty server never blocks on a full pipe."""
        try:
            for _ in self._proc.stderr:
                pass
        except Exception:
            pass

    def _notify(self, method, params=None):
        """Send a JSON-RPC notification (no id, no reply expected)."""
        if self._proc is None:
            raise MCPError(f"server '{self.name}' is not started")
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        line = json.dumps(msg) + "\n"
        try:
            with self._lock:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
        except Exception as exc:
            raise MCPError(
                f"server '{self.name}': failed to send '{method}': {exc}"
            )

    def _request(self, method, params, timeout):
        """Send a request and wait for the response with the matching id.

        Server -> client notifications (no id), responses to other ids,
        and malformed lines are all skipped. Raises MCPError when the
        server dies (EOF) or the timeout expires.
        """
        if self._proc is None:
            raise MCPError(f"server '{self.name}' is not started")
        with self._lock:
            self._id += 1
            rid = self._id
            msg = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                msg["params"] = params
            line = json.dumps(msg) + "\n"
            try:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            except Exception as exc:
                raise MCPError(f"server '{self.name}': write failed: {exc}")
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPError(
                        f"server '{self.name}': no response to '{method}' "
                        f"within {timeout}s"
                    )
                try:
                    item = self._lines.get(timeout=remaining)
                except queue.Empty:
                    raise MCPError(
                        f"server '{self.name}': no response to '{method}' "
                        f"within {timeout}s"
                    )
                if item is _EOF:
                    rc = self._proc.poll()
                    suffix = f" (exit code {rc})" if rc is not None else ""
                    raise MCPError(
                        f"server '{self.name}' closed the connection "
                        f"during '{method}'{suffix}"
                    )
                try:
                    resp = json.loads(item)
                except ValueError:
                    continue  # malformed line: skip, keep waiting
                if not isinstance(resp, dict):
                    continue
                if resp.get("id") != rid:
                    continue  # notification or another request's response
                err = resp.get("error")
                if err:
                    raise MCPError(
                        f"server '{self.name}' error on '{method}': {err}"
                    )
                return resp.get("result")
