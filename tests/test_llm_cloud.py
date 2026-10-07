"""llm.py cloud-backend tests: chain building, failover, OpenAI wire format.

ALL HTTP is stubbed — no live provider is ever contacted.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_mod
import llm as llm_mod


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, lines=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self._lines = lines or []
        self.text = text

    def json(self):
        return self._json

    def iter_lines(self):
        return iter(self._lines)


class _CloudStub:
    """Stub for requests: routes by URL substring, supports sequences."""

    def __init__(self, routes):
        # routes: list of (url_substring, response-or-list-of-responses)
        self._routes = [(sub, r if isinstance(r, list) else [r]) for sub, r in routes]
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None, stream=False):
        self.calls.append({"url": url, "payload": json, "headers": headers,
                           "stream": stream})
        for sub, resps in self._routes:
            if sub in url:
                resp = resps.pop(0) if len(resps) > 1 else resps[0]
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected URL: {url}")


def _openai_ok(content="hi", tool_calls=None, usage=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return _FakeResp(json_data={
        "choices": [{"message": msg, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 3},
    })


def _use_cloud(monkeypatch, stub):
    monkeypatch.setattr(config_mod, "LLM_PROVIDER", "cloud")
    monkeypatch.setattr(llm_mod, "requests", stub)
    llm_mod._cooldowns.clear()


def _chain_env(monkeypatch):
    monkeypatch.setenv("SANDBOX_AGENT_LLM_CHAIN", "groq,gemini")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test1")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza_test2")


# --------------------------------------------------------------------------
# chain building
# --------------------------------------------------------------------------

def test_cloud_chain_builds_ordered_entries(monkeypatch):
    _chain_env(monkeypatch)
    chain = config_mod.cloud_chain()
    assert [e["provider"] for e in chain] == ["groq", "gemini"]
    assert chain[0]["base_url"] == "https://api.groq.com/openai/v1"
    assert chain[0]["api_key"] == "gsk_test1"
    assert chain[0]["model"] == "openai/gpt-oss-120b"  # default
    assert "generativelanguage" in chain[1]["base_url"]


def test_cloud_chain_multiple_keys_same_provider(monkeypatch):
    monkeypatch.setenv("SANDBOX_AGENT_LLM_CHAIN", "groq")
    monkeypatch.setenv("GROQ_API_KEYS", "gsk_a,gsk_b")
    chain = config_mod.cloud_chain()
    assert len(chain) == 2
    assert [e["api_key"] for e in chain] == ["gsk_a", "gsk_b"]


def test_cloud_chain_skips_providers_without_keys(monkeypatch):
    monkeypatch.setenv("SANDBOX_AGENT_LLM_CHAIN", "groq,gemini,nvidia")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    chain = config_mod.cloud_chain()
    assert [e["provider"] for e in chain] == ["groq"]


def test_cloud_chain_model_override(monkeypatch):
    monkeypatch.setenv("SANDBOX_AGENT_LLM_CHAIN", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    monkeypatch.setenv("GROQ_MODEL", "llama-3.1-8b-instant")
    chain = config_mod.cloud_chain()
    assert chain[0]["model"] == "llama-3.1-8b-instant"


def test_cloud_chain_ignores_unknown_provider(monkeypatch):
    monkeypatch.setenv("SANDBOX_AGENT_LLM_CHAIN", "groq,bogus")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    chain = config_mod.cloud_chain()
    assert [e["provider"] for e in chain] == ["groq"]


# --------------------------------------------------------------------------
# model-string -> entry selection
# --------------------------------------------------------------------------

def _sample_chain():
    return [
        {"provider": "groq", "base_url": "u1", "api_key": "k1", "model": "m1"},
        {"provider": "groq", "base_url": "u1", "api_key": "k2", "model": "m1"},
        {"provider": "gemini", "base_url": "u2", "api_key": "k3", "model": "m2"},
    ]


def test_select_entries_none_uses_whole_chain():
    assert len(llm_mod._select_entries(_sample_chain(), None)) == 3


def test_select_entries_provider_only():
    entries = llm_mod._select_entries(_sample_chain(), "groq")
    assert [e["api_key"] for e in entries] == ["k1", "k2"]


def test_select_entries_provider_and_model_override():
    entries = llm_mod._select_entries(_sample_chain(), "gemini:gemini-2.5-flash")
    assert len(entries) == 1
    assert entries[0]["model"] == "gemini-2.5-flash"


def test_select_entries_ollama_style_name_falls_back_to_chain():
    # "qwen2.5:7b" is not a chain provider — use the whole chain, don't fail.
    assert len(llm_mod._select_entries(_sample_chain(), "qwen2.5:7b")) == 3


# --------------------------------------------------------------------------
# failover
# --------------------------------------------------------------------------

def test_cloud_failover_on_429(monkeypatch):
    _chain_env(monkeypatch)
    stub = _CloudStub([
        ("api.groq.com", _FakeResp(status_code=429, text="rate limited")),
        ("generativelanguage", _openai_ok("hello from gemini")),
    ])
    _use_cloud(monkeypatch, stub)
    msg = llm_mod.chat([{"role": "user", "content": "x"}], [])
    assert msg["content"] == "hello from gemini"
    assert len(stub.calls) == 2
    assert "api.groq.com" in stub.calls[0]["url"]
    assert "generativelanguage" in stub.calls[1]["url"]
    # groq entry is now cooling down
    assert llm_mod._in_cooldown({"provider": "groq", "api_key": "gsk_test1"})


def test_cloud_failover_on_500_then_401(monkeypatch):
    _chain_env(monkeypatch)
    stub = _CloudStub([
        ("api.groq.com", _FakeResp(status_code=500, text="boom")),
        ("generativelanguage", _FakeResp(status_code=401, text="bad key")),
    ])
    _use_cloud(monkeypatch, stub)
    with pytest.raises(RuntimeError, match="All cloud LLM providers failed"):
        llm_mod.chat([{"role": "user", "content": "x"}], [])
    assert len(stub.calls) == 2


def test_cloud_failover_on_network_error(monkeypatch):
    import requests as _rq
    _chain_env(monkeypatch)
    stub = _CloudStub([
        ("api.groq.com", _rq.ConnectionError("dns fail")),
        ("generativelanguage", _openai_ok("recovered")),
    ])
    _use_cloud(monkeypatch, stub)
    msg = llm_mod.chat([{"role": "user", "content": "x"}], [])
    assert msg["content"] == "recovered"


def test_cloud_400_does_not_failover(monkeypatch):
    _chain_env(monkeypatch)
    stub = _CloudStub([
        ("api.groq.com", _FakeResp(status_code=400, text="bad request")),
    ])
    _use_cloud(monkeypatch, stub)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        llm_mod.chat([{"role": "user", "content": "x"}], [])
    assert len(stub.calls) == 1  # no failover on plain 400


def test_cloud_cooldown_skips_entry(monkeypatch):
    _chain_env(monkeypatch)
    stub = _CloudStub([
        ("api.groq.com", _FakeResp(status_code=429, text="slow down")),
        ("generativelanguage", _openai_ok("second try works")),
    ])
    _use_cloud(monkeypatch, stub)
    llm_mod.chat([{"role": "user", "content": "x"}], [])
    # groq cooling down now: next chat goes straight to gemini
    stub2_calls = stub.calls
    assert len(stub2_calls) == 2
    msg = llm_mod.chat([{"role": "user", "content": "y"}], [])
    assert msg["content"] == "second try works"
    assert len(stub.calls) == 3
    assert "generativelanguage" in stub.calls[2]["url"]


def test_cloud_no_chain_configured(monkeypatch):
    monkeypatch.setenv("SANDBOX_AGENT_LLM_CHAIN", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEYS", raising=False)
    stub = _CloudStub([])
    _use_cloud(monkeypatch, stub)
    with pytest.raises(RuntimeError, match="no cloud chain entries"):
        llm_mod.chat([{"role": "user", "content": "x"}], [])


def test_cloud_auth_header_sent(monkeypatch):
    _chain_env(monkeypatch)
    stub = _CloudStub([("api.groq.com", _openai_ok())])
    _use_cloud(monkeypatch, stub)
    llm_mod.chat([{"role": "user", "content": "x"}], [])
    assert stub.calls[0]["headers"]["Authorization"] == "Bearer gsk_test1"
    assert stub.calls[0]["url"].endswith("/chat/completions")


# --------------------------------------------------------------------------
# OpenAI wire format
# --------------------------------------------------------------------------

def test_cloud_nonstreaming_tool_call_shape(monkeypatch):
    _chain_env(monkeypatch)
    tc = [{"id": "call_9", "type": "function",
           "function": {"name": "list_dir", "arguments": '{"path":"."}'}}]
    stub = _CloudStub([("api.groq.com", _openai_ok("", tool_calls=tc))])
    _use_cloud(monkeypatch, stub)
    msg = llm_mod.chat([{"role": "user", "content": "x"}],
                       [{"type": "function", "function": {"name": "list_dir"}}])
    # Ollama-shaped: agent.py unchanged
    assert msg["role"] == "assistant"
    fn = msg["tool_calls"][0]["function"]
    assert fn["name"] == "list_dir"
    assert fn["arguments"] == {"path": "."}  # parsed to dict
    assert msg["_usage"] == {"prompt_tokens": 10, "completion_tokens": 3}
    # tools passed through in OpenAI format
    assert stub.calls[0]["payload"]["tools"][0]["function"]["name"] == "list_dir"


def test_to_openai_messages_synthesizes_ids(monkeypatch):
    history = [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "list_dir", "arguments": {"path": "."}}},
                        {"function": {"name": "read_file", "arguments": {"path": "a"}}}]},
        {"role": "tool", "content": "f1"},
        {"role": "tool", "content": "f2"},
    ]
    wire = llm_mod._to_openai_messages(history)
    asst = wire[1]
    assert asst["tool_calls"][0]["id"] == "call_1"
    assert asst["tool_calls"][1]["id"] == "call_2"
    assert asst["tool_calls"][0]["function"]["arguments"] == '{"path": "."}'
    assert wire[2] == {"role": "tool", "tool_call_id": "call_1", "content": "f1"}
    assert wire[3] == {"role": "tool", "tool_call_id": "call_2", "content": "f2"}


def test_to_openai_messages_drops_usage_key(monkeypatch):
    history = [{"role": "assistant", "content": "hi", "_usage": {"a": 1}}]
    wire = llm_mod._to_openai_messages(history)
    assert wire == [{"role": "assistant", "content": "hi"}]


def test_cloud_streaming_accumulates_tool_fragments(monkeypatch):
    _chain_env(monkeypatch)

    def _sse(payload):
        return ("data: " + json.dumps(payload)).encode()

    lines = [
        # content chunks
        _sse({"choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]}),
        # tool call fragments across chunks (OpenAI streams them piecemeal)
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1",
             "function": {"name": "list", "arguments": ""}}]}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"path":'}}]}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '"."}'}}]}}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        _sse({"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 4}}),
        b"data: [DONE]",
    ]
    stub = _CloudStub([
        ("api.groq.com", _FakeResp(lines=lines)),
    ])
    _use_cloud(monkeypatch, stub)
    chunks = []
    msg = llm_mod.chat([{"role": "user", "content": "x"}], [],
                       on_token=chunks.append)
    assert chunks == ["Hel", "lo"]
    assert msg["content"] == "Hello"
    fn = msg["tool_calls"][0]["function"]
    assert fn["name"] == "list"
    assert fn["arguments"] == {"path": "."}
    assert msg["_usage"] == {"prompt_tokens": 11, "completion_tokens": 4}
    assert stub.calls[0]["payload"]["stream"] is True
    assert stub.calls[0]["payload"]["stream_options"] == {"include_usage": True}


def test_cloud_streaming_failover_midstream(monkeypatch):
    import requests as _rq
    _chain_env(monkeypatch)
    stub = _CloudStub([
        ("api.groq.com", _rq.ConnectionError("reset")),
        ("generativelanguage", _openai_ok("ok")),
    ])
    _use_cloud(monkeypatch, stub)
    # non-streaming request after a mid-setup failure still fails over
    msg = llm_mod.chat([{"role": "user", "content": "x"}], [])
    assert msg["content"] == "ok"
