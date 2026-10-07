"""Phase 3: safety layer for run_command.

Three tiers, checked in this order before ANY execution:
  deny    -> never executes (destructive, no legitimate agent use)
  approve -> asks the human on stdin ([y/N]); anything but explicit
             y/yes (or EOF/non-interactive) means never execute
  ok      -> proceed as before

Honest note: this is a speed bump, not bulletproof isolation. Pattern
matching cannot catch every obfuscation ($VAR tricks, base64 payloads,
unicode lookalikes, ...). Real isolation is the Docker sandbox (sandbox.py).
"""

import re

# (pattern, human-readable reason). Matched against whitespace-collapsed
# commands, case-insensitively. Kept focused: catastrophic, irreversible,
# and never something the agent legitimately needs.
_DENY = [
    (
        r"\brm\s+(?=[^;&|]*-[a-zA-Z]*r)(?=[^;&|]*-[a-zA-Z]*f)"
        r"[^;&|]*\s(?:/|/\*|~|~/\*)(?:\s|$|[;&|])",
        "recursive forced delete of filesystem root (/) or home (~)",
    ),
    (r"\bmkfs(\.[a-z0-9]+)?\b", "filesystem formatting (mkfs)"),
    (r"\bdd\b[^;&|]*\bif=/dev/", "raw disk copy reading from a device (dd)"),
    (r"\bdd\b[^;&|]*\bof=/dev/", "raw disk write to a device (dd)"),
    (
        r":\(\s*\)\s*\{\s*:\s*\|\s*:&\s*\}\s*;\s*:",
        "fork bomb",
    ),
    # Power commands only in command position (start, after ; & |, after
    # sudo) so `echo reboot` (just printing the word) is not flagged.
    (r"(^|[;&|]\s*|sudo\s+)shutdown\b", "system shutdown"),
    (r"(^|[;&|]\s*|sudo\s+)reboot\b", "system reboot"),
    (r"(^|[;&|]\s*|sudo\s+)halt\b", "system halt"),
    (r"(^|[;&|]\s*|sudo\s+)poweroff\b", "system poweroff"),
    (
        r">{1,2}\s*/dev/(sd[a-z]+|hd[a-z]+|nvme\d+n\d+|vd[a-z]+"
        r"|mmcblk\d+|loop\d+)",
        "direct write to a block device",
    ),
]

# Needs an explicit human yes. Destructive-or-wide-reaching, but with
# legitimate uses (installing a package, cleaning a build dir, ...).
_RISKY = [
    (
        r"\brm\b(?=[^;&|]*-[a-zA-Z]*r)(?=[^;&|]*-[a-zA-Z]*f)",
        "recursive forced delete (rm -rf)",
    ),
    (r"\bmv\b[^;&|]*\s/\*", "moving the filesystem root's contents (mv /*)"),
    (r"\bchmod\b[^;&|]*-[a-zA-Z]*R", "recursive permission change (chmod -R)"),
    (r"\bchown\b[^;&|]*-[a-zA-Z]*R", "recursive ownership change (chown -R)"),
    (
        r"\b(curl|wget)\b[^|]*\|\s*(sh|bash|zsh|dash)\b",
        "piping a download straight into a shell (curl/wget | sh)",
    ),
    (r"\bpip3?\s+install\b", "installing Python packages (pip install)"),
    (
        r"\bapt(-get)?\s+install\b",
        "installing system packages (apt install)",
    ),
    (
        r"(^|[;&|]\s*|sudo\s+)docker\b",
        "docker command (can escape the sandbox: --privileged, -v /:/host)",
    ),
    (r"(^|[;&|]\s*)sudo\b", "privilege escalation (sudo)"),
]


def _normalize(command):
    """Collapse whitespace so `rm   -rf    /` still matches."""
    return re.sub(r"\s+", " ", command.strip())


def classify(command):
    """Return (verdict, reason). Verdict is "deny" | "approve" | "ok".

    Denylist is checked first: a command matching both lists is denied.
    """
    text = _normalize(command or "")
    for pattern, reason in _DENY:
        if re.search(pattern, text, re.IGNORECASE):
            return "deny", reason
    for pattern, reason in _RISKY:
        if re.search(pattern, text, re.IGNORECASE):
            return "approve", reason
    return "ok", ""


def check_command(command):
    """Return just the verdict: "deny" | "approve" | "ok"."""
    verdict, _ = classify(command)
    return verdict


def ask_approval(command, reason):
    """Prompt the human. True only on explicit y/yes.

    EOFError (non-interactive stdin) means decline — never execute.
    """
    print("\n[Safety] This command needs your approval before it runs.")
    print(f"  command: {command}")
    print(f"  reason:  {reason}")
    try:
        answer = input("Allow it to run? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")
