"""Phase 2 tests. Docker is NEVER touched — subprocess.run is mocked."""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_mod
import sandbox as sandbox_mod
import tools as tools_mod


def _completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _fake_docker(captured, info_rc=0, images_stdout="sha256:abc123\n",
                 run_stdout="hello\n", run_stderr="", run_rc=0,
                 run_side_effect=None):
    """Fake subprocess.run dispatching on the docker subcommand."""
    def fake_run(argv, **kwargs):
        captured.append(argv)
        if argv[:2] == ["docker", "info"]:
            return _completed(argv, info_rc)
        if argv[:2] == ["docker", "images"]:
            return _completed(argv, 0, stdout=images_stdout)
        if argv[:2] == ["docker", "pull"]:
            return _completed(argv, 0, stdout="pulled\n")
        if argv[:2] == ["docker", "run"]:
            if run_side_effect is not None:
                raise run_side_effect
            return _completed(argv, run_rc, stdout=run_stdout, stderr=run_stderr)
        raise AssertionError(f"unexpected docker call: {argv}")
    return fake_run


def test_run_in_sandbox_builds_correct_docker_command(monkeypatch, tmp_path):
    """The docker argv must isolate the container and mount only workdir."""
    captured = []
    monkeypatch.setattr(subprocess, "run",
                        _fake_docker(captured, run_stdout="out\n"))
    monkeypatch.setattr(config_mod, "SANDBOX_WORKDIR", str(tmp_path))

    out, returncode = sandbox_mod.run_in_sandbox("echo hi", timeout=10)

    assert ("out" in out) and returncode == 0
    run_argv = [a for a in captured if a[:2] == ["docker", "run"]][0]
    for token in ("docker", "run", "--rm", "--network", "none",
                  config_mod.SANDBOX_IMAGE, "sh", "-c", "echo hi"):
        assert token in run_argv, f"missing {token!r} in {run_argv}"
    vol_idx = run_argv.index("-v")
    assert run_argv[vol_idx + 1].endswith(":/work")
    assert str(tmp_path) in run_argv[vol_idx + 1]
    w_idx = run_argv.index("-w")
    assert run_argv[w_idx + 1] == "/work"


def test_timeout_kills_container_command(monkeypatch, tmp_path):
    """TimeoutExpired from the docker call becomes a clean error string."""
    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "info"]:
            return _completed(argv, 0)
        if argv[:2] == ["docker", "images"]:
            return _completed(argv, 0, stdout="img\n")
        raise subprocess.TimeoutExpired(argv, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(config_mod, "SANDBOX_WORKDIR", str(tmp_path))

    out, returncode = sandbox_mod.run_in_sandbox("sleep 99", timeout=1)

    assert "timed out" in out
    assert "1s" in out
    assert returncode != 0


def test_output_truncation(monkeypatch, tmp_path):
    """Oversized sandbox output is truncated with a marker."""
    captured = []
    monkeypatch.setattr(subprocess, "run",
                        _fake_docker(captured, run_stdout="A" * 500))
    monkeypatch.setattr(config_mod, "SANDBOX_WORKDIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "MAX_OUTPUT_CHARS", 100)

    out, _ = sandbox_mod.run_in_sandbox("yes", timeout=10)

    assert "truncated" in out
    assert len(out) < 500


def test_docker_missing_raises_clear_error(monkeypatch):
    """No docker binary -> RuntimeError with an actionable message."""
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Docker is not available"):
        sandbox_mod.run_in_sandbox("echo hi")
    with pytest.raises(RuntimeError, match="Docker Desktop"):
        sandbox_mod.ensure_image()


def test_docker_daemon_down_raises_clear_error(monkeypatch):
    """Binary present but daemon unreachable -> same clear RuntimeError."""
    captured = []
    monkeypatch.setattr(subprocess, "run", _fake_docker(captured, info_rc=1))

    with pytest.raises(RuntimeError, match="Docker is not available"):
        sandbox_mod.run_in_sandbox("echo hi")


def test_ensure_image_pulls_only_when_missing(monkeypatch):
    """docker images -q decides: empty -> pull, id present -> no pull."""
    # image missing -> pull happens
    captured = []
    monkeypatch.setattr(subprocess, "run",
                        _fake_docker(captured, info_rc=0, images_stdout=""))
    sandbox_mod.ensure_image()
    assert any(a[:2] == ["docker", "pull"] for a in captured)

    # image present -> no pull
    captured.clear()
    monkeypatch.setattr(subprocess, "run",
                        _fake_docker(captured, info_rc=0,
                                     images_stdout="sha256:abc123\n"))
    sandbox_mod.ensure_image()
    assert not any(a[:2] == ["docker", "pull"] for a in captured)


def test_fallback_to_host_with_warning(monkeypatch):
    """Sandbox unavailable -> host execution WITH the warning prefix."""
    def boom(command, timeout=None):
        raise sandbox_mod.DockerUnavailableError("no docker")

    monkeypatch.setattr(sandbox_mod, "run_in_sandbox", boom)
    monkeypatch.setattr(config_mod, "SANDBOX_ENABLED", True)

    result = tools_mod.run_command("echo hi")

    assert result.startswith("[WARNING: Docker unavailable")
    assert "not sandboxed" in result
    assert "hi" in result
    assert "exit code 0" in result


def test_sandbox_disabled_forces_host_without_warning(monkeypatch):
    """SANDBOX_ENABLED=false -> host path, no warning, sandbox untouched."""
    def boom(command, timeout=None):
        raise AssertionError("sandbox must not be called when disabled")

    monkeypatch.setattr(sandbox_mod, "run_in_sandbox", boom)
    monkeypatch.setattr(config_mod, "SANDBOX_ENABLED", False)

    result = tools_mod.run_command("echo hi")

    assert "[WARNING" not in result
    assert "hi" in result
    assert "exit code 0" in result
