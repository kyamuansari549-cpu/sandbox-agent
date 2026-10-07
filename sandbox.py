"""Phase 2: isolated command execution inside a Docker container.

run_command (tools.py) prefers this sandbox. If Docker is missing or its
daemon is unreachable, we raise DockerUnavailableError and the caller falls
back to host execution with a clear warning — the agent loop never crashes.

No docker SDK dependency: we shell out to the `docker` CLI via subprocess.
"""

import os
import subprocess

import config

DOCKER_MISSING_MSG = (
    "Docker is not available (binary missing or daemon unreachable). "
    "Install Docker Desktop (https://www.docker.com/products/docker-desktop) "
    "and make sure the Docker daemon is running."
)


class DockerUnavailableError(RuntimeError):
    """Raised when commands can't run in the sandbox (no Docker)."""


def _docker_available():
    """True only if the `docker` binary exists AND the daemon answers."""
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=10
        )
    except FileNotFoundError:
        return False  # no docker binary
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0  # daemon unreachable -> nonzero exit


def _require_docker():
    if not _docker_available():
        raise DockerUnavailableError(DOCKER_MISSING_MSG)


def _mount_path(host_path):
    """Host path as Docker's -v flag expects it.

    Docker Desktop for Windows parses drive-letter paths fine, but forward
    slashes are safer for the flag's colon splitting; normalize on Windows
    only (a backslash is legal in a Linux filename, so leave those alone).
    """
    if os.name == "nt":
        return host_path.replace("\\", "/")
    return host_path


def _truncate(out):
    if len(out) > config.MAX_OUTPUT_CHARS:
        out = (
            out[: config.MAX_OUTPUT_CHARS]
            + f"\n... [truncated: output exceeded {config.MAX_OUTPUT_CHARS} chars]"
        )
    return out


def ensure_image():
    """Pull the sandbox image if it isn't already present locally.

    Raises DockerUnavailableError if Docker can't be used.
    """
    _require_docker()
    proc = subprocess.run(
        ["docker", "images", "-q", config.SANDBOX_IMAGE],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ERROR: 'docker images' failed: {(proc.stderr or '').strip()}"
        )
    if proc.stdout.strip():
        return  # image already present; no pull needed
    pull = subprocess.run(
        ["docker", "pull", config.SANDBOX_IMAGE],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if pull.returncode != 0:
        detail = (pull.stderr or pull.stdout or "").strip()[:500]
        raise RuntimeError(
            f"ERROR: could not pull Docker image '{config.SANDBOX_IMAGE}': {detail}"
        )


def run_in_sandbox(command, timeout=None):
    """Run *command* inside the Docker sandbox.

    Returns (combined stdout+stderr truncated to config.MAX_OUTPUT_CHARS,
    returncode). Raises DockerUnavailableError if Docker can't be used.
    """
    timeout = config.TIMEOUT if timeout is None else timeout
    ensure_image()  # raises DockerUnavailableError when Docker is unusable

    workdir = os.path.abspath(config.SANDBOX_WORKDIR)
    os.makedirs(workdir, exist_ok=True)

    argv = [
        "docker",
        "run",
        "--rm",  # container is removed when the command finishes
        "--network",
        "none",  # Phase 2: no internet inside the sandbox
        "-v",
        f"{_mount_path(workdir)}:/work",  # ONLY this host dir is visible
        "-w",
        "/work",
        config.SANDBOX_IMAGE,
        "sh",
        "-c",
        command,
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s and was killed.", 124
    out = _truncate((proc.stdout or "") + (proc.stderr or ""))
    return out, proc.returncode
