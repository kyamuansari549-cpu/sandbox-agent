"""Phase 1 tests. ALL Ollama HTTP calls are mocked — no live Ollama needed."""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod
import config as config_mod
import llm as llm_mod
import tools as tools_mod


def _tool_call(name, args):
    return {"function": {"name": name, "arguments": args}}


def test_loop_executes_tool_then_returns_final_answer(monkeypatch, tmp_path):
    """Loop: first LLM turn calls list_dir, tool result feeds back, final text returned."""
    (tmp_path / "hello.txt").write_text("hi")
    seen = {"n": 0, "messages": None}

    def fake_chat(messages, tools, **kw):
        seen["n"] += 1
        assert tools  # schemas are passed to the model
        if seen["n"] == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [_tool_call("list_dir", {"path": str(tmp_path)})],
            }
        seen["messages"] = messages
        return {"role": "assistant", "content": "Found your file."}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    result = agent_mod.run_agent("list my files", verbose=False)

    assert result == "Found your file."
    tool_msgs = [m for m in seen["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "hello.txt" in tool_msgs[0]["content"]


def test_run_command_timeout_enforced(monkeypatch):
    """A command exceeding its timeout is killed and reported, not hung."""
    monkeypatch.setattr(config_mod, "SANDBOX_ENABLED", False)  # host path
    result = tools_mod.run_command("sleep 5", timeout=1)
    assert result.startswith("ERROR:")
    assert "timed out" in result


def test_output_truncation(monkeypatch):
    """Oversized output is truncated with a marker, protecting the context."""
    monkeypatch.setattr(config_mod, "SANDBOX_ENABLED", False)  # host path
    monkeypatch.setattr(config_mod, "MAX_OUTPUT_CHARS", 100)
    result = tools_mod.run_command("python3 -c \"print('A' * 500)\"")
    assert "truncated" in result
    assert len(result) < 500


def test_bad_paths_return_error_strings():
    """Bad paths return ERROR text — never raise into the loop."""
    assert tools_mod.read_file("/definitely/not/here.txt").startswith("ERROR:")
    assert tools_mod.list_dir("/definitely/not/here").startswith("ERROR:")
    assert tools_mod.read_file("/tmp").startswith("ERROR:")  # a directory
    # and the happy path still works
    assert tools_mod.write_file("/tmp/sandbox-agent-test.txt", "abc").startswith("OK:")
    assert tools_mod.read_file("/tmp/sandbox-agent-test.txt") == "abc"
    os.remove("/tmp/sandbox-agent-test.txt")


def test_max_iterations_guard_triggers(monkeypatch):
    """A model that always calls tools is stopped after MAX_ITERS."""
    monkeypatch.setattr(config_mod, "MAX_ITERS", 3)

    def fake_chat(messages, tools, **kw):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [_tool_call("list_dir", {"path": "."})],
        }

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    result = agent_mod.run_agent("do something", verbose=False)
    assert "Stopped after 3 iterations" in result


def test_unknown_tool_name_handled(monkeypatch):
    """A hallucinated tool name becomes an ERROR tool message, not a crash."""
    def fake_chat(messages, tools, **kw):
        if not any(m.get("role") == "tool" for m in messages):
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [_tool_call("delete_everything", {})],
            }
        return {"role": "assistant", "content": "Recovered."}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    result = agent_mod.run_agent("test", verbose=False)
    assert result == "Recovered."


# --------------------------------------------------------------------------
# New contract: cancel, web approval gate, token streaming, usage, model.


def _approve_classified_tool_call():
    """One run_command call with an approve-classified command."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            _tool_call("run_command", {"command": "pip install something"})
        ],
    }


def test_cancel_already_set_yields_stopped(monkeypatch):
    """A pre-set cancel event stops the loop before any LLM call."""

    def fake_chat(messages, tools, **kw):
        raise AssertionError("llm.chat must not be called when cancelled")

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    cancel = threading.Event()
    cancel.set()
    events = list(agent_mod.stream_agent("hi", [], cancel=cancel))
    assert events == [{"type": "stopped"}]


def test_cancel_before_tool_call_yields_stopped(monkeypatch):
    """Cancel set during the LLM turn stops the run before the tool runs."""
    cancel = threading.Event()

    def fake_chat(messages, tools, **kw):
        cancel.set()  # user hits stop while the model is "thinking"
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [_tool_call("list_dir", {"path": "."})],
        }

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    events = list(agent_mod.stream_agent("hi", [], cancel=cancel))
    types = [e["type"] for e in events]
    assert "stopped" in types
    assert "tool_call" not in types  # the tool never ran


def test_approval_deny_cancels_command(monkeypatch):
    """Web approval gate: deny -> command never runs, cancellation recorded."""

    def fake_chat(messages, tools, **kw):
        if not any(m.get("role") == "tool" for m in messages):
            return _approve_classified_tool_call()
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    events = list(agent_mod.stream_agent("x", [], approver=lambda c, r: "deny"))

    types = [e["type"] for e in events]
    assert "approval_required" in types
    assert "approval_resolved" in types

    req = next(e for e in events if e["type"] == "approval_required")
    assert req["command"] == "pip install something"
    assert req["reason"]  # safety reason surfaced to the dialog

    res = next(e for e in events if e["type"] == "approval_resolved")
    assert res["decision"] == "deny"

    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["output"] == "ERROR: command cancelled by user."
    assert events[-1] == {"type": "answer", "content": "done"}
    assert tools_mod.web_mode.get() is False  # contextvar reset after the run


def test_approval_approve_runs_command(monkeypatch):
    """Web approval gate: approve -> command executes (post-approval path)."""
    monkeypatch.setattr(config_mod, "SANDBOX_ENABLED", False)  # host path
    ran = {}

    def fake_run_on_host(command, timeout):
        ran["command"] = command
        return "[exit code 0]\nran-fine"

    monkeypatch.setattr(tools_mod, "_run_on_host", fake_run_on_host)

    def fake_chat(messages, tools, **kw):
        if not any(m.get("role") == "tool" for m in messages):
            return _approve_classified_tool_call()
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    decisions = []
    events = list(
        agent_mod.stream_agent(
            "x", [], approver=lambda c, r: decisions.append((c, r)) or "approve"
        )
    )

    assert decisions and decisions[0][0] == "pip install something"
    assert ran.get("command") == "pip install something"  # actually executed
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert "ran-fine" in tool_result["output"]
    assert "cancelled" not in tool_result["output"]


def test_token_events_stream_live(monkeypatch):
    """on_token chunks become token events, in order, before the answer."""

    def fake_chat(messages, tools, **kw):
        on_token = kw["on_token"]
        on_token("Hello ")
        on_token("world")
        return {"role": "assistant", "content": "Hello world"}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    events = list(agent_mod.stream_agent("hi", []))

    tokens = [e for e in events if e["type"] == "token"]
    assert [t["content"] for t in tokens] == ["Hello ", "world"]
    types = [e["type"] for e in events]
    assert types.index("token") < types.index("answer")
    assert events[-1] == {"type": "answer", "content": "Hello world"}


def test_no_tokens_when_llm_does_not_stream(monkeypatch):
    """Instant (non-streaming) fakes produce no token events — old behavior."""

    def fake_chat(messages, tools, **kw):
        return {"role": "assistant", "content": "plain answer"}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    events = list(agent_mod.stream_agent("hi", []))
    assert not [e for e in events if e["type"] == "token"]
    assert events[-1] == {"type": "answer", "content": "plain answer"}


def test_usage_event_emitted_and_popped_from_history(monkeypatch):
    """_usage becomes a usage event and never lands in the message history."""

    def fake_chat(messages, tools, **kw):
        return {
            "role": "assistant",
            "content": "hi",
            "_usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    messages = []
    events = list(agent_mod.stream_agent("hi", messages))

    usage = next(e for e in events if e["type"] == "usage")
    assert usage == {"type": "usage", "prompt_tokens": 12, "completion_tokens": 3}
    assert all("_usage" not in m for m in messages)  # never goes back to Ollama


def test_model_kwarg_reaches_llm_chat(monkeypatch):
    """stream_agent(model=...) is forwarded to llm.chat."""
    seen = {}

    def fake_chat(messages, tools, **kw):
        seen["model"] = kw.get("model")
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    list(agent_mod.stream_agent("hi", [], model="llama3.1:8b"))
    assert seen["model"] == "llama3.1:8b"


def test_streaming_fallback_when_stream_drops_response(monkeypatch):
    """If streaming returns empty (Ollama quirk), retry once without streaming."""

    calls = []
    tool_call_given = []

    def fake_chat(messages, tools, **kw):
        calls.append(kw.get("on_token") is not None)
        if kw.get("on_token") is not None:
            # Simulate the Ollama streaming quirk: empty message back.
            return {"role": "assistant", "content": ""}
        if not tool_call_given:
            # Non-streaming retry: proper tool call (only once).
            tool_call_given.append(True)
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "list_dir", "arguments": {"path": "."}}}
                ],
            }
        # Next turn: final answer, loop ends.
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    monkeypatch.setattr(tools_mod, "call_tool", lambda name, args: "file1.txt")
    agent_mod._stream_broken.clear()
    try:
        events = list(agent_mod.stream_agent("list files", []))
    finally:
        agent_mod._stream_broken.clear()

    assert calls == [True, False, False]  # streamed, retried w/o stream, then 2nd turn skips streaming
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_calls) == 1 and tool_calls[0]["name"] == "list_dir"
    assert events[-1] == {"type": "answer", "content": "done"}


def test_streaming_skipped_after_quirk_seen(monkeypatch):
    """Once a model is flagged, later turns skip streaming entirely."""

    calls = []

    def fake_chat(messages, tools, **kw):
        calls.append(kw.get("on_token") is not None)
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    agent_mod._stream_broken.clear()
    try:
        agent_mod._stream_broken.add("quirky-model")
        list(agent_mod.stream_agent("hi", [], model="quirky-model"))
    finally:
        agent_mod._stream_broken.clear()

    assert calls == [False]  # no streaming attempted
