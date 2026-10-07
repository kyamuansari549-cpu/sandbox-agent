"""Server tests. ALL Ollama HTTP calls are mocked — no live Ollama needed."""

import json
import os
import sys

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


def _one_tool_then_answer(monkeypatch, path):
    """llm.chat: first turn calls list_dir, second turn answers."""
    seen = {"n": 0, "messages": None}

    def fake_chat(messages, tools):
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

    # session handshake is the FIRST event on a fresh chat
    assert types[0] == "session"
    assert events[0]["session_id"]
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
    assert events[0]["type"] == "session"
    new_id = events[0]["session_id"]
    assert new_id != "does-not-exist"
    assert new_id in server_mod._sessions


def test_multiturn_reuses_session_messages(monkeypatch, tmp_path):
    """A second turn with the same session_id keeps the conversation going."""
    (tmp_path / "hello.txt").write_text("hi")
    seen = _one_tool_then_answer(monkeypatch, tmp_path)
    client = _client()

    sid = _parse_sse(
        client.post("/api/chat", json={"message": "list my files"}).text
    )[0]["session_id"]
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
    def fake_chat(messages, tools):
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
    sid = _parse_sse(
        client.post("/api/chat", json={"message": long_msg}).text
    )[0]["session_id"]

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

    sid1 = _parse_sse(
        client.post("/api/chat", json={"message": "first chat"}).text
    )[0]["session_id"]
    sid2 = _parse_sse(
        client.post("/api/chat", json={"message": "second chat"}).text
    )[0]["session_id"]

    items = client.get("/api/sessions").json()
    assert len(items) == 2
    assert [i["session_id"] for i in items] == [sid2, sid1]
    assert items[0]["created_at"] >= items[1]["created_at"]


def test_session_history_returns_display_turns(monkeypatch, tmp_path):
    """History: one user turn + one assistant turn carrying the tool calls."""
    (tmp_path / "hello.txt").write_text("hi")
    _one_tool_then_answer(monkeypatch, tmp_path)
    client = _client()

    sid = _parse_sse(
        client.post("/api/chat", json={"message": "list my files"}).text
    )[0]["session_id"]

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

    sid = _parse_sse(
        client.post("/api/chat", json={"message": "first question"}).text
    )[0]["session_id"]
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
    def fake_chat(messages, tools):
        raise RuntimeError("Cannot reach Ollama at http://localhost:11434.")

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    client = _client()

    sid = _parse_sse(
        client.post("/api/chat", json={"message": "hi there"}).text
    )[0]["session_id"]

    display = client.get(f"/api/sessions/{sid}").json()["display"]
    assert len(display) == 2
    assert display[0]["role"] == "user"
    assert display[1]["role"] == "assistant"
    assert "Ollama" in display[1]["content"]
