"""SandboxAgent Phase 1: CLI agent loop with tools.

The loop lives in stream_agent(), which yields event dicts (consumed by
run_agent() for the CLI and by server.py for the web UI's SSE stream).

Event contract:
  {"type": "status", "message": "Thinking..."}     - human-readable progress
  {"type": "tool_call", "name": <tool>, "args": <dict>}
  {"type": "tool_result", "name": <tool>, "output": <str>}  - output truncated
  {"type": "answer", "content": <final text>}       - also emitted on max-iters stop
  {"type": "error", "message": <str>}               - e.g. Ollama down; never raised out

Usage:
    python agent.py "your task here"
"""

import json
import sys

import config
import llm
import session
from tools import TOOLS, TOOL_SCHEMAS

SYSTEM_PROMPT = (
    "You are SandboxAgent, a friendly AI assistant with tools — think of "
    "yourself as a warm, capable friend who happens to be great with computers.\n\n"
    "PERSONALITY:\n"
    "- Warm, friendly, a little playful. Talk like a helpful friend, never like a manual.\n"
    "- Match the user's language: if they write in Hinglish or Hindi, reply in Hinglish (Roman script); "
    "if they write in English, reply in English.\n"
    "- When writing Hinglish, use SIMPLE everyday words and keep every sentence short "
    "(under 15 words). Never invent fancy Hindi words — if you don't know the natural "
    "word, just use the English word instead. Example tone: 'Samajh gaya! Ye kaam main "
    "kar dunga. Pehle files dekh leta hun.'\n"
    "- Keep answers conversational and reasonably concise. No robotic filler phrases.\n"
    "- Be honest: never claim you did something you didn't do, never invent tool results. "
    "If a tool failed, say so plainly and try another way.\n"
    "- Never narrate your tool calls in prose (no 'I will now call list_dir...') — "
    "the UI already shows your activity step-by-step. Just do the work, then summarize.\n\n"
    "WORK STYLE:\n"
    "- Think step-by-step: gather information or take action with tools, read the results, "
    "repeat until the task is done, then reply with a short summary of what you did.\n"
    "- If a step fails, try a different approach once or twice before giving up.\n\n"
    "IMPORTANT — every final reply you write MUST end with exactly one of these, on its own line:\n"
    "- a short follow-up question, or\n"
    "- a helpful suggestion for what the user could try next.\n"
    "Keep it to one line and make it sound natural, like something a friend would ask."
)

# Tool-result payloads in events are truncated harder than the model's
# context-window cap: the UI only needs a preview.
EVENT_OUTPUT_CHARS = 2000


def _coerce_args(args):
    # Ollama's native API already gives a dict; be tolerant of a JSON string.
    if isinstance(args, str):
        try:
            return json.loads(args)
        except json.JSONDecodeError:
            return {}
    return args or {}


def stream_agent(task, messages):
    """Run the agent loop, yielding event dicts (see module docstring).

    Appends the user task to `messages` and mutates the list in place as the
    conversation grows (assistant turns, tool results). Raises nothing:
    Ollama failures surface as an {"type": "error"} event instead.
    """
    messages.append({"role": "user", "content": task})
    for _ in range(config.MAX_ITERS):
        yield {"type": "status", "message": "Thinking..."}
        try:
            message = llm.chat(messages, TOOL_SCHEMAS)
        except RuntimeError as exc:
            # e.g. Ollama unreachable — report, don't raise out of the generator
            yield {"type": "error", "message": str(exc)}
            return
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            final = message.get("content", "") or "(no answer returned)"
            yield {"type": "answer", "content": final}
            return
        messages.append(message)  # keep the assistant turn incl. tool_calls
        for tc in tool_calls:
            func = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = func.get("name", "")
            args = _coerce_args(func.get("arguments"))
            yield {"type": "status", "message": f"Calling {name}..."}
            yield {"type": "tool_call", "name": name, "args": args}
            fn = TOOLS.get(name)
            if fn is None:
                result = f"ERROR: unknown tool '{name}'"
            else:
                try:
                    result = fn(**args)
                except Exception as exc:  # last-resort guard; tools shouldn't raise
                    result = f"ERROR: tool '{name}' crashed: {exc}"
            session.log_turn(name, args, result)  # Phase 3: never raises
            messages.append({"role": "tool", "content": str(result)})
            yield {
                "type": "tool_result",
                "name": name,
                "output": str(result)[:EVENT_OUTPUT_CHARS],
            }
    stop_note = (
        f"Stopped after {config.MAX_ITERS} iterations without finishing. "
        "Try a smaller task, or raise SANDBOX_AGENT_MAX_ITERS."
    )
    yield {"type": "answer", "content": stop_note}


def run_agent(task, verbose=True):
    """Run the agent loop for one task. Returns the final text (or stop note).

    Thin consumer of stream_agent(): CLI output is unchanged —
    `[tool] name({...})` lines plus the final text. Raises RuntimeError on
    the {"type": "error"} event so main() prints the same clean CLI error.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    final = None
    for event in stream_agent(task, messages):
        etype = event["type"]
        if etype == "tool_call":
            if verbose:
                print(f"[tool] {event['name']}({json.dumps(event['args'])[:200]})")
        elif etype == "answer":
            final = event["content"]
            if verbose:
                print(final)
        elif etype == "error":
            raise RuntimeError(event["message"])
        # status events are for the web UI; the CLI stays quiet
    return final


def main(argv):
    if len(argv) < 2:
        print('Usage: python agent.py "your task here"')
        return 1
    try:
        run_agent(argv[1])
    except RuntimeError as exc:
        # e.g. Ollama unreachable — clean message, no traceback
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
