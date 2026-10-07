"""Server tests. ALL Ollama HTTP calls are mocked — no live Ollama needed."""

import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import llm as llm_mod
import server as server_mod


def _tool_call(name, args):
    return {"function": {"name": name, "arguments": args}}


def _parse_sse(text):
    """Parse a text/event-stream body into a list of event dicts."""
    events = []
    for chunk in text.split("\n\n"):
        for line in chunk.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:"):].strip()))
    return events


def _client():
    server_mod._sessions.clear()
    return TestClient(server_mod.app)


def _session_id(events):
    """session_id from the "session" handshake event (now the 2nd event)."""
    return next(e for e in events if e["type"] == "session")["session_id"]


def _one_tool_then_answer(monkeypatch, path):
    """llm.chat: first turn calls list_dir, second turn answers."""
    seen = {"n": 0, "messages": None}

    def fake_chat(messages, tools, **kw):
        seen["n"] += 1
        if seen["n"] == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [_tool_call("list_dir", {"path": str(path)})],
            }
        seen["messages"] = messages
        return {"role": "assistant", "content": "Found your file."}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    return seen


def test_health_returns_ok():
    client = _client()
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "model": server_mod.config.MODEL}


def test_chat_streams_full_event_sequence(monkeypatch, tmp_path):
    """tool_call -> tool_result -> answer -> done, with session event first."""
    (tmp_path / "hello.txt").write_text("hi")
    _one_tool_then_answer(monkeypatch, tmp_path)
    client = _client()

    resp = client.post("/api/chat", json={"message": "list my files"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(resp.text)
    types = [e["type"] for e in events]

    # run handshake is the FIRST event; session handshake comes second
    assert types[0] == "run"
    assert events[0]["run_id"]
    assert types[1] == "session"
    assert events[1]["session_id"]
    # the core loop events are all present, in order
    assert "tool_call" in types and "tool_result" in types and "answer" in types
    assert types.index("tool_call") < types.index("tool_result")
    assert types.index("tool_result") < types.index("answer")
    # stream ALWAYS ends with done
    assert types[-1] == "done"

    tool_call = next(e for e in events if e["type"] == "tool_call")
    assert tool_call["name"] == "list_dir"
    assert tool_call["args"] == {"path": str(tmp_path)}

    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["name"] == "list_dir"
    assert "hello.txt" in tool_result["output"]

    answer = next(e for e in events if e["type"] == "answer")
    assert answer["content"] == "Found your file."


def test_unknown_session_id_creates_new_session(monkeypatch, tmp_path):
    """An unknown session_id is ignored: a fresh uuid is minted and announced."""
    _one_tool_then_answer(monkeypatch, tmp_path)
    client = _client()

    resp = client.post(
        "/api/chat", json={"message": "hi", "session_id": "does-not-exist"}
    )
    events = _parse_sse(resp.text)
    assert events[0]["type"] == "run"
    assert events[1]["type"] == "session"
    new_id = events[1]["session_id"]
    assert new_id != "does-not-exist"
    assert new_id in server_mod._sessions


def test_multiturn_reuses_session_messages(monkeypatch, tmp_path):
    """A second turn with the same session_id keeps the conversation going."""
    (tmp_path / "hello.txt").write_text("hi")
    seen = _one_tool_then_answer(monkeypatch, tmp_path)
    client = _client()

    sid = _session_id(_parse_sse(
        client.post("/api/chat", json={"message": "list my files"}).text
    ))
    history_len = len(server_mod._sessions[sid]["messages"])

    # Second turn: llm sees the SAME messages list, now longer (prior turn kept)
    resp = client.post("/api/chat", json={"message": "again", "session_id": sid})
    events = _parse_sse(resp.text)
    assert events[0]["type"] != "session"  # no new handshake for known session
    assert events[-1]["type"] == "done"
    assert server_mod._sessions[sid]["messages"] is seen["messages"]
    assert len(seen["messages"]) > history_len  # history grew, wasn't reset


def test_ollama_down_yields_error_event_not_crash(monkeypatch):
    """llm.chat raising RuntimeError becomes an error event; stream ends done."""
    def fake_chat(messages, tools, **kw):
        raise RuntimeError("Cannot reach Ollama at http://localhost:11434.")

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    client = _client()

    resp = client.post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    types = [e["type"] for e in events]
    assert "error" in types
    error = next(e for e in events if e["type"] == "error")
    assert "Ollama" in error["message"]
    assert types[-1] == "done"  # stream still terminates cleanly


# --------------------------------------------------------------------------
# session history


def test_sessions_list_empty_when_no_chats():
    client = _client()
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_sessions_list_after_chat(monkeypatch, tmp_path):
    """One chat -> one summary: id, created_at, turns=1, 60-char preview."""
    (tmp_path / "hello.txt").write_text("hi")
    _one_tool_then_answer(monkeypatch, tmp_path)
    client = _client()

    long_msg = "list my files please, this message is deliberately longer than sixty characters"
    sid = _session_id(_parse_sse(
        client.post("/api/chat", json={"message": long_msg}).text
    ))

    items = client.get("/api/sessions").json()
    assert len(items) == 1
    item = items[0]
    assert item["session_id"] == sid
    assert item["created_at"]
    assert item["turns"] == 1
    assert item["preview"] == long_msg[:60]
    assert len(item["preview"]) == 60


def test_sessions_list_newest_first(monkeypatch, tmp_path):
    """Two sessions come back newest-first."""
    _one_tool_then_answer(monkeypatch, tmp_path)
    client = _client()

    sid1 = _session_id(_parse_sse(
        client.post("/api/chat", json={"message": "first chat"}).text
    ))
    sid2 = _session_id(_parse_sse(
        client.post("/api/chat", json={"message": "second chat"}).text
    ))

    items = client.get("/api/sessions").json()
    assert len(items) == 2
    assert [i["session_id"] for i in items] == [sid2, sid1]
    assert items[0]["created_at"] >= items[1]["created_at"]


def test_session_history_returns_display_turns(monkeypatch, tmp_path):
    """History: one user turn + one assistant turn carrying the tool calls."""
    (tmp_path / "hello.txt").write_text("hi")
    _one_tool_then_answer(monkeypatch, tmp_path)
    client = _client()

    sid = _session_id(_parse_sse(
        client.post("/api/chat", json={"message": "list my files"}).text
    ))

    resp = client.get(f"/api/sessions/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == sid
    assert body["created_at"]

    display = body["display"]
    assert len(display) == 2
    user_turn, asst_turn = display

    assert user_turn["role"] == "user"
    assert user_turn["content"] == "list my files"
    assert user_turn["tools"] == []
    assert user_turn["ts"]

    assert asst_turn["role"] == "assistant"
    assert asst_turn["content"] == "Found your file."
    assert asst_turn["ts"]
    assert len(asst_turn["tools"]) == 1
    tool = asst_turn["tools"][0]
    assert tool["name"] == "list_dir"
    assert tool["args"] == {"path": str(tmp_path)}
    assert "hello.txt" in tool["output"]


def test_session_history_404_for_unknown_id():
    client = _client()
    resp = client.get("/api/sessions/does-not-exist")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "unknown session"}


def test_multiturn_accumulates_display_turns(monkeypatch, tmp_path):
    """Two requests on one session -> 2 user + 2 assistant display turns."""
    (tmp_path / "hello.txt").write_text("hi")
    _one_tool_then_answer(monkeypatch, tmp_path)
    client = _client()

    sid = _session_id(_parse_sse(
        client.post("/api/chat", json={"message": "first question"}).text
    ))
    client.post("/api/chat", json={"message": "second question", "session_id": sid})

    display = client.get(f"/api/sessions/{sid}").json()["display"]
    assert [t["role"] for t in display] == ["user", "assistant", "user", "assistant"]
    assert display[0]["content"] == "first question"
    assert display[2]["content"] == "second question"
    # the mock only emits a tool call on the first llm turn, so check turn 1
    assert display[1]["tools"][0]["name"] == "list_dir"

    items = client.get("/api/sessions").json()
    assert len(items) == 1
    assert items[0]["turns"] == 2


def test_error_turn_still_recorded_in_display(monkeypatch):
    """An Ollama failure records the error text as the assistant turn."""
    def fake_chat(messages, tools, **kw):
        raise RuntimeError("Cannot reach Ollama at http://localhost:11434.")

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    client = _client()

    sid = _session_id(_parse_sse(
        client.post("/api/chat", json={"message": "hi there"}).text
    ))

    display = client.get(f"/api/sessions/{sid}").json()["display"]
    assert len(display) == 2
    assert display[0]["role"] == "user"
    assert display[1]["role"] == "assistant"
    assert "Ollama" in display[1]["content"]


# --------------------------------------------------------------------------
# New contract: download endpoint, run lifecycle, models, usage in done.


def test_download_guards(monkeypatch, tmp_path):
    """download: absolute -> 400, traversal -> 403, missing -> 404, legit -> 200."""
    monkeypatch.setattr(server_mod.config, "SANDBOX_WORKDIR", str(tmp_path))
    client = _client()

    assert (
        client.get("/api/download", params={"path": "/etc/passwd"}).status_code
        == 400
    )
    assert (
        client.get("/api/download", params={"path": "../../etc/passwd"}).status_code
        == 403
    )
    assert (
        client.get("/api/download", params={"path": "nope.txt"}).status_code == 404
    )

    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "note.txt").write_text("hello-download")
    resp = client.get("/api/download", params={"path": "sub/note.txt"})
    assert resp.status_code == 200
    assert resp.content == b"hello-download"


def test_done_event_carries_usage_run_id_and_model(monkeypatch):
    """done is last and carries run_id, usage totals, stopped, model."""

    def fake_chat(messages, tools, **kw):
        return {
            "role": "assistant",
            "content": "hi there",
            "_usage": {"prompt_tokens": 10, "completion_tokens": 4},
        }

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    client = _client()
    events = _parse_sse(client.post("/api/chat", json={"message": "hi"}).text)

    assert events[0]["type"] == "run"
    run_id = events[0]["run_id"]
    assert run_id

    usage_ev = next(e for e in events if e["type"] == "usage")
    assert (usage_ev["prompt_tokens"], usage_ev["completion_tokens"]) == (10, 4)

    done = events[-1]
    assert done["type"] == "done"
    assert done["run_id"] == run_id
    assert done["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
    }
    assert done["stopped"] is False
    assert done["model"] == server_mod.config.MODEL

    assert run_id not in server_mod._runs  # unregistered when stream finished

    sid = _session_id(events)
    display = client.get(f"/api/sessions/{sid}").json()["display"]
    assert display[1]["usage"] == {"prompt_tokens": 10, "completion_tokens": 4}
    assert display[1]["model"] == server_mod.config.MODEL


def test_chat_model_override_persists_on_session(monkeypatch):
    """model= in the request is used and sticks to the session."""

    def fake_chat(messages, tools, **kw):
        seen["model"] = kw.get("model")
        return {"role": "assistant", "content": "ok"}

    seen = {}
    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    client = _client()

    events = _parse_sse(
        client.post("/api/chat", json={"message": "hi", "model": "llama3.1:8b"}).text
    )
    assert seen["model"] == "llama3.1:8b"
    assert events[-1]["model"] == "llama3.1:8b"

    # second turn without a model keeps the session's model
    sid = _session_id(events)
    seen.clear()
    client.post("/api/chat", json={"message": "again", "session_id": sid})
    assert seen["model"] == "llama3.1:8b"


def _register_test_run(run_id, approval_event=None):
    with server_mod._runs_lock:
        server_mod._runs[run_id] = {
            "cancel": threading.Event(),
            "approval_event": approval_event,
            "approval_decision": None,
            "session_id": "s",
        }


def _unregister_test_run(run_id):
    with server_mod._runs_lock:
        server_mod._runs.pop(run_id, None)


def test_stop_unknown_run_404():
    client = _client()
    assert client.post("/api/runs/does-not-exist/stop").status_code == 404


def test_stop_known_run_sets_cancel():
    client = _client()
    run_id = "test-run-stop"
    _register_test_run(run_id)
    try:
        resp = client.post(f"/api/runs/{run_id}/stop")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert server_mod._runs[run_id]["cancel"].is_set()
    finally:
        _unregister_test_run(run_id)


def test_approve_unknown_run_404():
    client = _client()
    resp = client.post("/api/runs/nope/approve", json={"decision": "approve"})
    assert resp.status_code == 404


def test_approve_bad_decision_400():
    client = _client()
    run_id = "test-run-approve-400"
    _register_test_run(run_id, approval_event=threading.Event())
    try:
        resp = client.post(f"/api/runs/{run_id}/approve", json={"decision": "maybe"})
        assert resp.status_code == 400
    finally:
        _unregister_test_run(run_id)


def test_approve_no_pending_409():
    client = _client()
    run_id = "test-run-approve-409"
    # no gate created yet
    _register_test_run(run_id)
    try:
        resp = client.post(f"/api/runs/{run_id}/approve", json={"decision": "deny"})
        assert resp.status_code == 409
    finally:
        _unregister_test_run(run_id)
    # gate already resolved
    gate = threading.Event()
    gate.set()
    _register_test_run(run_id, approval_event=gate)
    try:
        resp = client.post(f"/api/runs/{run_id}/approve", json={"decision": "deny"})
        assert resp.status_code == 409
    finally:
        _unregister_test_run(run_id)


def test_approve_happy_path():
    client = _client()
    run_id = "test-run-approve-ok"
    gate = threading.Event()
    _register_test_run(run_id, approval_event=gate)
    try:
        resp = client.post(f"/api/runs/{run_id}/approve", json={"decision": "deny"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert gate.is_set()
        assert server_mod._runs[run_id]["approval_decision"] == "deny"
    finally:
        _unregister_test_run(run_id)


def test_models_endpoint_proxies_ollama(monkeypatch):
    """GET /api/models lists Ollama's tags, default from config."""

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "qwen2.5:7b"}, {"name": "llama3.1:8b"}, {}]}

    class FakeRequests:
        @staticmethod
        def get(url, timeout=None):
            assert url.endswith("/api/tags")
            assert timeout == 5
            return FakeResp()

    monkeypatch.setattr(server_mod, "requests", FakeRequests())
    client = _client()
    body = client.get("/api/models").json()
    assert body == {
        "models": ["qwen2.5:7b", "llama3.1:8b"],
        "default": server_mod.config.MODEL,
    }


def test_models_endpoint_falls_back_on_failure(monkeypatch):
    """Any Ollama failure -> empty list, default still set."""

    class FakeRequests:
        @staticmethod
        def get(url, timeout=None):
            raise RuntimeError("ollama down")

    monkeypatch.setattr(server_mod, "requests", FakeRequests())
    client = _client()
    body = client.get("/api/models").json()
    assert body == {"models": [], "default": server_mod.config.MODEL}
