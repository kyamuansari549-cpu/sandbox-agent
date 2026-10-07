"""Phase 1 tools. Every tool returns a string.

Contract: tools NEVER raise into the agent loop. Any failure — bad path,
timeout, crashed command — comes back as an "ERROR: ..." string so the LLM
can read it and recover.
"""

import os
import subprocess

import config
import safety
import sandbox
from webtools import fetch_url, web_search

# Prefixed to run_command's result when Docker is missing and the command
# ran on the host instead of inside the sandbox.
HOST_FALLBACK_WARNING = (
    "[WARNING: Docker unavailable \u2014 ran on HOST, not sandboxed]"
)


def _format_result(out, returncode):
    header = f"[exit code {returncode}]"
    return f"{header}\n{out}" if out else f"{header} (no output)"


def _truncate(out):
    if len(out) > config.MAX_OUTPUT_CHARS:
        out = (
            out[: config.MAX_OUTPUT_CHARS]
            + f"\n... [truncated: output exceeded {config.MAX_OUTPUT_CHARS} chars]"
        )
    return out


def _run_on_host(command, timeout):
    """Phase 1 behaviour: execute directly on the host machine."""
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s and was killed."
    except Exception as exc:  # e.g. bad shell, OS error
        return f"ERROR: could not run command: {exc}"
    out = _truncate((proc.stdout or "") + (proc.stderr or ""))
    return _format_result(out, proc.returncode)


def run_command(command, timeout=None):
    """Run a shell command; return stdout+stderr (truncated) and exit code.

    Phase 2: prefers the Docker sandbox. If Docker is unavailable, falls
    back to host execution with a clear warning line. Set
    SANDBOX_AGENT_SANDBOX_ENABLED=false to always use the host path.

    Phase 3: before ANY execution, the safety gate runs (unless
    SANDBOX_AGENT_SAFETY=false). Denied commands are never executed;
    risky ones ask the human first. With SANDBOX_AGENT_DRY_RUN=true,
    nothing executes — the call just reports what would have run.
    """
    timeout = config.TIMEOUT if timeout is None else timeout

    if config.DRY_RUN:
        return f"[DRY RUN] would execute: {command}"

    if config.SAFETY_ENABLED:
        verdict, reason = safety.classify(command)
        if verdict == "deny":
            return f"ERROR: blocked by safety policy: {reason}"
        if verdict == "approve" and not safety.ask_approval(command, reason):
            return "ERROR: command cancelled by user."

    if config.SANDBOX_ENABLED:
        try:
            out, returncode = sandbox.run_in_sandbox(command, timeout=timeout)
        except sandbox.DockerUnavailableError:
            return f"{HOST_FALLBACK_WARNING}\n{_run_on_host(command, timeout)}"
        except Exception as exc:  # never crash the agent loop
            return f"ERROR: sandbox execution failed: {exc}"
        return _format_result(out, returncode)
    return _run_on_host(command, timeout)


def read_file(path):
    """Return file contents (truncated), or an error string."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except IsADirectoryError:
        return f"ERROR: path is a directory, not a file: {path}"
    except Exception as exc:
        return f"ERROR: could not read file {path}: {exc}"
    if len(content) > config.MAX_OUTPUT_CHARS:
        content = (
            content[: config.MAX_OUTPUT_CHARS]
            + f"\n... [truncated: file exceeded {config.MAX_OUTPUT_CHARS} chars]"
        )
    return content


def write_file(path, content):
    """Write content to path (creating parent dirs). Returns OK/error string."""
    try:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as exc:
        return f"ERROR: could not write file {path}: {exc}"
    return f"OK: wrote {len(content)} chars to {path}"


def list_dir(path):
    """List directory entries (dirs suffixed with /), or an error string."""
    try:
        entries = sorted(os.listdir(path))
    except FileNotFoundError:
        return f"ERROR: directory not found: {path}"
    except NotADirectoryError:
        return f"ERROR: not a directory: {path}"
    except Exception as exc:
        return f"ERROR: could not list directory {path}: {exc}"
    if not entries:
        return f"(empty directory: {path})"
    lines = [
        name + "/" if os.path.isdir(os.path.join(path, name)) else name
        for name in entries
    ]
    return "\n".join(lines)


# Name -> implementation, used by the agent loop to dispatch tool calls.
TOOLS = {
    "run_command": run_command,
    "read_file": read_file,
    "write_file": write_file,
    "list_dir": list_dir,
    "fetch_url": fetch_url,
    "web_search": web_search,
}


def _schema(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


# Ollama-format tool schemas, passed to every /api/chat call.
TOOL_SCHEMAS = [
    _schema(
        "run_command",
        "Run a shell command and return its output (stdout+stderr) and exit code.",
        {
            "command": {"type": "string", "description": "The shell command to run."},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds before the command is killed.",
            },
        },
        ["command"],
    ),
    _schema(
        "read_file",
        "Read a text file and return its contents.",
        {"path": {"type": "string", "description": "Path to the file to read."}},
        ["path"],
    ),
    _schema(
        "write_file",
        "Write text to a file, creating parent directories if needed.",
        {
            "path": {"type": "string", "description": "Path of the file to write."},
            "content": {"type": "string", "description": "Text content to write."},
        },
        ["path", "content"],
    ),
    _schema(
        "list_dir",
        "List the entries of a directory.",
        {"path": {"type": "string", "description": "Path of the directory to list."}},
        ["path"],
    ),
    _schema(
        "fetch_url",
        "Read a web page and return its visible text. Use this when you need "
        "the contents of a specific URL.",
        {
            "url": {"type": "string", "description": "The http(s) URL to read."},
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters of page text to return.",
            },
        },
        ["url"],
    ),
    _schema(
        "web_search",
        "Search the web and return numbered results with titles, URLs and "
        "snippets. Use this to find information, documentation, or current facts.",
        {
            "query": {"type": "string", "description": "The search query."},
            "num_results": {
                "type": "integer",
                "description": "How many results to return (1-10).",
            },
        },
        ["query"],
    ),
]
