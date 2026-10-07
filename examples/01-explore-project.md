# Example 1 — Explore a project

Copy-paste this as your task:

```
List the files in this project and tell me what each one does.
Then read README.md and give me a 3-line summary of what this project is for.
```

**What this shows off:** local file tools (`list_dir`, `read_file`) plus
reasoning — the agent gathers facts first, then synthesizes an answer
instead of guessing.

**What to expect:** a few `[tool]` lines as it lists the directory and
reads the README, then a short summary in plain text.
