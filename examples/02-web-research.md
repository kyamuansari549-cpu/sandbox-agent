# Example 2 — Web research

Copy-paste this as your task:

```
What is the latest stable Python release, and what are the 3 most
important changes in it? Cite the source URL you used.
```

**What this shows off:** the web tools. The agent should `web_search`
for the answer, `fetch_url` the most relevant result (e.g. the official
Python downloads or "What's New" page), and summarize.

**What to expect:** `[tool] web_search({...})` first, then
`[tool] fetch_url({...})` on a promising URL, then a summary with the
source. No API keys needed — search runs on DuckDuckGo.
