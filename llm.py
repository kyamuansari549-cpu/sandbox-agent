"""LLM client for SandboxAgent: local Ollama or OpenAI-compatible cloud APIs.

Two backends, one contract — chat() always returns an Ollama-shaped message
dict so agent.py never needs to know which backend answered:

    {"role": "assistant", "content": "...",
     "tool_calls": [{"function": {"name": ..., "arguments": {...}}}]}
    ("tool_calls" is absent/empty when the model is done and just answering.)

The returned dict always carries "_usage" =
{"prompt_tokens": int, "completion_tokens": int}. The agent loop pops
"_usage" before appending the message to history, so it never goes back
to any provider.

Backends (config.LLM_PROVIDER):
- "ollama" (default): local Ollama's native /api/chat endpoint.
- "cloud": OpenAI-compatible endpoints (Groq, Gemini, NVIDIA, ...) with an
  ordered fallback chain — when one key/provider hits a rate limit (429),
  a server error (5xx) or a network failure, the next chain entry
  automatically takes over.

Raises RuntimeError with a human-readable message when every backend fails —
agent.py turns it into a clean CLI error.
"""

import json
import time

import requests
from requests import exceptions as _rq_exc

import config

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TIMEOUT = 120  # seconds per request


def _usage_from_openai(usage):
    usage = usage or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
    }


def _parse_arguments(argstr):
    """Tool arguments arrive as a JSON string on OpenAI-compatible APIs."""
    if isinstance(argstr, dict):
        return argstr
    if not argstr:
        return {}
    try:
        parsed = json.loads(argstr)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# Backend: local Ollama (native /api/chat)
# ---------------------------------------------------------------------------

def _ollama_chat(messages, tools, model=None, on_token=None):
    model = config.MODEL if model is None else model
    streaming = on_token is not None
    url = f"{config.OLLAMA_HOST}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "stream": streaming,
    }
    try:
        resp = requests.post(url, json=payload, timeout=_TIMEOUT, stream=streaming)
    except _rq_exc.ConnectionError as exc:
        raise RuntimeError(
            f"Cannot reach Ollama at {config.OLLAMA_HOST}. "
            "Is Ollama running? Start it (Ollama app, or `ollama serve`), "
            "pull a tool-capable model (`ollama pull qwen2.5:7b`), then retry."
        ) from exc
    except _rq_exc.Timeout as exc:
        raise RuntimeError(
            f"Ollama at {config.OLLAMA_HOST} took too long to answer "
            f"(>{_TIMEOUT}s). The model may be overloaded — try again."
        ) from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"Ollama returned HTTP {resp.status_code}: {resp.text[:500]}"
        )
    if not streaming:
        data = resp.json()
        if "message" not in data:
            raise RuntimeError(
                f"Unexpected Ollama response (no 'message' key): {str(data)[:500]}"
            )
        message = data["message"]
        message["_usage"] = {
            "prompt_tokens": int(data.get("prompt_eval_count") or 0),
            "completion_tokens": int(data.get("eval_count") or 0),
        }
        return message
    # Streaming: emit content chunks as they arrive, then assemble the
    # final message from the done:true frame (it carries tool_calls and
    # the eval counts). The done frame's own content is usually empty
    # (content was streamed in chunks), so fall back to the chunks.
    chunks = []
    final_frame = None
    for line in resp.iter_lines():
        if not line:
            continue
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        msg = frame.get("message") or {}
        content = msg.get("content") or ""
        if content:
            chunks.append(content)
            on_token(content)
        if frame.get("done"):
            final_frame = frame
            break
    if final_frame is None:
        raise RuntimeError("Ollama closed the stream without a final message.")
    message = final_frame.get("message") or {}
    if not message.get("content"):
        message["content"] = "".join(chunks)
    message.setdefault("role", "assistant")
    message["_usage"] = {
        "prompt_tokens": int(final_frame.get("prompt_eval_count") or 0),
        "completion_tokens": int(final_frame.get("eval_count") or 0),
    }
    return message


# ---------------------------------------------------------------------------
# Backend: OpenAI-compatible cloud APIs with fallback chain
# ---------------------------------------------------------------------------

class _Failover(Exception):
    """A chain entry failed in a way the next entry should be tried for."""


# entry_id -> unix timestamp until which the entry is skipped (rate-limited).
_cooldowns = {}


def _entry_id(entry):
    return (entry["provider"], entry["api_key"][-6:])


def _in_cooldown(entry):
    return _cooldowns.get(_entry_id(entry), 0) > time.time()


def _set_cooldown(entry):
    _cooldowns[_entry_id(entry)] = time.time() + config.LLM_FAILOVER_COOLDOWN


def _select_entries(chain, model):
    """Filter/override the chain from the session's model string.

    model may be None (whole chain), "groq" (that provider's entries), or
    "groq:some-model" (that provider's entries with an overridden model).
    A model string that doesn't name a chain provider (e.g. the Ollama
    default "qwen2.5:7b") falls back to the whole chain instead of failing.
    """
    providers = {e["provider"] for e in chain}
    provider_hint, model_override = None, None
    if isinstance(model, str) and model.strip():
        text = model.strip()
        if ":" in text:
            maybe_provider, maybe_model = text.split(":", 1)
            maybe_provider = maybe_provider.strip().lower()
            if maybe_provider in providers:
                provider_hint = maybe_provider
                model_override = maybe_model.strip() or None
            # else: not a cloud model string — use the whole chain
        elif text.lower() in providers:
            provider_hint = text.lower()
    entries = []
    for entry in chain:
        if provider_hint and entry["provider"] != provider_hint:
            continue
        entry = dict(entry)
        if model_override:
            entry["model"] = model_override
        entries.append(entry)
    return entries


def _to_openai_messages(messages):
    """Convert agent history to OpenAI chat format.

    OpenAI requires every tool_call to carry an "id", and every "tool"
    message to reference it via "tool_call_id". The agent loop stores
    Ollama-shaped turns (no ids), so we synthesize stable ids in one
    pass: assistant tool_calls get call_1, call_2, ... and each following
    tool message takes the next pending id in order (which matches the
    order agent.py appends them).
    """
    out = []
    pending_ids = []
    counter = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            tcs = []
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                counter += 1
                cid = f"call_{counter}"
                pending_ids.append(cid)
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                argstr = args if isinstance(args, str) else json.dumps(args or {})
                tcs.append(
                    {
                        "id": cid,
                        "type": "function",
                        "function": {
                            "name": fn.get("name") or "",
                            "arguments": argstr,
                        },
                    }
                )
            out.append(
                {
                    "role": "assistant",
                    "content": m.get("content") or "",
                    "tool_calls": tcs,
                }
            )
        elif role == "tool":
            cid = m.get("tool_call_id")
            if not cid:
                cid = pending_ids.pop(0) if pending_ids else f"call_{counter + 1}"
                counter += 1
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": cid,
                    "content": str(m.get("content") or ""),
                }
            )
        elif role in ("user", "assistant", "system"):
            out.append({"role": role, "content": m.get("content") or ""})
        # "_usage" and anything else never goes back to a provider.
    return out


def _openai_request(entry, messages, tools, on_token):
    """One POST to an OpenAI-compatible /chat/completions endpoint.

    Returns an Ollama-shaped message dict. Raises _Failover when the next
    chain entry should be tried (429/5xx/401/network), RuntimeError for
    anything else.
    """
    streaming = on_token is not None
    url = f"{entry['base_url']}/chat/completions"
    payload = {
        "model": entry["model"],
        "messages": messages,
        "stream": streaming,
    }
    if tools:
        payload["tools"] = tools  # Ollama tool schema == OpenAI tool schema
    if streaming:
        payload["stream_options"] = {"include_usage": True}
    headers = {
        "Authorization": f"Bearer {entry['api_key']}",
        "Content-Type": "application/json",
    }
    label = f"{entry['provider']}:{entry['model']}"
    try:
        resp = requests.post(url, json=payload, headers=headers,
                             timeout=_TIMEOUT, stream=streaming)
    except (_rq_exc.ConnectionError, _rq_exc.Timeout) as exc:
        raise _Failover(f"{label}: network error ({exc})") from exc
    except _rq_exc.RequestException as exc:
        raise _Failover(f"{label}: request failed ({exc})") from exc

    status = resp.status_code
    if status == 429:
        raise _Failover(f"{label}: rate limited (HTTP 429)")
    if status == 401:
        raise _Failover(f"{label}: bad API key (HTTP 401)")
    if 500 <= status <= 599:
        raise _Failover(f"{label}: server error (HTTP {status})")
    if status != 200:
        raise RuntimeError(f"{label} returned HTTP {status}: {resp.text[:500]}")

    if not streaming:
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"{label}: bad JSON response") from exc
        choices = data.get("choices") or []
        if not choices or "message" not in choices[0]:
            raise RuntimeError(
                f"{label}: unexpected response (no choices[0].message): "
                f"{str(data)[:500]}"
            )
        return _openai_message_to_ollama(choices[0]["message"],
                                         data.get("usage"))

    return _openai_stream_to_ollama(resp, on_token, label)


def _openai_message_to_ollama(msg, usage):
    """Non-streamed OpenAI message -> Ollama-shaped dict."""
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        fn = (tc or {}).get("function") or {}
        tool_calls.append(
            {"function": {"name": fn.get("name") or "",
                          "arguments": _parse_arguments(fn.get("arguments"))}}
        )
    return {
        "role": "assistant",
        "content": msg.get("content") or "",
        "tool_calls": tool_calls,
        "_usage": _usage_from_openai(usage),
    }


def _openai_stream_to_ollama(resp, on_token, label):
    """Accumulate an OpenAI SSE stream into an Ollama-shaped message dict.

    Content deltas go to on_token live; tool_call deltas arrive as
    fragments keyed by index and are stitched together at the end.
    """
    content_chunks = []
    # index -> {"id":..., "name":..., "arguments": "..."}
    tc_fragments = {}
    usage = None
    try:
        for line in resp.iter_lines():
            if not line:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except ValueError:
                continue
            choices = event.get("choices") or []
            if event.get("usage"):
                usage = event["usage"]
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                content_chunks.append(content)
                on_token(content)
            for frag in delta.get("tool_calls") or []:
                idx = frag.get("index", 0)
                slot = tc_fragments.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if frag.get("id"):
                    slot["id"] = frag["id"]
                fn = frag.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            if choices[0].get("finish_reason") and not tc_fragments and not content_chunks:
                # empty finish with no data yet — keep reading (usage may follow)
                continue
    except _rq_exc.RequestException as exc:
        raise _Failover(f"{label}: stream broke ({exc})") from exc

    tool_calls = []
    for idx in sorted(tc_fragments):
        slot = tc_fragments[idx]
        if not slot["name"]:
            continue
        tool_calls.append(
            {"function": {"name": slot["name"],
                          "arguments": _parse_arguments(slot["arguments"])}}
        )
    return {
        "role": "assistant",
        "content": "".join(content_chunks),
        "tool_calls": tool_calls,
        "_usage": _usage_from_openai(usage),
    }


def _cloud_chat(messages, tools, model=None, on_token=None):
    """Walk the fallback chain until one entry answers."""
    chain = config.cloud_chain()
    entries = _select_entries(chain, model)
    if not entries:
        raise RuntimeError(
            "LLM_PROVIDER=cloud but no cloud chain entries are configured. "
            "Set e.g. SANDBOX_AGENT_LLM_CHAIN=\"groq\" and GROQ_API_KEY, "
            "then retry."
        )
    wire_messages = _to_openai_messages(messages)
    tried = []
    for entry in entries:
        if _in_cooldown(entry):
            tried.append(f"{entry['provider']}:{entry['model']} (cooling down)")
            continue
        try:
            return _openai_request(entry, wire_messages, tools, on_token)
        except _Failover as exc:
            _set_cooldown(entry)
            tried.append(str(exc))
            continue
    raise RuntimeError(
        "All cloud LLM providers failed:\n- " + "\n- ".join(tried)
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def chat(messages, tools, model=None, on_token=None):
    """POST to the configured LLM backend and return the response message.

    Args:
        messages: the messages list (Ollama-shaped; converted as needed).
        tools: tool schemas (Ollama format == OpenAI format).
        model: model name; defaults to config.MODEL. In cloud mode it may
            be "provider" or "provider:model" to pin the chain.
        on_token: when given, the request streams and on_token(chunk) is
            called for every non-empty content chunk as it arrives.

    Returns: Ollama-shaped message dict with "_usage" (see module docstring).
    Raises RuntimeError when every backend fails.
    """
    if config.LLM_PROVIDER == "cloud":
        return _cloud_chat(messages, tools, model=model, on_token=on_token)
    return _ollama_chat(messages, tools, model=model, on_token=on_token)
