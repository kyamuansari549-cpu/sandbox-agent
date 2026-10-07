# SandboxAgent — 30-second demo GIF script

Record with **ScreenToGif** on Windows. Before you start:

- Backend running: `uvicorn server:app --host 127.0.0.1 --port 8000`
- Ollama running with a tool-capable model: `ollama pull qwen2.5:7b`
- Browser window resized to **1280x800** (ScreenToGif: Recorder > Board >
  drag the record area to exactly 1280x800, or maximize and crop later)
- Have `work/` contain a few sample files (e.g. run `python agent.py
  "create work/notes.txt with a short project todo"` first)
- Recording settings: 15 FPS, and delete/retype mistakes — keep it clean.

Export when done: **GIF, under 5 MB** (ScreenToGif: File > Save As > GIF;
lower the FPS or trim the last seconds if the file is too big).

---

## Shot-by-shot script (~30 seconds)

### 0:00–0:05 — Open the dashboard

- Address bar: type `http://localhost:8000` and press Enter.
- Let the dashboard load: chat input visible, sidebar with past sessions.
- **Goal:** viewer sees a clean chat UI, nothing else.

### 0:05–0:12 — First task: list files (streaming demo)

- Click the message box and type verbatim: `list files in the work folder`
- Press Enter.
- **Watch and show:** the activity timeline streams in — "Thinking...",
  `list_dir` tool call expands, and the final answer's tokens stream in.
- **Goal:** viewer sees the live tool-call timeline.

### 0:12–0:18 — Approval popup

- Type verbatim: `delete the file work/old_draft.txt`
  (create that file first if needed: `echo draft > work/old_draft.txt`)
- An **approval popup** appears for the risky command — pause 1 second so
  the popup is clearly visible, then click **Approve**.
- **Goal:** viewer sees the safety gate: risky commands ask first.

### 0:18–0:24 — Model picker + Stop button

- Click the **model picker** dropdown (top of the chat header), pick a
  different installed model (e.g. `llama3.1`), then switch back to
  `qwen2.5:7b`.
- Type verbatim: `write a long essay about operating systems, at least 500
  words`
- As soon as streaming starts, click **Stop**. The stream halts.
- **Goal:** viewer sees model switching and the working Stop button.

### 0:24–0:30 — Scheduled tasks + download

- Open the **Scheduled tasks** view (sidebar / header icon).
- It shows a past scheduled run with a created file.
- Click the **download button** on that file — the browser's save prompt
  or download completes.
- **Goal:** viewer sees scheduling exists and files are downloadable.

---

## Checklist before exporting

- Total runtime ~30 seconds (trim pauses, keep the pacing snappy).
- No password, no personal info, no other tabs visible.
- Export as GIF, **under 5 MB**. If oversize: drop to 10 FPS, trim the
  0:00–0:05 loading pause, or shrink the record area slightly.
