"""llm.py tests: model selection, usage capture, streaming assembly.

ALL HTTP is stubbed — no live Ollama needed.
"""

import json
import os
import sys

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


class _StubRequests:
    """Stands in for the `requests` module inside llm.py."""

    def __init__(self, resp):
        self._resp = resp
        self.seen = {}

    def post(self, url, json=None, timeout=None, stream=False):
        self.seen = {"url": url, "payload": json, "stream": stream}
        return self._resp


def test_non_streaming_defaults_and_captures_usage():
    stub = _StubRequests(
        _FakeResp(
            json_data={
                "message": {"role": "assistant", "content": "hi"},
                "prompt_eval_count": 25,
                "eval_count": 7,
            }
        )
    )
    llm_mod.requests = stub
    try:
        msg = llm_mod.chat([{"role": "user", "content": "x"}], [])
    finally:
        llm_mod.requests = sys.modules["requests"]
    assert msg["content"] == "hi"
    assert msg["_usage"] == {"prompt_tokens": 25, "completion_tokens": 7}
    assert stub.seen["payload"]["stream"] is False
    assert stub.seen["payload"]["model"] == config_mod.MODEL  # default
    assert stub.seen["stream"] is False


def test_non_streaming_explicit_model_and_missing_counts():
    stub = _StubRequests(
        _FakeResp(json_data={"message": {"role": "assistant", "content": "ok"}})
    )
    llm_mod.requests = stub
    try:
        msg = llm_mod.chat([], [], model="llama3.1:8b")
    finally:
        llm_mod.requests = sys.modules["requests"]
    assert stub.seen["payload"]["model"] == "llama3.1:8b"
    assert msg["_usage"] == {"prompt_tokens": 0, "completion_tokens": 0}


def test_streaming_assembles_from_done_frame():
    frames = [
        {"message": {"role": "assistant", "content": "Hel"}, "done": False},
        {"message": {"role": "assistant", "content": "lo"}, "done": False},
        {
            "message": {
                "role": "assistant",
                "content": "",  # Ollama puts streamed content in chunks, not the done frame
                "tool_calls": [
                    {"function": {"name": "list_dir", "arguments": {"path": "."}}}
                ],
            },
            "done": True,
            "prompt_eval_count": 30,
            "eval_count": 5,
        },
    ]
    stub = _StubRequests(
        _FakeResp(lines=[json.dumps(f).encode() for f in frames])
    )
    llm_mod.requests = stub
    chunks = []
    try:
        msg = llm_mod.chat(
            [{"role": "user", "content": "x"}], [], on_token=chunks.append
        )
    finally:
        llm_mod.requests = sys.modules["requests"]
    assert stub.seen["payload"]["stream"] is True
    assert stub.seen["stream"] is True
    assert chunks == ["Hel", "lo"]  # live chunks delivered
    assert msg["content"] == "Hello"  # assembled from chunks (done frame was empty)
    assert msg["tool_calls"][0]["function"]["name"] == "list_dir"
    assert msg["_usage"] == {"prompt_tokens": 30, "completion_tokens": 5}
