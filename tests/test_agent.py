"""Phase 1 tests. ALL Ollama HTTP calls are mocked — no live Ollama needed."""

import os
import sys

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

    def fake_chat(messages, tools):
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

    def fake_chat(messages, tools):
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
    def fake_chat(messages, tools):
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
