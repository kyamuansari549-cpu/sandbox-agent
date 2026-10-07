"""Thin wrapper over Ollama's native /api/chat endpoint. No SDK needed."""

import json

import requests

import config


def chat(messages, tools, model=None, on_token=None):
    """POST to Ollama /api/chat and return the response message dict.

    Args:
        messages: the Ollama messages list.
        tools: Ollama-format tool schemas.
        model: model name; defaults to config.MODEL when None.
        on_token: when given, the request streams and on_token(chunk) is
            called for every non-empty content chunk as it arrives.

    Returns: response["message"], e.g.
        {"role": "assistant", "content": "...",
         "tool_calls": [{"function": {"name": ..., "arguments": {...}}}]}
    ("tool_calls" is absent/empty when the model is done and just answering.)

    The returned dict always carries "_usage" =
    {"prompt_tokens": int, "completion_tokens": int} taken from Ollama's
    top-level prompt_eval_count / eval_count (0 when absent). The agent
    loop pops "_usage" before appending the message to history, so it
    never goes back to Ollama.

    Raises RuntimeError with a human-readable message if Ollama is
    unreachable or returns an error — the agent loop catches nothing here,
    agent.py turns it into a clean CLI error.
    """
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
        resp = requests.post(url, json=payload, timeout=120, stream=streaming)
    except requests.ConnectionError as exc:
        raise RuntimeError(
            f"Cannot reach Ollama at {config.OLLAMA_HOST}. "
            "Is Ollama running? Start it (Ollama app, or `ollama serve`), "
            "pull a tool-capable model (`ollama pull qwen2.5:7b`), then retry."
        ) from exc
    except requests.Timeout as exc:
        raise RuntimeError(
            f"Ollama at {config.OLLAMA_HOST} took too long to answer "
            "(>120s). The model may be overloaded — try again."
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
