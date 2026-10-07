# SandboxAgent — Architecture (Phase 0)

SandboxAgent is kyamuddin's own mini AI agent: you give it a task in plain
language, and it works step-by-step using tools — running commands, reading
and writing files — until the task is done. It runs against a **local**
Ollama model, so it's free and private.

## 1. The 4 components (simple terms)

1. **LLM (the brain)** — a local model served by Ollama (default
   `qwen2.5:7b`). It does the thinking: it reads the task, decides what to
   do next, and picks a tool. It never touches your computer directly.
2. **Sandbox (the computer)** — the machine the tools run on. In Phase 1
   this is simply the user's own machine (the commands run as the user).
   Phase 3 moves execution into an isolated Docker container so a mistake
   can't hurt the host. ✅ (done in Phase 2)
3. **Tools (the hands)** — small Python functions the LLM is allowed to
   call: `run_command`, `read_file`, `write_file`, `list_dir`, plus Phase 4's
   web tools `web_search` and `fetch_url`. The LLM only ever *asks* for a
   tool call; Python executes it and hands the result back.
4. **Agent loop (the heartbeat)** — the cycle that makes it an *agent*:
   think → pick a tool → run it → read the result → think again … until the
   model answers in plain text instead of calling a tool. That final text is
   the summary of what was done.

## 2. Agent loop pseudocode

```
messages = [system_prompt, user_task]
repeat up to MAX_ITERS:
    response = ollama_chat(messages, tools)      # one LLM call
    if response has no tool_calls:
        print(response.content)                  # done: final answer
        stop
    append response to messages
    for each tool_call in response.tool_calls:
        result = execute(tool_call.name, tool_call.arguments)   # tools.py
        append {"role": "tool", "content": result} to messages
print("Stopped after MAX_ITERS iterations without finishing.")
```

## 3. Ollama native API contract (exact)

One HTTP call per loop iteration — no streaming, no SDK needed.

```
POST http://localhost:11434/api/chat
Content-Type: application/json

{
  "model": "qwen2.5:7b",
  "messages": [
    {"role": "system", "content": "You are SandboxAgent, ..."},
    {"role": "user",   "content": "list the files here"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "list_dir", "arguments": {"path": "."}}}
    ]},
    {"role": "tool", "content": "agent.py\nconfig.py\n..."}
  ],
  "tools": [
    {"type": "function",
     "function": {"name": "run_command",
                  "description": "Run a shell command and return its output.",
                  "parameters": {"type": "object",
                                 "properties": {
                                   "command": {"type": "string", "description": "The shell command to run."},
                                   "timeout": {"type": "integer", "description": "Timeout in seconds."}},
                                 "required": ["command"]}}}
  ],
  "stream": false
}
```

Response (non-streaming). The message we care about is `response["message"]`:

```json
{
  "model": "qwen2.5:7b",
  "message": {
    "role": "assistant",
    "content": "",
    "tool_calls": [
      {"function": {"name": "run_command", "arguments": {"command": "dir"}}}
    ]
  },
  "done": true
}
```

Rules we implement against:
- Tool calls arrive in `response["message"]["tool_calls"]` (list; absent or
  empty means the model is done talking — `content` is the final answer).
- Each tool call is `{"function": {"name": ..., "arguments": {...}}}` —
  `arguments` is already a dict in the native API.
- Tool results go back as new messages: `{"role": "tool", "content": "<text>"}`.
- If Ollama isn't reachable (`ConnectionError`), we raise a clear
  `RuntimeError` telling the user to start Ollama — the loop never crashes
  with a raw traceback.

## 4. Phase 1 tool list + JSON schemas

| Tool | Args | What it does |
|---|---|---|
| `run_command` | `command` (string, required), `timeout` (integer, optional) | Runs a shell command, returns stdout+stderr (truncated) plus the exit code |
| `read_file` | `path` (string, required) | Returns file contents (truncated); error text if unreadable |
| `write_file` | `path` (string, required), `content` (string, required) | Writes (creating parent dirs); returns OK or error text |
| `list_dir` | `path` (string, required) | Returns entries, `dir/` suffixed for directories; error text if bad path |

Schemas are defined once in `tools.py` as `TOOL_SCHEMAS` (Ollama format:
`{"type": "function", "function": {"name", "description", "parameters"}}`)
and passed to every `/api/chat` call.

## 5. Safety model

**Phase 1 (now):**
- Every command runs with a timeout (default 30 s, per-call overridable);
  a timed-out command is killed and reported as an error string.
- All tool output is truncated to `MAX_OUTPUT_CHARS` (default 4000) so a
  huge listing can't flood the model's context.
- The loop stops after `MAX_ITERS` (default 20) with a clear message.
- Every tool catches its own failures and returns `"ERROR: ..."` text —
  a bad path or crashed command can never kill the loop.

**Deferred to Phase 3:**
- Command denylist (block `rm -rf`, `format`, `shutdown`, …).
- Human approval before destructive commands.
- Docker container as the real sandbox (Phase 1 runs on the host as the
  user — treat it like giving a careful assistant your terminal).

## 6. Roadmap

- **Phase 0** — this document (design). ✅
- **Phase 1** — core loop + 4 tools + CLI + mocked tests. ✅
- **Phase 2** — Docker sandbox: run `run_command` (and file tools) inside
  an isolated container; stream container logs back as tool results. ✅
- **Phase 3** — safety layer: denylist, per-command approval prompts,
  dry-run mode, full session logging to a file.
- **Phase 4** — web tools: `web_search` and `fetch_url` so the agent can
  research, not just work locally. ✅
- **Phase 5** — polish: expanded README (features, quickstart,
  env-var table, honest limitations), `examples/` task prompts,
  `.gitignore`, repo cleanup. ✅
- **Phase 6** — web dashboard: FastAPI serves a prebuilt React UI with
  live SSE activity feed, sidebar sessions, stats. ✅
- **Phase 7** — interactive runtime: token streaming, run cancellation,
  web approval dialog, model picker, file downloads, scheduled tasks,
  token usage, MCP client, Dockerfile + CI. ✅

## 8. Web runtime: runs, streaming, approvals, tasks

**Run registry.** Every `POST /api/chat` mints a `run_id` and registers
`_runs[run_id] = {"cancel": threading.Event, "approval_event",
"approval_decision", "session_id"}`. The agent loop runs in one dedicated
daemon thread pumping SSE strings into a queue (this keeps the
`tools.web_mode` ContextVar visible — starlette may resume a plain sync
generator on different threadpool threads between yields). The registry
entry is removed in a `finally` when the stream ends.

**SSE event contract** (`POST /api/chat` → `text/event-stream`):
`run` (always first) → `session` (new sessions only) → repeating
`status` / `tool_call` / `tool_result` → `token` (answer chunks, appended
by the UI) → `usage` (per LLM call) → `approval_required` /
`approval_resolved` (only when the safety gate pauses) → `answer` (full
text, overwrites streamed tokens) → `error` (non-fatal) → `done` (always
last: `{run_id, usage, stopped, model}`). `stopped` is yielded when the
user cancels.

**Stop.** `POST /api/runs/{id}/stop` sets the run's cancel event; the loop
checks it each iteration and before every tool call, then yields `stopped`
and finishes with `done(stopped=true)`. A command already executing is
allowed to finish (no thread killing).

**Web approval gate.** `tools.run_command` raises `ApprovalNeeded` instead
of prompting stdin when the `web_mode` ContextVar is set (set by
`stream_agent` whenever an `approver` callback is passed; the CLI never
sets it, so terminal behavior is unchanged). The loop yields
`approval_required {run_id, command, reason}`, blocks in the approver
(stop-aware, 10-minute timeout → deny), then yields `approval_resolved`
and either re-runs the command with a private `_approved=True` bypass
(not in the tool schema, so the model can never set it) or returns
`"ERROR: command cancelled by user."`. `POST /api/runs/{id}/approve`
`{"decision": "approve"|"deny"}` resolves it (404 unknown run, 409 no
pending approval). Concurrent runs are isolated by run_id.

**Token streaming.** `llm.chat(..., on_token=...)` POSTs Ollama with
`stream: true` and invokes the callback per content chunk; `stream_agent`
runs this in a daemon thread and drains the chunks through a queue so the
generator can `yield {"type": "token", ...}` live. Eval counts
(`prompt_eval_count`/`eval_count`) are attached to the returned message as
`_usage` (popped before the message enters history) and surfaced as
`usage` events plus run totals in `done`.

**Model picker.** `GET /api/models` proxies Ollama `/api/tags` (empty list
+ configured default on any failure). `POST /api/chat` accepts an optional
`model`, stored per session and reused for later turns.

**File downloads.** `GET /api/download?path=<relative>` serves files from
`SANDBOX_WORKDIR` only: absolute paths → 400, `commonpath` escape →
403, missing → 404.

**Scheduled tasks** (`tasks.py`). Interval-based, stdlib-only: a daemon
thread ticks every 15s; due tasks (enabled and `last_run_at is None` or
interval elapsed) run `stream_agent` with a fresh system prompt and no
approver — risky commands auto-decline via the stdin/EOFError fallback.
State persists in `scheduled_tasks.json` (atomic tmp+replace writes under
a lock; corrupt file → empty state). `GET/POST/DELETE /api/tasks` and
`GET /api/tasks/{id}/runs`; runs capped at 20 per task. Wired via
`register_routes(app)` (router + `on_startup` scheduler).

## 7. MCP (Model Context Protocol)

MCP lets the agent gain new tools without new agent code: any program that
speaks the MCP stdio transport (newline-delimited JSON-RPC 2.0) can be
configured as a *server*, and every tool it advertises becomes callable by
the model.

**Transport (`mcp_client.py`).** Each server is a subprocess spawned with
`stdin`/`stdout` pipes in line-buffered text mode, one JSON-RPC message
per line. `start()` performs the MCP `initialize` handshake — sending
`protocolVersion: "2024-11-05"` plus client info, waiting for the matching
`id`'s response (MCPError on timeout or an `"error"` field), then sending
the `notifications/initialized` notification (no id, no reply). Reading is
done by a daemon thread feeding a `queue.Queue`; the requesting thread
blocks on `queue.get(timeout=…)` against a deadline, so no `select` and no
platform-specific code — this works on Windows too. A second daemon thread
drains `stderr` so a chatty server can never block on a full pipe. Server
crash mid-request surfaces as EOF → MCPError. Server→client traffic that
isn't a response to our request (pings, `notifications/message`, or just
malformed lines) is skipped by id-matching, never confused with a reply.

**Discovery + dispatch (`mcp_tools.py`).** On first use, every entry in
`config.MCP_SERVERS` is started (10 s) and asked for `tools/list` (10 s).
Each advertised tool becomes an Ollama-format schema named
`mcp__<server>__<tool>` (e.g. `mcp__echo__echo`), carrying the server's own
description and `inputSchema`. Both the server handles and the schema list
are cached; `reset_mcp_cache()` stops every server and clears the cache
(used by tests). With `MCP_SERVERS` empty, discovery does nothing and
returns `[]` fast — no subprocess is ever spawned. Dispatch revalidates the
name against the cached schemas and calls `tools/call` (30 s); tool-level
failures (`result.isError`) come back prefixed `MCP tool error: `, and —
like every other tool — nothing ever raises into the agent loop; failures
are plain `"ERROR: …"` strings.

**Failure isolation.** Each server is discovered and called independently:
any exception (spawn failure, timeout, crash, bad JSON) prints one stderr
line — `sandbox-agent: MCP server '<name>' unavailable: <err>` — and that
server is skipped while the rest continue.

**Plug-in convention with the core.** `tools.py` (backend-core) does
`from mcp_tools import mcp_schemas, call_mcp_tool` inside
try/except ImportError: MCP schemas are merged into the tool list passed
to every `/api/chat` call, and `mcp__*` calls are routed to
`call_mcp_tool`. This module was built so no edit to `tools.py`,
`agent.py`, `server.py`, or `llm.py` is needed for the plug-in to work.

**Trust note.** MCP tools run their server subprocesses on the host with
the user's privileges — they are NOT sandboxed in Docker and are NOT
subject to the `run_command` safety gate. Only configure servers you trust,
the same way you'd trust any program you choose to run.
