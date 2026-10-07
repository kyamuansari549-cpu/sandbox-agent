"""SandboxAgent web backend: FastAPI + Server-Sent Events.

Run:
    uvicorn server:app --host 127.0.0.1 --port 8000
then open http://localhost:8000 — the prebuilt dashboard UI is served
from the same origin (no npm/node needed). `npm run dev` in web/ is only
for UI development.

SSE EVENT CONTRACT (consumed by the frontend)
---------------------------------------------
POST /api/chat streams `text/event-stream`; each event is one line:

    data: <json>\n\n

Event types, in order:
  1. {"type": "run", "run_id": "<uuid>"}  ALWAYS the first event. The
     client uses run_id for POST /api/runs/{run_id}/stop and
     /api/runs/{run_id}/approve.
  2. {"type": "session", "session_id": "<uuid>"}   SECOND event, only when the
     request had no (or an unknown) session_id \u2014 the client must store it
     and send it back on the next turn for multi-turn chat.
  3. {"type": "status", "message": "Thinking..."}  human-readable progress;
     also "Calling <tool>..." before each tool runs. Show as activity feed.
  4. {"type": "token", "content": "<chunk>"}  live model output, chunk by
     chunk. Render by appending to the in-progress answer bubble.
  5. {"type": "usage", "prompt_tokens": n, "completion_tokens": n}
     per-LLM-turn token counts, as reported by Ollama.
  6. {"type": "tool_call", "name": "<tool>", "args": {...}}
  7. {"type": "tool_result", "name": "<tool>", "output": "<text>"}
     output is truncated to ~2000 chars \u2014 a preview, not the full result.
  8. {"type": "approval_required", "run_id": "<uuid>", "command": "<cmd>",
      "reason": "<why>"}  a risky command needs a human yes/no: pop the
     approval dialog, then POST the decision to
     /api/runs/{run_id}/approve.
  9. {"type": "approval_resolved", "decision": "approve"|"deny"}
 10. {"type": "answer", "content": "<final reply>"}  also sent when the
     agent stops after MAX_ITERS without finishing.
 11. {"type": "error", "message": "<reason>"}  e.g. Ollama unreachable.
     The stream does NOT die: it still ends with {"type": "done"}.
 12. {"type": "stopped"}  the run was cancelled via /api/runs/{run_id}/stop.
 13. {"type": "done", "run_id": "<uuid>",
      "usage": {"prompt_tokens": n, "completion_tokens": n, "total_tokens": n},
      "stopped": <bool>, "model": "<model>"}
     ALWAYS the last event. Close the stream on it.

Example turn:
    data: {"type": "run", "run_id": "9a..."}
    data: {"type": "session", "session_id": "3f..."}
    data: {"type": "status", "message": "Thinking..."}
    data: {"type": "token", "content": "Here "}
    data: {"type": "token", "content": "are your files..."}
    data: {"type": "usage", "prompt_tokens": 120, "completion_tokens": 9}
    data: {"type": "answer", "content": "Here are your files..."}
    data: {"type": "done", "run_id": "9a...", "usage": {...}, "stopped": false, "model": "qwen2.5:7b"}

SESSION HISTORY (for the dashboard sidebar)
------------------------------------------
Sessions live in memory only: `_sessions[session_id]` is a dict
{"messages": [...], "display": [...], "created_at": "<iso>"}. "messages"
is the Ollama conversation (system prompt + full history); "display" is
what the UI renders — exactly one user turn + one assistant turn appended
per /api/chat request, the assistant turn carrying that request's tool
calls as [{"name", "args", "output"}]. Restarting the server clears every
session; there is no persistence.

  GET /api/sessions
      -> [{"session_id", "created_at", "turns", "preview"}]
      "turns" = number of user turns; "preview" = first user message,
      truncated to 60 chars. Newest first. [] when no sessions exist.
  GET /api/sessions/{session_id}
      -> {"session_id", "created_at", "display": [...]}
      404 {"detail": "unknown session"} for unknown ids.
"""

import json
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import config
from agent import SYSTEM_PROMPT, stream_agent
from tasks import register_routes

app = FastAPI(title="SandboxAgent")

# Local-dev CORS: the frontend is served separately.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Scheduled tasks (tasks.py): /api/tasks endpoints + background scheduler.
register_routes(app)

# In-memory sessions: session_id -> {"messages", "display", "created_at", "model"}.
# "messages" is the Ollama messages list (system prompt + full history) —
# multi-turn works because each new user message appends to the SAME list.
# "display" is the UI-facing turn history (user + assistant turns, the
# assistant turn carrying its tool calls). "model" is the session's chosen
# model (None -> config.MODEL). No auth, no database — restart the server,
# lose the chats.
_sessions = {}

# Live runs: run_id -> {"cancel": threading.Event,
#                       "approval_event": threading.Event | None,
#                       "approval_decision": "approve" | "deny" | None,
#                       "session_id": str}.
# Registered BEFORE the StreamingResponse is returned; removed when the
# stream generator finishes (try/finally). /api/runs/{id}/stop sets the
# cancel event; /api/runs/{id}/approve resolves a pending approval gate.
_runs = {}
_runs_lock = threading.Lock()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = None


class ApproveRequest(BaseModel):
    decision: str


def _get_session(session_id):
    """Return the session dict, creating it if needed."""
    sess = _sessions.get(session_id)
    if sess is None:
        sess = {
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
            "display": [],
            "created_at": _now_iso(),
            "model": None,
        }
        _sessions[session_id] = sess
    return sess


def _make_approver(run_id):
    """Return an approver callable for stream_agent's approval gate.

    Blocks (stop-aware) until /api/runs/{run_id}/approve resolves the
    pending approval, the run is cancelled, or 10 minutes pass — then
    returns "approve" | "deny" ("deny" on any timeout/cancel).
    """

    def _approve(command, reason):
        with _runs_lock:
            run = _runs.get(run_id)
        if run is None or run["cancel"].is_set():
            return "deny"
        run["approval_event"] = threading.Event()
        run["approval_decision"] = None
        deadline = time.time() + 600
        while time.time() < deadline:
            if run["cancel"].is_set():
                return "deny"
            if run["approval_event"].wait(1.0):
                return run.get("approval_decision") or "deny"
        return "deny"

    return _approve


def _event_stream(first_event, task, session, run_id, cancel, approver, model_used):
    """Yield SSE lines for one agent turn; one slow client can't break others.

    stream_agent() already converts Ollama failures into {"type": "error"}
    events. The outer try/except is a last-resort guard so an unexpected
    exception mid-stream still ends cleanly with {"type": "done"}.

    The FIRST event is always {"type": "run", "run_id": ...}, then the
    session handshake (for new sessions). "token"/"usage"/
    "approval_required"/"approval_resolved"/"stopped" events pass through
    untouched. Usage totals accumulate across the run for the final "done"
    event, and the run is unregistered from _runs when the generator
    finishes (try/finally).

    While streaming, tool_call/tool_result pairs and the final answer (or
    error message) are collected; when the stream finishes, exactly one
    user turn and one assistant turn are appended to session["display"],
    the assistant turn carrying usage totals and the model used.

    Threading: the whole agent loop runs in ONE dedicated producer thread
    that pumps SSE strings into a queue; the returned iterator just drains
    the queue. This matters because starlette resumes a plain sync
    generator on arbitrary threadpool threads between yields, and the
    tools.web_mode ContextVar that stream_agent sets (so run_command
    raises ApprovalNeeded instead of prompting on stdin) is only visible
    inside the context/thread where it was set. With one producer thread,
    stream_agent and run_command always share it.
    """

    def _generate():
        yield f"data: {json.dumps({'type': 'run', 'run_id': run_id})}\n\n"
        if first_event is not None:
            yield f"data: {json.dumps(first_event)}\n\n"
        tools = []
        pending = []  # tool_call dicts waiting for their tool_result
        final_text = None
        stopped = False
        prompt_tokens = 0
        completion_tokens = 0
        try:
            for event in stream_agent(
                task,
                session["messages"],
                model=model_used,
                run_id=run_id,
                cancel=cancel,
                approver=approver,
            ):
                etype = event.get("type")
                if etype == "tool_call":
                    pending.append(
                        {
                            "name": event.get("name", ""),
                            "args": event.get("args", {}),
                            "output": "",
                        }
                    )
                elif etype == "tool_result":
                    if pending:
                        pending[-1]["output"] = event.get("output", "")
                    tools.extend(pending)
                    pending = []
                elif etype == "answer":
                    final_text = event.get("content", "")
                elif etype == "error":
                    final_text = event.get("message", "")
                elif etype == "usage":
                    prompt_tokens += int(event.get("prompt_tokens", 0) or 0)
                    completion_tokens += int(event.get("completion_tokens", 0) or 0)
                elif etype == "stopped":
                    stopped = True
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # never let the SSE stream die mid-flight
            errmsg = str(exc)
            if final_text is None:
                final_text = errmsg
            yield f"data: {json.dumps({'type': 'error', 'message': errmsg})}\n\n"
        tools.extend(pending)  # dangling calls (no result) still get recorded
        ts = _now_iso()
        session["display"].append(
            {"role": "user", "content": task, "tools": [], "ts": ts}
        )
        session["display"].append(
            {
                "role": "assistant",
                "content": final_text or "",
                "tools": tools,
                "ts": ts,
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                },
                "model": model_used,
            }
        )
        yield (
            "data: "
            + json.dumps(
                {
                    "type": "done",
                    "run_id": run_id,
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                    "stopped": stopped,
                    "model": model_used,
                }
            )
            + "\n\n"
        )

    out_q = queue.Queue()
    _END = object()

    def _producer():
        try:
            for line in _generate():
                out_q.put(line)
        finally:
            with _runs_lock:
                _runs.pop(run_id, None)
            out_q.put(_END)

    thread = threading.Thread(target=_producer, daemon=True)
    thread.start()

    def _drain():
        while True:
            item = out_q.get()
            if item is _END:
                thread.join()
                return
            yield item

    return _drain()


@app.get("/api/health")
def health():
    return {"status": "ok", "model": config.MODEL}


@app.post("/api/chat")
def chat(req: ChatRequest):
    session_id = req.session_id
    first_event = None
    if not session_id or session_id not in _sessions:
        # New (or unknown) session: mint an id and announce it SECOND
        # (the "run" event is always first).
        session_id = str(uuid.uuid4())
        first_event = {"type": "session", "session_id": session_id}
    session = _get_session(session_id)
    if req.model:
        session["model"] = req.model
    model_used = session.get("model") or config.MODEL

    run_id = str(uuid.uuid4())
    cancel = threading.Event()
    with _runs_lock:
        _runs[run_id] = {
            "cancel": cancel,
            "approval_event": None,
            "approval_decision": None,
            "session_id": session_id,
        }
    approver = _make_approver(run_id)
    return StreamingResponse(
        _event_stream(
            first_event, req.message, session, run_id, cancel, approver, model_used
        ),
        media_type="text/event-stream",
    )


@app.get("/api/sessions")
def list_sessions():
    """Sidebar data: one summary per session, newest first."""
    items = []
    for sid, sess in _sessions.items():
        display = sess["display"]
        user_turns = [t for t in display if t["role"] == "user"]
        preview = user_turns[0]["content"][:60] if user_turns else ""
        items.append(
            {
                "session_id": sid,
                "created_at": sess["created_at"],
                "turns": len(user_turns),
                "preview": preview,
            }
        )
    items.sort(key=lambda i: i["created_at"], reverse=True)
    return items


@app.get("/api/sessions/{session_id}")
def get_session_history(session_id: str):
    """Full display history for one session (what the UI renders)."""
    sess = _sessions.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return {
        "session_id": session_id,
        "created_at": sess["created_at"],
        "display": sess["display"],
    }


@app.post("/api/runs/{run_id}/stop")
def stop_run(run_id: str):
    """Cancel a live run: the agent loop stops at the next checkpoint."""
    with _runs_lock:
        run = _runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    run["cancel"].set()
    return {"ok": True}


@app.post("/api/runs/{run_id}/approve")
def approve_run(run_id: str, req: ApproveRequest):
    """Resolve a pending approval gate ("approve" | "deny")."""
    with _runs_lock:
        run = _runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    if req.decision not in ("approve", "deny"):
        raise HTTPException(
            status_code=400, detail="decision must be 'approve' or 'deny'"
        )
    gate = run.get("approval_event")
    if gate is None or gate.is_set():
        raise HTTPException(status_code=409, detail="no pending approval")
    run["approval_decision"] = req.decision
    gate.set()
    return {"ok": True}


@app.get("/api/download")
def download_file(path: str):
    """Serve a file from the sandbox workdir as a download.

    `path` must be relative and stay inside config.SANDBOX_WORKDIR:
    absolute paths -> 400, escapes (../..) -> 403, missing/non-files -> 404.
    """
    base = os.path.abspath(config.SANDBOX_WORKDIR)
    if os.path.isabs(path):
        raise HTTPException(status_code=400, detail="path must be relative")
    full = os.path.abspath(os.path.join(base, path))
    if os.path.commonpath([base, full]) != base:
        raise HTTPException(status_code=403, detail="path escapes the workdir")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="not a file")
    return FileResponse(full)


@app.get("/api/models")
def list_models():
    """Models available for the picker, plus the configured default.

    Cloud mode (LLM_PROVIDER=cloud): lists the configured fallback chain
    as "provider:model" entries (deduplicated, chain order). Picking one
    pins the chain to that provider/model; the default (first entry)
    walks the whole chain with automatic failover.
    Ollama mode: proxies Ollama's /api/tags; on ANY failure (Ollama down,
    bad JSON, ...) returns an empty list with the default still set.
    """
    if config.LLM_PROVIDER == "cloud":
        seen = set()
        models = []
        for entry in config.cloud_chain():
            label = f"{entry['provider']}:{entry['model']}"
            if label not in seen:
                seen.add(label)
                models.append(label)
        return {"models": models, "default": models[0] if models else ""}
    try:
        resp = requests.get(f"{config.OLLAMA_HOST}/api/tags", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = [
            m.get("name") for m in data.get("models", []) if m.get("name")
        ]
        return {"models": models, "default": config.MODEL}
    except Exception:
        return {"models": [], "default": config.MODEL}


# ---------------------------------------------------------------------------
# Serve the prebuilt web UI from the same origin (no npm/node needed).
# web/dist/ is built once with `npm run build` and shipped inside the zip.
# These routes are registered AFTER every /api/* route, so the API is never
# shadowed. If web/dist/ is absent (dev checkout), only the API is served.
# ---------------------------------------------------------------------------
_DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "dist")

if os.path.isdir(_DIST_DIR):

    @app.get("/", include_in_schema=False)
    def _ui_index():
        return FileResponse(os.path.join(_DIST_DIR, "index.html"))

    def _ui_file(path):
        return FileResponse(path)

    for _fname in sorted(os.listdir(_DIST_DIR)):
        _fpath = os.path.join(_DIST_DIR, _fname)
        if os.path.isfile(_fpath) and _fname != "index.html":
            app.add_api_route(
                f"/{_fname}",
                lambda _p=_fpath: _ui_file(_p),
                methods=["GET"],
                include_in_schema=False,
            )

    from fastapi.staticfiles import StaticFiles

    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(_DIST_DIR, "assets")),
        name="ui-assets",
    )
