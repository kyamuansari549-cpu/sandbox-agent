"""Feature 8 tests: scheduled tasks (tasks.py).

No live Ollama needed: stream_agent is monkeypatched wherever a run
would hit the LLM. The real scheduler thread is never started except in
the idempotency test.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import tasks as tasks_mod


@pytest.fixture()
def tmp_tasks(monkeypatch, tmp_path):
    """Point TASKS_FILE at an isolated temp file for this test."""
    fpath = tmp_path / "scheduled_tasks.json"
    monkeypatch.setattr(tasks_mod, "TASKS_FILE", str(fpath))
    return fpath


def test_create_list_delete_roundtrip(tmp_tasks):
    task = tasks_mod.create_task("morning report", "summarize logs", 600)
    assert task["id"]
    assert task["name"] == "morning report"
    assert task["interval_seconds"] == 600
    assert task["enabled"] is True
    assert task["last_run_at"] is None
    assert task["last_status"] is None

    tasks = tasks_mod.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["id"] == task["id"]

    assert tasks_mod.delete_task(task["id"]) is True
    assert tasks_mod.list_tasks() == []
    assert tasks_mod.delete_task(task["id"]) is False


def test_list_tasks_newest_first(tmp_tasks):
    first = tasks_mod.create_task("first", "prompt a", 600)
    second = tasks_mod.create_task("second", "prompt b", 900)
    tasks = tasks_mod.list_tasks()
    assert [t["id"] for t in tasks] == [second["id"], first["id"]]


@pytest.mark.parametrize("bad", [10, 0, -5, 59])
def test_create_validation_interval_too_small(tmp_tasks, bad):
    with pytest.raises(ValueError):
        tasks_mod.create_task("name", "prompt", bad)


def test_create_validation_interval_type(tmp_tasks):
    with pytest.raises(ValueError):
        tasks_mod.create_task("name", "prompt", "600")
    with pytest.raises(ValueError):
        tasks_mod.create_task("name", "prompt", True)


@pytest.mark.parametrize(
    "name,prompt",
    [("", "prompt"), ("   ", "prompt"), ("name", ""), ("name", "   "), (None, "prompt")],
)
def test_create_validation_empty_fields(tmp_tasks, name, prompt):
    with pytest.raises(ValueError):
        tasks_mod.create_task(name, prompt, 600)


def test_persistence_reload_from_disk(tmp_tasks):
    task = tasks_mod.create_task("persist me", "a durable prompt", 3600, model="qwen2.5:7b")
    # Re-load from disk in "fresh import state": bypass all in-memory caches.
    state = tasks_mod._load()
    assert len(state["tasks"]) == 1
    assert state["tasks"][0]["id"] == task["id"]
    assert state["tasks"][0]["prompt"] == "a durable prompt"
    assert state["tasks"][0]["model"] == "qwen2.5:7b"
    # Corrupt file -> empty state, never crashes.
    tmp_tasks.write_text("{not valid json")
    assert tasks_mod._load() == {"tasks": [], "runs": {}}


def _fake_stream(events):
    def fake(task, messages, **kw):
        return iter(events)

    return fake


def test_run_task_records_answer_run(tmp_tasks, monkeypatch):
    monkeypatch.setattr(
        tasks_mod,
        "stream_agent",
        _fake_stream([{"type": "answer", "content": "done-x"}]),
    )
    task = tasks_mod.create_task("runner", "do the thing", 600)
    tasks_mod._run_task(task)

    runs = tasks_mod.get_runs(task["id"])
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "ok"
    assert run["summary"] == "done-x"
    assert run["started_at"]

    (task_now,) = tasks_mod.list_tasks()
    assert task_now["last_status"] == "ok"
    assert task_now["last_run_at"] is not None


def test_run_task_error_path(tmp_tasks, monkeypatch):
    monkeypatch.setattr(
        tasks_mod, "stream_agent", _fake_stream([{"type": "error", "message": "boom"}])
    )
    task = tasks_mod.create_task("failing", "do the thing", 600)
    tasks_mod._run_task(task)

    runs = tasks_mod.get_runs(task["id"])
    assert len(runs) == 1
    assert runs[0]["status"] == "error"
    assert runs[0]["summary"] == "boom"

    (task_now,) = tasks_mod.list_tasks()
    assert task_now["last_status"] == "error"
    assert task_now["last_run_at"] is not None


def test_run_task_summary_truncated_to_500(tmp_tasks, monkeypatch):
    monkeypatch.setattr(
        tasks_mod,
        "stream_agent",
        _fake_stream([{"type": "answer", "content": "x" * 1200}]),
    )
    task = tasks_mod.create_task("long", "do the thing", 600)
    tasks_mod._run_task(task)
    (run,) = tasks_mod.get_runs(task["id"])
    assert len(run["summary"]) == 500


def test_runs_capped_at_20(tmp_tasks, monkeypatch):
    monkeypatch.setattr(
        tasks_mod,
        "stream_agent",
        _fake_stream([{"type": "answer", "content": "ok"}]),
    )
    task = tasks_mod.create_task("chatty", "do the thing", 600)
    for _ in range(25):
        tasks_mod._run_task(task)
    runs = tasks_mod.get_runs(task["id"])
    assert len(runs) == 20


def test_get_runs_unknown_task(tmp_tasks):
    assert tasks_mod.get_runs("no-such-id") == []


def test_tick_once_runs_due_tasks_and_survives_exceptions(tmp_tasks, monkeypatch):
    ran = []

    def fake(task, messages):
        ran.append(task["id"])
        return iter([{"type": "answer", "content": "tick-run"}])

    monkeypatch.setattr(tasks_mod, "stream_agent", fake)
    due_task = tasks_mod.create_task("due", "prompt", 60)
    # A task that errors inside stream_agent must not kill the tick.
    def exploding(task, messages):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(tasks_mod, "stream_agent", exploding)
    tasks_mod._tick_once()
    # Error path still records an error run; tick itself survived.
    runs = tasks_mod.get_runs(due_task["id"])
    assert len(runs) == 1 and runs[0]["status"] == "error"


def test_tick_once_skips_not_due_and_disabled(tmp_tasks, monkeypatch):
    calls = []

    def fake(task, messages):
        calls.append(task["id"])
        return iter([{"type": "answer", "content": "x"}])

    monkeypatch.setattr(tasks_mod, "stream_agent", fake)
    task = tasks_mod.create_task("fresh", "prompt", 3600)
    tasks_mod._run_task(task)  # sets last_run_at = now; not due for 3600s
    assert tasks_mod.get_runs(task["id"]) != []
    tasks_mod._tick_once()
    assert calls == []  # not due -> not re-run

    # Disabled task never runs even without last_run_at.
    t2 = tasks_mod.create_task("disabled", "prompt", 60)
    import tasks as tm

    with tm._lock:
        state = tm._load_locked()
        for t in state["tasks"]:
            if t["id"] == t2["id"]:
                t["enabled"] = False
        tm._save_locked(state)
    tasks_mod._tick_once()
    assert tasks_mod.get_runs(t2["id"]) == []


def test_start_scheduler_idempotent(tmp_tasks):
    first = tasks_mod.start_scheduler()
    assert first.is_alive()
    second = tasks_mod.start_scheduler()
    assert second is first
    # Leave a clean module state for other tests / production import.
    tasks_mod._scheduler_thread = None


def _client(tmp_tasks):
    app = FastAPI()
    tasks_mod.register_routes(app)
    return TestClient(app)


def test_router_crud(tmp_tasks):
    client = _client(tmp_tasks)

    r = client.post(
        "/api/tasks",
        json={"name": "nightly", "prompt": "summarize", "interval_seconds": 3600},
    )
    assert r.status_code == 201
    task_id = r.json()["task"]["id"]

    r = client.get("/api/tasks")
    assert r.status_code == 200
    assert [t["id"] for t in r.json()["tasks"]] == [task_id]

    r = client.get(f"/api/tasks/{task_id}/runs")
    assert r.status_code == 200
    assert r.json() == {"runs": []}

    r = client.delete(f"/api/tasks/{task_id}")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    r = client.delete(f"/api/tasks/{task_id}")
    assert r.status_code == 404

    r = client.get(f"/api/tasks/{task_id}/runs")
    assert r.status_code == 404


def test_router_post_validation_error(tmp_tasks):
    client = _client(tmp_tasks)
    r = client.post(
        "/api/tasks", json={"name": "bad", "prompt": "p", "interval_seconds": 5}
    )
    assert r.status_code == 400
    r = client.post(
        "/api/tasks", json={"name": "", "prompt": "p", "interval_seconds": 600}
    )
    assert r.status_code == 400
