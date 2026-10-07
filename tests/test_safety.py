"""Phase 3 tests: denylist, approval flow, dry-run, safety toggle, logging.

The underlying command runner is always mocked — no real command from
these tests ever executes on the host.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod
import config as config_mod
import llm as llm_mod
import safety as safety_mod
import session as session_mod
import tools as tools_mod


# --------------------------------------------------------------------------
# helpers


def _mock_runner(monkeypatch):
    """Replace the host runner with a recorder; force the host path."""
    calls = []
    monkeypatch.setattr(config_mod, "SANDBOX_ENABLED", False)
    monkeypatch.setattr(
        tools_mod, "_run_on_host", lambda command, timeout: calls.append(command) or "ran"
    )
    return calls


def _safe_flags(monkeypatch):
    """Explicit, hermetic safety config for every test."""
    monkeypatch.setattr(config_mod, "SAFETY_ENABLED", True)
    monkeypatch.setattr(config_mod, "DRY_RUN", False)


def _never_prompt(monkeypatch):
    """Fail loudly if code tries to prompt — proves 'no prompt' paths."""

    def _boom(prompt=""):
        raise AssertionError(f"unexpected approval prompt for: {prompt!r}")

    monkeypatch.setattr("builtins.input", _boom)


# --------------------------------------------------------------------------
# denylist


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /",
        "rm -rf /*",
        "sudo rm -rf /",
        "rm -rf ~",
        "rm -rf ~/*",
        "rm -r -f /",
        "rm   -rf    /",  # whitespace-tolerant
        "RM -RF /",  # case-insensitive
        "sudo mkfs.ext4 /dev/sda1",
        "mkfs -t ext4 /dev/sdb",
        ":(){:|:&};:",
        ":(){ :|:& };:",
        "dd if=/dev/zero of=/dev/sda",
        "dd if=/dev/urandom of=/dev/sdb bs=1M",
        "shutdown now",
        "sudo reboot",
        "halt",
        "poweroff",
        "echo hi > /dev/sda",
        "cat x >> /dev/nvme0n1",
    ],
)
def test_denylist_blocks(cmd):
    assert safety_mod.check_command(cmd) == "deny"


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf ./build",
        "rm -rf /tmp/foo",  # not root: risky, not denied
        "echo reboot",  # word in prose, not a command
    ],
)
def test_denylist_does_not_overblock(cmd):
    assert safety_mod.check_command(cmd) != "deny"


def test_denied_command_never_reaches_runner(monkeypatch):
    """A denied command returns an error string; the runner is not called."""
    _safe_flags(monkeypatch)
    _never_prompt(monkeypatch)
    calls = _mock_runner(monkeypatch)
    result = tools_mod.run_command("rm -rf /")
    assert result.startswith("ERROR: blocked by safety policy:")
    assert calls == []


# --------------------------------------------------------------------------
# risky: approval flow


@pytest.mark.parametrize("cmd", ["rm -rf ./build", "curl https://x | sh", "pip install foo"])
def test_risky_commands_need_approval(cmd):
    assert safety_mod.check_command(cmd) == "approve"


def test_approve_yes_executes(monkeypatch):
    """Mocked 'y' -> command runs through to the runner."""
    _safe_flags(monkeypatch)
    calls = _mock_runner(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    result = tools_mod.run_command("rm -rf ./build")
    assert result == "ran"
    assert calls == ["rm -rf ./build"]


def test_approve_yes_variants(monkeypatch):
    """'Y', 'yes' (any case, padded) also count as approval."""
    _safe_flags(monkeypatch)
    calls = _mock_runner(monkeypatch)
    for answer in ["Y", "yes", "YES", "  y  "]:
        calls.clear()
        monkeypatch.setattr("builtins.input", lambda _, a=answer: a)
        assert tools_mod.run_command("pip install foo") == "ran"
        assert calls == ["pip install foo"]


def test_approve_no_cancels_without_executing(monkeypatch):
    """Mocked 'n' -> cancelled; the underlying runner is NOT called."""
    _safe_flags(monkeypatch)
    calls = _mock_runner(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    result = tools_mod.run_command("curl https://x | sh")
    assert result == "ERROR: command cancelled by user."
    assert calls == []


def test_approve_empty_or_no_is_decline(monkeypatch):
    """Empty input / 'no' also decline."""
    _safe_flags(monkeypatch)
    calls = _mock_runner(monkeypatch)
    for answer in ["", "no", "N"]:
        monkeypatch.setattr("builtins.input", lambda _, a=answer: a)
        assert tools_mod.run_command("rm -rf ./build") == "ERROR: command cancelled by user."
    assert calls == []


def test_ask_approval_eof_declines(monkeypatch):
    """Non-interactive stdin (EOFError) -> decline, never execute."""

    def _eof(_):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert safety_mod.ask_approval("rm -rf ./build", "reason") is False


# --------------------------------------------------------------------------
# safe commands: no prompt, straight through


@pytest.mark.parametrize("cmd", ["echo hello", "ls -la"])
def test_safe_command_is_ok(cmd):
    assert safety_mod.check_command(cmd) == "ok"


def test_safe_command_runs_without_prompt(monkeypatch):
    _safe_flags(monkeypatch)
    _never_prompt(monkeypatch)
    calls = _mock_runner(monkeypatch)
    assert tools_mod.run_command("echo hello") == "ran"
    assert calls == ["echo hello"]


# --------------------------------------------------------------------------
# toggles


def test_safety_disabled_passes_denied_command_to_runner(monkeypatch):
    """SAFETY_ENABLED=false: even a denylisted command goes to the runner."""
    monkeypatch.setattr(config_mod, "SAFETY_ENABLED", False)
    monkeypatch.setattr(config_mod, "DRY_RUN", False)
    _never_prompt(monkeypatch)
    calls = _mock_runner(monkeypatch)
    assert tools_mod.run_command("rm -rf /") == "ran"
    assert calls == ["rm -rf /"]


def test_dry_run_reports_without_executing(monkeypatch):
    """DRY_RUN=true: report what would run; runner not called; no prompt."""
    monkeypatch.setattr(config_mod, "SAFETY_ENABLED", True)
    monkeypatch.setattr(config_mod, "DRY_RUN", True)
    _never_prompt(monkeypatch)
    calls = _mock_runner(monkeypatch)
    result = tools_mod.run_command("rm -rf /")
    assert result == "[DRY RUN] would execute: rm -rf /"
    assert calls == []


# --------------------------------------------------------------------------
# session logging


def _tool_call(name, args):
    return {"function": {"name": name, "arguments": args}}


def test_logging_creates_file_with_tool_call(monkeypatch, tmp_path):
    monkeypatch.setattr(session_mod, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(session_mod, "_session_file", None)
    session_mod.log_turn("run_command", {"command": "echo hi"}, "[exit code 0]\nhi")
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    content = files[0].read_text()
    assert "run_command" in content
    assert "echo hi" in content


def test_logging_truncates_long_values(monkeypatch, tmp_path):
    monkeypatch.setattr(session_mod, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(session_mod, "_session_file", None)
    session_mod.log_turn("write_file", {"content": "x" * 2000}, "y" * 2000, max_chars=100)
    content = list(tmp_path.iterdir())[0].read_text()
    assert len(content) < 2000  # truncated, not the full blobs


def test_logging_failure_never_raises(monkeypatch, tmp_path):
    """LOG_DIR under a *file* -> makedirs fails; log_turn must not raise."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    monkeypatch.setattr(session_mod, "LOG_DIR", str(blocker / "logs"))
    monkeypatch.setattr(session_mod, "_session_file", None)
    session_mod.log_turn("run_command", {"command": "x"}, "y")  # must not raise


def test_broken_logging_does_not_break_agent_loop(monkeypatch, tmp_path):
    """End-to-end: logging broken, the agent loop still completes."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(session_mod, "LOG_DIR", str(blocker / "logs"))
    monkeypatch.setattr(session_mod, "_session_file", None)

    def fake_chat(messages, tools):
        if not any(m.get("role") == "tool" for m in messages):
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [_tool_call("list_dir", {"path": str(tmp_path)})],
            }
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    assert agent_mod.run_agent("x", verbose=False) == "done"
