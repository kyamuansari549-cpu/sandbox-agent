"""Thin wrapper over Ollama's native /api/chat endpoint. No SDK needed."""

import requests

import config


def chat(messages, tools):
    """POST to Ollama /api/chat and return the response message dict.

    Returns: response["message"], e.g.
        {"role": "assistant", "content": "...",
         "tool_calls": [{"function": {"name": ..., "arguments": {...}}}]}
    ("tool_calls" is absent/empty when the model is done and just answering.)

    Raises RuntimeError with a human-readable message if Ollama is
    unreachable or returns an error — the agent loop catches nothing here,
    agent.py turns it into a clean CLI error.
    """
    url = f"{config.OLLAMA_HOST}/api/chat"
    payload = {
        "model": config.MODEL,
        "messages": messages,
        "tools": tools,
        "stream": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=120)
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
    data = resp.json()
    if "message" not in data:
        raise RuntimeError(f"Unexpected Ollama response (no 'message' key): {str(data)[:500]}")
    return data["message"]
