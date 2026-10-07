"""SandboxAgent feature 8: scheduled tasks.

A stdlib-only interval scheduler with JSON persistence. The coordinator
wires it into server.py with a single call:

    from tasks import register_routes
    register_routes(app)

Scheduling model
----------------
A task has a `name`, a `prompt`, and an `interval_seconds`. Every
TICK_SECONDS (15s) the background scheduler thread checks each enabled
task; a task is due when it has never run, or its last run finished
>= interval_seconds ago. Due tasks run sequentially in the scheduler
thread. The tick loop is wrapped so a task exception can never kill it.

HEADLESS SAFETY (risky commands)
--------------------------------
Scheduled runs call stream_agent() with NO approver and NO web-approval
contextvar. Risky commands therefore fall back to the stdin prompt in
safety.ask_approval(), which catches EOFError and returns False
(decline). Under uvicorn the process stdin is typically closed/inherited,
so input() raises EOFError and risky commands are AUTO-DECLINED.
Denylist-blocked commands never execute regardless.

NOTE on `model`: tasks store an optional `model` field, passed through to
stream_agent(model=...) so a task can pin a specific Ollama model.
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent import SYSTEM_PROMPT, stream_agent

TASKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduled_tasks.json")

# Seconds between scheduler wakeups.
TICK_SECONDS = 15
# Minimum allowed task interval (seconds).
MIN_INTERVAL_SECONDS = 60
# Max run records kept per task (oldest dropped).
MAX_RUNS_PER_TASK = 20
# Run summaries are truncated to this many chars.
SUMMARY_CHARS = 500

# Guards ALL file IO. `_load_locked`/`_save_locked` assume the caller
# already holds it; the public API helpers acquire it themselves.
_lock = threading.Lock()

# The single scheduler thread, once start_scheduler() has run.
_scheduler_thread = None


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _empty_state():
    return {"tasks": [], "runs": {}}


def _load_locked():
    """Read the tasks file. A corrupt or missing file -> empty state (never crashes)."""
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    tasks = data.get("tasks")
    runs = data.get("runs")
    if not isinstance(tasks, list) or not isinstance(runs, dict):
        return _empty_state()
    return {"tasks": tasks, "runs": runs}


def _save_locked(state):
    """Persist atomically: write tmp file, then os.replace."""
    tmp = TASKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, TASKS_FILE)


def _load():
    with _lock:
        return _load_locked()


# ---------------------------------------------------------------------------
# Task CRUD API
# ---------------------------------------------------------------------------

def _validate(name, prompt, interval_seconds):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int):
        raise ValueError("interval_seconds must be an integer")
    if interval_seconds < MIN_INTERVAL_SECONDS:
        raise ValueError(f"interval_seconds must be >= {MIN_INTERVAL_SECONDS}")


def create_task(name, prompt, interval_seconds, model=None):
    """Create a task, persist it immediately, and return its dict."""
    _validate(name, prompt, interval_seconds)
    task = {
        "id": uuid.uuid4().hex,
        "name": name,
        "prompt": prompt,
        "interval_seconds": interval_seconds,
        "model": model,
        "enabled": True,
        "created_at": _now_iso(),
        "last_run_at": None,
        "last_status": None,
    }
    with _lock:
        state = _load_locked()
        state["tasks"].append(task)
        _save_locked(state)
    return dict(task)


def delete_task(task_id):
    """Delete a task (and its runs). Returns True if it existed."""
    with _lock:
        state = _load_locked()
        remaining = [t for t in state["tasks"] if t.get("id") != task_id]
        if len(remaining) == len(state["tasks"]):
            return False
        state["tasks"] = remaining
        state["runs"].pop(task_id, None)
        _save_locked(state)
        return True


def list_tasks():
    """All tasks, newest first."""
    with _lock:
        state = _load_locked()
    tasks = [dict(t) for t in state["tasks"]]
    tasks.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    return tasks


def _task_exists(task_id):
    with _lock:
        state = _load_locked()
    return any(t.get("id") == task_id for t in state["tasks"])


def get_runs(task_id):
    """Run records for a task, newest first. [] for an unknown task."""
    with _lock:
        state = _load_locked()
        runs = state["runs"].get(task_id)
    if not isinstance(runs, list):
        return []
    return [dict(r) for r in reversed(runs)]


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def _is_due(task, now):
    last = task.get("last_run_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (now - last_dt).total_seconds() >= task.get("interval_seconds", 0)


def _run_task(task):
    """Execute one task's prompt through the agent loop and persist the run.

    Takes the task dict (as returned by create_task/list_tasks). Never
    raises: an unexpected exception becomes an "error" run record.

    Headless safety: stream_agent() is called with no approver and no
    web-approval contextvar, so risky commands fall back to the stdin
    prompt in safety.ask_approval(), which auto-declines on EOFError
    (uvicorn's stdin is not interactive).
    """
    task_id = task["id"]
    started_at = _now_iso()
    status = "ok"
    final_text = "(no answer returned)"
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for event in stream_agent(task["prompt"], messages, model=task.get("model")):
            if not isinstance(event, dict):
                continue
            etype = event.get("type")
            if etype == "answer":
                final_text = event.get("content", "") or final_text
            elif etype == "error":
                status = "error"
                final_text = event.get("message", "") or final_text
    except Exception as exc:  # never let a run die without a record
        status = "error"
        final_text = str(exc) or "scheduled run crashed"

    run = {
        "run_id": uuid.uuid4().hex,
        "started_at": started_at,
        "status": status,
        "summary": (final_text or "")[:SUMMARY_CHARS],
    }
    now = _now_iso()
    with _lock:
        state = _load_locked()
        for t in state["tasks"]:
            if t.get("id") == task_id:
                t["last_run_at"] = now
                t["last_status"] = status
                break
        runs = state["runs"].setdefault(task_id, [])
        if not isinstance(runs, list):
            runs = state["runs"][task_id] = []
        runs.append(run)
        del runs[:-MAX_RUNS_PER_TASK]  # drop oldest beyond the cap
        _save_locked(state)
    return run


def _tick_once():
    """Check due tasks and run each one; a task exception never kills the tick."""
    now = datetime.now(timezone.utc)
    with _lock:
        state = _load_locked()
        due = [t for t in state["tasks"] if t.get("enabled", True) and _is_due(t, now)]
    for task in due:
        try:
            _run_task(task)
        except Exception:
            pass  # the tick loop must never die


def _tick_forever():
    while True:
        try:
            _tick_once()
        except Exception:
            pass
        time.sleep(TICK_SECONDS)


def start_scheduler():
    """Start the scheduler thread. Idempotent: returns the same live thread.

    The thread is a daemon, so it never blocks interpreter shutdown.
    """
    global _scheduler_thread
    with _lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return _scheduler_thread
        thread = threading.Thread(
            target=_tick_forever, name="sandbox-scheduler", daemon=True
        )
        _scheduler_thread = thread
        thread.start()
        return thread


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

router = APIRouter()


class TaskCreate(BaseModel):
    name: str
    prompt: str
    interval_seconds: int
    model: Optional[str] = None


@router.get("/tasks")
def api_list_tasks():
    return {"tasks": list_tasks()}


@router.post("/tasks", status_code=201)
def api_create_task(body: TaskCreate):
    try:
        task = create_task(
            body.name, body.prompt, body.interval_seconds, model=body.model
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"task": task}


@router.delete("/tasks/{task_id}")
def api_delete_task(task_id: str):
    if not delete_task(task_id):
        raise HTTPException(status_code=404, detail="unknown task")
    return {"ok": True}


@router.get("/tasks/{task_id}/runs")
def api_get_runs(task_id: str):
    if not _task_exists(task_id):
        raise HTTPException(status_code=404, detail="unknown task")
    return {"runs": get_runs(task_id)}


def register_routes(app):
    """Wire the tasks API and the scheduler into the FastAPI app.

    Call once from server.py:  from tasks import register_routes
    """
    app.include_router(router, prefix="/api")
    # Version-safe startup hook: on_startup runs at startup on all
    # supported FastAPI/Starlette versions.
    app.router.on_startup.append(start_scheduler)
