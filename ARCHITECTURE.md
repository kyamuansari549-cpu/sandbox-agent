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
