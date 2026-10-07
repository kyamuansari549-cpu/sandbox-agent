"""SandboxAgent Phase 1: CLI agent loop with tools.

The loop lives in stream_agent(), which yields event dicts (consumed by
run_agent() for the CLI and by server.py for the web UI's SSE stream).

Event contract:
  {"type": "status", "message": "Thinking..."}     - human-readable progress
  {"type": "token", "content": <chunk>}           - live model output chunk
  {"type": "usage", "prompt_tokens": n, "completion_tokens": n} - per-turn tokens
  {"type": "tool_call", "name": <tool>, "args": <dict>}
  {"type": "tool_result", "name": <tool>, "output": <str>}  - output truncated
  {"type": "answer", "content": <final text>}       - also emitted on max-iters stop
  {"type": "error", "message": <str>}               - e.g. Ollama down; never raised out
  {"type": "approval_required", "run_id": ..., "command": ..., "reason": ...}
  {"type": "approval_resolved", "decision": "approve"|"deny"}
  {"type": "stopped"}                              - run cancelled via cancel event

Usage:
    python agent.py "your task here"
"""

import json
import queue
import sys
import threading

import config
import llm
import session
import tools

SYSTEM_PROMPT = (
    "You are SandboxAgent, a friendly AI assistant with tools — think of "
    "yourself as a warm, capable friend who happens to be great with computers.\n\n"
    "PERSONALITY:\n"
    "- Warm, friendly, a little playful. Talk like a helpful friend, never like a manual.\n"
    "- Match the user's LATEST message language (ignore earlier turns): if the latest message "
    "is Hinglish or Hindi (Roman script), you MUST reply in Hinglish — even if earlier "
    "messages were in English. If the latest message is English, reply in English.\n"
    "- When writing Hinglish, use SIMPLE everyday words and keep every sentence short "
    "(under 15 words). Never invent fancy Hindi words — if you don't know the natural "
    "word, just use the English word instead. Do NOT start replies with ritual phrases "
    "like 'Samajh gaya! Ye kaam main kar dunga' — just do the work or answer directly.\n"
    "- Keep answers conversational and reasonably concise. No robotic filler phrases.\n"
    "- Be honest: never claim you did something you didn't do, never invent tool results. "
    "If a tool failed, say so plainly and try another way.\n"
    "- Never narrate your tool calls in prose (no 'I will now call list_dir...') — "
    "the UI already shows your activity step-by-step. Just do the work, then summarize.\n\n"
    "WORK STYLE:\n"
    "- Think step-by-step: gather information or take action with tools, read the results, "
    "repeat until the task is done, then reply with a short summary of what you did.\n"
    "- When the user asks you to DO something (run, check, find, search, create, look up), "
    "you MUST call tools — never just say you will do it and then reply without acting. "
    "Talk is not work: a tool call must actually happen.\n"
    "- NEVER ask 'Do you want me to...?' or 'Shall I...?' — if the user already asked, "
    "just do it immediately with tools. Asking permission instead of acting is a failure.\n"
    "- Trigger words: if the user's message has 'search', 'find online', 'internet se', "
    "'check karo', 'look up', 'latest' or 'current' → call web_search in your FIRST turn, "
    "no exceptions. NEVER invent an answer the user asked you to look up — making up "
    "a story/fact instead of searching is a failure.\n"
    "- Answer the user's CURRENT message. Do not drag in previous tasks unless they are "
    "directly relevant.\n"
    "- If a step fails, try a different approach once or twice before giving up.\n\n"
    "IMPORTANT — every final reply you write MUST end with exactly one of these, on its own line:\n"
    "- a short follow-up question, or\n"
    "- a helpful suggestion for what the user could try next.\n"
    "Keep it to one line and make it sound natural, like something a friend would ask. "
    "This comes AFTER you finish the task — never skip the actual work just to jump to "
    "the closing question."
)

# Tool-result payloads in events are truncated harder than the model's
# context-window cap: the UI only needs a preview.
EVENT_OUTPUT_CHARS = 2000


def _coerce_args(args):
    # Ollama's native API already gives a dict; be tolerant of a JSON string.
    if isinstance(args, str):
        try:
            return json.loads(args)
        except JSONDecodeError:
            return {}
    return args or {}


# Model names for which Ollama's streaming responses came back empty
# (no content AND no tool_calls) — a known quirk in some Ollama versions
# where stream:true drops tool calls. Once seen, we skip streaming for
# that model and use a single non-streaming call instead.
_stream_broken = set()


def _chat_with_tokens(messages, model):
    """Call llm.chat with on_token, yielding live token events; return message.

    llm.chat invokes on_token synchronously, so it runs in a daemon thread
    while this generator drains the chunk queue, yielding
    {"type": "token", "content": chunk} events. A sentinel marks the end;
    any exception raised by the worker is re-raised here after draining.

    Streaming fallback: some Ollama versions drop the whole response
    (no content AND no tool_calls) when stream:true is combined with
    tools. If that happens we retry ONCE with streaming off and remember
    the model in _stream_broken so later turns skip streaming entirely.
    """
    model_name = model or config.MODEL
    if model_name in _stream_broken:
        # Known-broken combo: one non-streaming call, no token events.
        return llm.chat(messages, tools.all_schemas(), model=model)

    chunk_q = queue.Queue()
    outcome = {}
    _done = object()

    def _on_token(chunk):
        chunk_q.put(chunk)

    def _worker():
        try:
            outcome["message"] = llm.chat(
                messages, tools.all_schemas(), model=model, on_token=_on_token
            )
        except Exception as exc:  # re-raised in the generator below
            outcome["error"] = exc
        finally:
            chunk_q.put(_done)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    while True:
        chunk = chunk_q.get()
        if chunk is _done:
            break
        yield {"type": "token", "content": chunk}
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    message = outcome["message"]
    if not message.get("content") and not message.get("tool_calls"):
        # Streaming dropped the response — retry once without streaming.
        _stream_broken.add(model_name)
        return llm.chat(messages, tools.all_schemas(), model=model)
    return message


def stream_agent(task, messages, model=None, run_id=None, cancel=None, approver=None):
    """Run the agent loop, yielding event dicts (see module docstring).

    Appends the user task to `messages` and mutates the list in place as the
    conversation grows (assistant turns, tool results). Raises nothing:
    Ollama failures surface as an {"type": "error"} event instead.

    Args:
        model: model name passed to llm.chat (default: config.MODEL).
        run_id: opaque run id echoed in approval events; informational only.
        cancel: threading.Event or None. When set, the loop yields
            {"type": "stopped"} and returns (checked each iteration and
            before every tool call).
        approver: callable (command, reason) -> "approve" | "deny", or None.
            When given, tools.web_mode is set so risky run_command calls
            raise ApprovalNeeded instead of prompting on stdin; the loop
            yields approval_required, blocks on approver, then yields
            approval_resolved and either re-runs the command approved or
            records a cancellation.

    New event types beyond the originals: "token" (live model output),
    "usage" (per-turn token counts), "approval_required",
    "approval_resolved", "stopped".
    """
    messages.append({"role": "user", "content": task})
    tools.web_mode.set(approver is not None)
    try:
        for _ in range(config.MAX_ITERS):
            if cancel is not None and cancel.is_set():
                yield {"type": "stopped"}
                return
            yield {"type": "status", "message": "Thinking..."}
            try:
                message = yield from _chat_with_tokens(messages, model)
            except RuntimeError as exc:
                # e.g. Ollama unreachable — report, don't raise out of the generator
                yield {"type": "error", "message": str(exc)}
                return
            # Token usage never goes back to Ollama: pop before history append.
            usage = message.pop("_usage", None)
            if usage:
                prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                completion_tokens = int(usage.get("completion_tokens", 0) or 0)
                yield {
                    "type": "usage",
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                }
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                final = message.get("content", "") or "(no answer returned)"
                yield {"type": "answer", "content": final}
                return
            messages.append(message)  # keep the assistant turn incl. tool_calls
            for tc in tool_calls:
                if cancel is not None and cancel.is_set():
                    yield {"type": "stopped"}
                    return
                func = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = func.get("name", "")
                args = _coerce_args(func.get("arguments"))
                yield {"type": "status", "message": f"Calling {name}..."}
                yield {"type": "tool_call", "name": name, "args": args}
                try:
                    result = tools.call_tool(name, args)
                except tools.ApprovalNeeded as need:
                    if approver is None:
                        result = "ERROR: command cancelled by user."
                    else:
                        yield {
                            "type": "approval_required",
                            "run_id": run_id,
                            "command": need.command,
                            "reason": need.reason,
                        }
                        decision = approver(need.command, need.reason)
                        yield {"type": "approval_resolved", "decision": decision}
                        if decision == "approve":
                            result = tools.TOOLS["run_command"](**args, _approved=True)
                        else:
                            result = "ERROR: command cancelled by user."
                except Exception as exc:  # last-resort guard; tools shouldn't raise
                    result = f"ERROR: tool '{name}' crashed: {exc}"
                session.log_turn(name, args, result)  # Phase 3: never raises
                messages.append({"role": "tool", "content": str(result)})
                yield {
                    "type": "tool_result",
                    "name": name,
                    "output": str(result)[:EVENT_OUTPUT_CHARS],
                }
    finally:
        tools.web_mode.set(False)
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
