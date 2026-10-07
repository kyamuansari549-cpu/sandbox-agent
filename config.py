"""Configuration for SandboxAgent. Every value is overridable via env vars."""

import json
import os

# Where Ollama serves its API.
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# Tool-capable local model. qwen2.5:7b is a good free default; any Ollama
# model with function-calling support works (e.g. llama3.1, mistral-nemo).
MODEL = os.getenv("SANDBOX_AGENT_MODEL", "qwen2.5:7b")

# Per-command timeout (seconds) for run_command when the call omits one.
TIMEOUT = int(os.getenv("SANDBOX_AGENT_TIMEOUT", "30"))

# Hard cap on agent-loop iterations per task (infinite-loop guard).
MAX_ITERS = int(os.getenv("SANDBOX_AGENT_MAX_ITERS", "20"))

# Truncate any single tool result longer than this (chars) to protect the
# model's context window.
MAX_OUTPUT_CHARS = int(os.getenv("SANDBOX_AGENT_MAX_OUTPUT_CHARS", "4000"))


def _env_bool(name, default):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# Docker image used for the Phase 2 sandbox.
SANDBOX_IMAGE = os.getenv("SANDBOX_AGENT_IMAGE", "python:3.12-slim")

# The ONLY host directory mounted into the sandbox container (at /work).
SANDBOX_WORKDIR = os.getenv(
    "SANDBOX_AGENT_WORKDIR",
    os.path.join(os.path.expanduser("~"), "workspace", "sandbox-agent", "work"),
)

# When false, run_command always executes on the host (no Docker attempt,
# no warning). Env: SANDBOX_AGENT_SANDBOX_ENABLED=false
SANDBOX_ENABLED = _env_bool("SANDBOX_AGENT_SANDBOX_ENABLED", True)

# Phase 3: safety layer (safety.py). When false, run_command skips all
# safety checks entirely. Env: SANDBOX_AGENT_SAFETY=false
SAFETY_ENABLED = _env_bool("SANDBOX_AGENT_SAFETY", True)

# Phase 3: dry-run mode. When true, run_command reports what it WOULD
# execute without executing anything — handy for seeing what the agent
# would do. Env: SANDBOX_AGENT_DRY_RUN=true
DRY_RUN = _env_bool("SANDBOX_AGENT_DRY_RUN", False)


def _env_json(name, default):
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


# MCP (Model Context Protocol) servers: JSON list of
# {"name": str, "command": str, "args": [str]}. Each entry spawns a stdio
# MCP server subprocess; every tool it exposes becomes an agent tool named
# mcp__<name>__<tool>. Env: MCP_SERVERS
# Example: MCP_SERVERS='[{"name":"echo","command":"python","args":["examples/mcp_echo_server.py"]}]'
MCP_SERVERS = _env_json("MCP_SERVERS", [])
