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
  1. {"type": "session", "session_id": "<uuid>"}   FIRST event, only when the
     request had no (or an unknown) session_id — the client must store it
     and send it back on the next turn for multi-turn chat.
  2. {"type": "status", "message": "Thinking..."}  human-readable progress;
     also "Calling <tool>..." before each tool runs. Show as activity feed.
  3. {"type": "tool_call", "name": "<tool>", "args": {...}}
  4. {"type": "tool_result", "name": "<tool>", "output": "<text>"}
     output is truncated to ~2000 chars — a preview, not the full result.
  5. {"type": "answer", "content": "<final reply>"}  also sent when the
     agent stops after MAX_ITERS without finishing.
  6. {"type": "error", "message": "<reason>"}  e.g. Ollama unreachable.
     The stream does NOT die: it still ends with {"type": "done"}.
  7. {"type": "done"}  ALWAYS the last event. Close the stream on it.

Example turn:
    data: {"type": "session", "session_id": "3f..."}
    data: {"type": "status", "message": "Thinking..."}
    data: {"type": "tool_call", "name": "list_dir", "args": {"path": "."}}
    data: {"type": "tool_result", "name": "list_dir", "output": "[exit code 0]..."}
    data: {"type": "status", "message": "Thinking..."}
    data: {"type": "answer", "content": "Here are your files..."}
    data: {"type": "done"}

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
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import config
from agent import SYSTEM_PROMPT, stream_agent

app = FastAPI(title="SandboxAgent")

# Local-dev CORS: the frontend is served separately.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory sessions: session_id -> {"messages", "display", "created_at"}.
# "messages" is the Ollama messages list (system prompt + full history) —
# multi-turn works because each new user message appends to the SAME list.
# "display" is the UI-facing turn history (user + assistant turns, the
# assistant turn carrying its tool calls). No auth, no database — restart
# the server, lose the chats.
_sessions = {}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


def _get_session(session_id):
    """Return the session dict, creating it if needed."""
    sess = _sessions.get(session_id)
    if sess is None:
        sess = {
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
            "display": [],
            "created_at": _now_iso(),
        }
        _sessions[session_id] = sess
    return sess


def _event_stream(first_event, task, session):
    """Yield SSE lines for one agent turn; one slow client can't break others.

    stream_agent() already converts Ollama failures into {"type": "error"}
    events. The outer try/except is a last-resort guard so an unexpected
    exception mid-stream still ends cleanly with {"type": "done"}.

    While streaming, tool_call/tool_result pairs and the final answer (or
    error message) are collected; when the stream finishes, exactly one
    user turn and one assistant turn are appended to session["display"].
    """

    def gen():
        if first_event is not None:
            yield f"data: {json.dumps(first_event)}\n\n"
        tools = []
        pending = []  # tool_call dicts waiting for their tool_result
        final_text = None
        try:
            for event in stream_agent(task, session["messages"]):
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
            {"role": "assistant", "content": final_text or "", "tools": tools, "ts": ts}
        )
        yield 'data: {"type": "done"}\n\n'

    return gen()


@app.get("/api/health")
def health():
    return {"status": "ok", "model": config.MODEL}


@app.post("/api/chat")
def chat(req: ChatRequest):
    session_id = req.session_id
    first_event = None
    if not session_id or session_id not in _sessions:
        # New (or unknown) session: mint an id and announce it FIRST.
        session_id = str(uuid.uuid4())
        first_event = {"type": "session", "session_id": session_id}
    session = _get_session(session_id)
    return StreamingResponse(
        _event_stream(first_event, req.message, session),
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
