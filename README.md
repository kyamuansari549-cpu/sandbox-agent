# SandboxAgent 🤖

**Your own mini AI agent with a computer.** You give it a task in plain
English — like "summarize these files" or "look up the latest Python release
and tell me what's new" — and it works step-by-step to get it done: it
thinks, picks a tool (run a command, read a file, search the web…), reads
the result, and keeps going until the task is finished. Everything runs
against a **local Ollama model**, so it's free, private, and needs no API
keys. Dangerous commands run inside an isolated **Docker sandbox**, and a
safety gate asks your permission before anything risky.

## Features

- **6 tools** — `run_command`, `read_file`, `write_file`, `list_dir`,
  `web_search`, `fetch_url`
- **Docker sandbox** — shell commands execute in an isolated
  `python:3.12-slim` container: no network, only one host folder visible,
  container deleted after each command
- **3-tier safety gate** — catastrophic commands are blocked outright,
  risky ones ask for your approval, safe ones just run
- **Dry-run mode** — preview what the agent *would* do without executing
  anything
- **Session logs** — every tool call of every run is saved to `logs/`
- **Web tools** — DuckDuckGo search + page fetching, stdlib-only, no API
  keys needed
- **Graceful degradation** — no Docker? No Ollama? You get a clear message,
  never a traceback

## Prerequisites

- **Python 3.10+** (`python3 --version` to check)
- **[Ollama](https://ollama.com)** installed **and running**
  (`ollama serve` in a terminal, or the Ollama app)
- A tool-capable model:
  ```
  ollama pull qwen2.5:7b
  ```
  (`llama3.1` and `mistral-nemo` also work — set `SANDBOX_AGENT_MODEL`.)
- **Docker Desktop** — *optional but recommended*. Without it, commands run
  on your own machine with a clear warning instead of inside the sandbox.
  See [Sandbox](#sandbox-docker) below.

**Windows notes:**
- Install [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop)
  and make sure it's running before starting the agent.
- Run the agent from CMD or PowerShell. (Git Bash / MSYS2 rewrites
  Unix-style paths before Docker sees them, which can break the volume
  mount — or set `MSYS_NO_PATHCONV=1` in Git Bash.)

## Quickstart

```
pip install -r requirements.txt
```

Then try one of these (each shows off something different):

```
# 1. Local exploration — reads files and reasons about them
python agent.py "list the files in this project and tell me what each one does"

# 2. Web research — searches the web, reads the best result, summarizes
python agent.py "what is the latest Python release and what changed in it"

# 3. Safe coding — writes a script and runs it inside the sandbox
python agent.py "write a Python script in work/ that prints the first 10 Fibonacci numbers, then run it"
```

The agent prints each tool call as it works (`[tool] name({...})`), then a
final summary. Try the prompts in [`examples/`](examples/) for more ideas.

## Web app (chat dashboard with live activity feed)

Prefer a chat interface over the terminal? The same agent runs behind a
web dashboard: you send messages, and every step appears live — thinking,
tool calls (expandable: arguments + output), and the final answer. Past
chats are listed in the sidebar with per-session stats.

**Only one terminal needed — no npm/node:**
```
pip install -r requirements.txt
uvicorn server:app --host 127.0.0.1 --port 8000
```

Then open **http://localhost:8000** in your browser. (Ollama must be
running, as with the CLI.) The prebuilt UI is served by the backend
itself; the API (`GET /api/health`, `POST /api/chat`, `GET /api/sessions`)
streams progress with Server-Sent Events.

*For UI development only:* `cd web && npm install && npm run dev`
(needs Node 20.19+), then `npm run build` to refresh `web/dist/`.

## How it works

```
you:  "your task in plain language"
        │
        ▼
┌─ agent loop (agent.py) ─────────────────────────┐
│  think → pick a tool → run it → read the result  │
│                    ↺ repeat until done            │
└──────────────────────────────────────────────────┘
        │                    │                 │
        ▼                    ▼                 ▼
   LLM (Ollama)      tools.py            sandbox.py / safety.py
   "the brain"     "the hands"         "the guardrails"
```

One Ollama `/api/chat` call per iteration, with the tool schemas attached.
The model either calls tools or answers in plain text — that final text is
the summary of what was done. The loop stops after `SANDBOX_AGENT_MAX_ITERS`
(default 20) iterations as an infinite-loop guard, and every tool returns
`"ERROR: ..."` text on failure instead of raising, so the loop never
crashes. If Ollama isn't reachable you get one clear line
(`ERROR: ...start Ollama...`), not a traceback.

## Project layout

```
sandbox-agent/
├── agent.py          # the agent loop + CLI
├── llm.py            # Ollama /api/chat wrapper (no SDK, just requests)
├── tools.py          # the 6 tools + their Ollama tool schemas
├── webtools.py       # web_search (DuckDuckGo) + fetch_url (HTML → text)
├── sandbox.py        # Docker container execution for run_command
├── safety.py         # denylist / approval gate for run_command
├── session.py        # append-only session logging to logs/
├── config.py         # all settings, overridable via env vars (see below)
├── requirements.txt  # requests, pytest
├── examples/         # try-me task prompts (01-explore-project.md, …)
├── work/             # the ONLY host dir mounted into the sandbox (/work)
├── logs/             # per-run session logs (created automatically)
├── tests/            # pytest suite — all external calls mocked
├── ARCHITECTURE.md   # design doc + phase roadmap
└── README.md
```

## Environment variables

| Variable | Default | What it does |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Where Ollama serves its API |
| `SANDBOX_AGENT_MODEL` | `qwen2.5:7b` | Tool-capable Ollama model to use |
| `SANDBOX_AGENT_TIMEOUT` | `30` | Per-command timeout in seconds (killed + reported on expiry) |
| `SANDBOX_AGENT_MAX_ITERS` | `20` | Max agent-loop iterations per task (infinite-loop guard) |
| `SANDBOX_AGENT_MAX_OUTPUT_CHARS` | `4000` | Truncate any single tool result longer than this |
| `SANDBOX_AGENT_IMAGE` | `python:3.12-slim` | Docker image for the sandbox |
| `SANDBOX_AGENT_WORKDIR` | `~/workspace/sandbox-agent/work` | The ONLY host dir mounted into the sandbox (at `/work`) |
| `SANDBOX_AGENT_SANDBOX_ENABLED` | `true` | `false` → always run commands on the host, no Docker attempt |
| `SANDBOX_AGENT_SAFETY` | `true` | `false` → skip the safety gate entirely (not recommended) |
| `SANDBOX_AGENT_DRY_RUN` | `false` | `true` → report what *would* run, execute nothing |

## Sandbox (Docker)

`run_command` doesn't run on your machine directly — it executes **inside
an isolated Docker container**:

```
docker run --rm --network none -v <sandbox-agent/work>:/work -w /work \
    python:3.12-slim sh -c "<your command>"
```

What this isolates:
- **Filesystem** — only `sandbox-agent/work/` is mounted (at `/work`);
  the container cannot see the rest of your computer.
- **Network** — `--network none`: no internet inside the sandbox.
- **Lifetime** — `--rm`: the container is deleted when the command ends.
- **Timeouts/output caps** — commands are killed after
  `SANDBOX_AGENT_TIMEOUT` (default 30 s), output truncated to
  `SANDBOX_AGENT_MAX_OUTPUT_CHARS` (default 4000 chars).

The image is pulled automatically on first use (`ensure_image()` checks
`docker images -q` first and pulls only when missing).

**No Docker? No problem.** If Docker isn't installed or its daemon isn't
running, the agent degrades gracefully: the command runs on the host, and
the result is prefixed with
`[WARNING: Docker unavailable — ran on HOST, not sandboxed]`.
To force host execution always: `SANDBOX_AGENT_SANDBOX_ENABLED=false`.

## Safety

Every `run_command` goes through a safety gate **before** anything executes
(sandbox or host). Three tiers:

| Verdict | Meaning | Examples |
|---|---|---|
| `deny` | Never executes — catastrophic and never legitimately needed | `rm -rf /`, `mkfs`, `dd if=/dev/zero of=/dev/sda`, fork bomb, `shutdown`/`reboot`, `> /dev/sda` |
| `approve` | Asks **you** on the terminal (`Allow it to run? [y/N]`) first | `rm -rf ./build`, `curl … \| sh`, `pip install …`, `apt install …`, `docker …`, `sudo …` |
| `ok` | Runs as before, no prompt | `ls -la`, `echo hello` |

Denied commands come back as `ERROR: blocked by safety policy: <reason>`;
a declined approval comes back as `ERROR: command cancelled by user.`
Both are plain tool results the model can read — the loop never crashes.

**Dry-run mode** — see what the agent *would* do without running anything:

```
SANDBOX_AGENT_DRY_RUN=true python agent.py "clean up the build directory"
# every run_command returns: [DRY RUN] would execute: <command>
```

**Disable the gate** (not recommended): `SANDBOX_AGENT_SAFETY=false`.

**Session logs** — every tool call of every run is appended to
`logs/session-<timestamp>.log` (time, tool name, args and result,
truncated to 500 chars each). Logging is best-effort by design: if it
fails, the agent keeps working.

> **Honest note:** the denylist is a speed bump, not a bulletproof vest.
> Pattern matching can't catch every obfuscation (`$VAR` tricks, base64
> payloads, unicode lookalikes). Real isolation is the Docker sandbox —
> the gate is there to stop accidents and make destructive intent visible,
> not to make arbitrary commands provably safe.

## Web tools

The agent can research, not just work locally:

| Tool | What it does |
|---|---|
| `web_search` | Search the web (DuckDuckGo, no API key) — returns numbered titles, URLs and snippets |
| `fetch_url` | Fetch a page and return its visible text (scripts/nav/footer stripped), truncated to `SANDBOX_AGENT_MAX_OUTPUT_CHARS` |

```
python agent.py "what is the latest Python release and what changed in it"
# the agent will web_search, then fetch_url the most relevant result
```

No API keys needed. Parsing is stdlib-only (`html.parser` + regex).

> **Sandbox tension, stated honestly:** the sandbox runs `run_command`
> with `--network none`, but these web tools run **on the host** — the
> agent itself needs internet to use them. This bypasses container
> isolation **by design**: both tools are read-only HTTP GETs. They fetch
> and read; they can never execute anything, so the safety gate doesn't
> apply to them either. Every failure (bad URL, timeout, HTTP error,
> non-HTML content like PDFs) comes back as a plain `ERROR: ...` tool
> result.

## Honest limitations

- The safety denylist is pattern matching — it stops accidents, not a
  determined attacker. Treat the Docker sandbox as the real isolation.
- Ollama must be running with a tool-capable model; without it the agent
  can't think at all (you'll get one clear error line).
- Model quality matters: a 7B local model is clever but not infallible —
  it sometimes picks a clumsy tool sequence or misreads a result. Bigger
  models (`qwen2.5:14b`, `llama3.1:8b`) generally do better.
- The sandbox has no internet (`--network none`), so anything the agent
  needs to download must go through the host-side web tools.
- Web tools bypass container network isolation by design (read-only HTTP
  GETs on the host) — documented above, not a bug.
- Session logs are plain text files; don't point the agent at secrets and
  expect them to stay secret.

## Example session

```
$ python agent.py "create a file hello.txt containing 'hi from sandbox-agent' and confirm it exists"
[tool] write_file({"path": "hello.txt", "content": "hi from sandbox-agent"})
[tool] run_command({"command": "cat hello.txt"})
Done — created hello.txt with the requested text and verified its contents with cat.
```

## Tests

```
pip install -r requirements.txt
python -m pytest tests/ -q
```

69 tests, all external calls mocked (Ollama, Docker, HTTP) — no model,
container, or network needed to run them.

See `ARCHITECTURE.md` for the full design doc, the safety model, and the
phase roadmap.
