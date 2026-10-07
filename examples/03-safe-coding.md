# Example 3 — Safe coding in the sandbox

Copy-paste this as your task:

```
Write a Python script in work/fib.py that prints the first 10 Fibonacci
numbers, then run it and show me the output.
```

**What this shows off:** the full loop — `write_file` to create the
script, then `run_command` to execute it **inside the Docker sandbox**
(only `work/` is visible to the container, no network).

**What to expect:** `[tool] write_file({...})`,
`[tool] run_command({"command": "python work/fib.py"})`, then the
numbers. If Docker isn't running you'll see
`[WARNING: Docker unavailable — ran on HOST, not sandboxed]` instead —
the task still completes, just without isolation.

**Try next:** ask it to do something risky, e.g.
`"delete the work directory"`, and watch the safety gate kick in —
`rm -rf` needs your explicit `[y/N]` approval first.
