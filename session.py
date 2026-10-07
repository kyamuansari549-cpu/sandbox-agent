"""Phase 3: append-only session logging.

Every agent turn is appended to logs/session-<timestamp>.log under the
project directory: time, tool name, args (truncated), result (truncated).

Logging must NEVER crash the agent loop — every failure is swallowed.
"""

import datetime
import os

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_session_file = None


def _log_path():
    global _session_file
    if _session_file is None:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        _session_file = os.path.join(LOG_DIR, f"session-{stamp}.log")
    return _session_file


def log_turn(tool_name, args, result, max_chars=500):
    """Append one tool-call turn. Never raises."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(
                f"[{ts}] {tool_name} "
                f"args={str(args)[:max_chars]!r} -> "
                f"{str(result)[:max_chars]!r}\n"
            )
    except Exception:
        pass  # logging must never break the loop
