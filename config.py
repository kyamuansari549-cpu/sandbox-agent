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


# ---------------------------------------------------------------------------
# Cloud LLM providers (OpenAI-compatible free APIs: Groq, Gemini, NVIDIA).
# ---------------------------------------------------------------------------
# LLM_PROVIDER: "ollama" (default, local) or "cloud" (OpenAI-compatible APIs).
# Switch with: SANDBOX_AGENT_LLM_PROVIDER=cloud
LLM_PROVIDER = os.getenv("SANDBOX_AGENT_LLM_PROVIDER", "ollama").strip().lower()

# Ordered fallback chain, e.g. SANDBOX_AGENT_LLM_CHAIN="groq,gemini,nvidia".
# Providers are tried in order; when one hits a rate limit (429), a server
# error (5xx) or a network failure, the next entry automatically takes over.
LLM_CHAIN = [
    p.strip().lower()
    for p in os.getenv("SANDBOX_AGENT_LLM_CHAIN", "groq,gemini").split(",")
    if p.strip()
]

# Known providers: OpenAI-compatible base URL + a sensible free default model.
# Override per provider with {NAME}_BASE_URL / {NAME}_MODEL env vars.
_CLOUD_PROVIDER_SPECS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-2.0-flash",
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "default_model": "meta/llama-3.3-70b-instruct",
    },
}

# Seconds a rate-limited (429) chain entry is skipped before being retried.
LLM_FAILOVER_COOLDOWN = int(os.getenv("SANDBOX_AGENT_LLM_COOLDOWN", "60"))


def cloud_chain():
    """Build the ordered fallback chain from env vars.

    For every provider in the chain (in order), every configured API key
    becomes one chain entry: {"provider", "base_url", "api_key", "model"}.

    Keys come from {NAME}_API_KEY or {NAME}_API_KEYS (comma-separated —
    extra keys for the same provider are tried in order, so one key's
    rate limit fails over to the next key). Model/base URL come from
    {NAME}_MODEL / {NAME}_BASE_URL, falling back to the spec defaults.

    Example:
        SANDBOX_AGENT_LLM_PROVIDER=cloud
        SANDBOX_AGENT_LLM_CHAIN="groq,gemini"
        GROQ_API_KEYS="gsk_aaa,gsk_bbb"
        GEMINI_API_KEY="AIza..."
    """
    chain = []
    raw = os.getenv("SANDBOX_AGENT_LLM_CHAIN")
    names = (
        [p.strip().lower() for p in raw.split(",") if p.strip()]
        if raw is not None
        else list(LLM_CHAIN)
    )
    for name in names:
        spec = _CLOUD_PROVIDER_SPECS.get(name)
        if spec is None:
            continue  # unknown provider name — ignore, don't crash
        upper = name.upper()
        raw_keys = os.getenv(f"{upper}_API_KEYS") or os.getenv(f"{upper}_API_KEY") or ""
        keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        if not keys:
            continue  # no key configured — skip this provider
        model = os.getenv(f"{upper}_MODEL", spec["default_model"])
        base_url = os.getenv(f"{upper}_BASE_URL", spec["base_url"])
        for key in keys:
            chain.append(
                {
                    "provider": name,
                    "base_url": base_url.rstrip("/"),
                    "api_key": key,
                    "model": model,
                }
            )
    return chain
