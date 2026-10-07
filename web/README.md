# SandboxAgent Web UI — Dashboard

Product-style dashboard for the SandboxAgent: a left sidebar with chat
history, a live activity timeline per turn, and session statistics.

## Layout

- **Sidebar (264px):** brand row, prominent "+ New chat" button, "Chats"
  section listing past sessions (preview + relative time, active chat
  highlighted), footer with model badge + Connected indicator.
- **Main area:** slim stats strip for the active session (Tools used,
  Files created, Turns), the chat timeline below it, composer fixed at the
  bottom.
- Clicking a session loads its history read-only; typing a new message
  continues that session live. `session_id` persists in `localStorage`.

## Prerequisites

The SandboxAgent backend must be running (default `http://localhost:8000`).

## Setup

```bash
cd web
npm install
```

## Configuration

The backend base URL comes from the `VITE_API_URL` environment variable.
It defaults to `http://localhost:8000`.

```bash
# Backend on another host/port:
VITE_API_URL=http://192.168.1.10:8000 npm run dev
```

Note: Vite bakes `VITE_API_URL` in at **build** time, so set it before
`npm run build` for production deployments.

## Run

```bash
npm run dev      # dev server on http://localhost:5173
npm run build    # production build -> web/dist/
npm run preview  # serve the production build locally
```

## How it works

- `GET /api/health` on load → model badge + Connected indicator in the sidebar footer.
- `GET /api/sessions` on load and after each turn → sidebar chat list
  (preview, turn count, relative time). Fails gracefully to an empty list
  when the backend is down.
- `GET /api/sessions/{id}` when a chat is selected → renders its stored
  turns (user bubbles + assistant tool timelines) read-only.
- `POST /api/chat` with `{ message, session_id }` returns an SSE stream.
  Events are parsed from `fetch()` + `ReadableStream` (see `src/api.js`):
  `session`, `status`, `tool_call`, `tool_result`, `answer`, `error`, `done`.
- Each assistant turn renders a live activity timeline above the final answer:
  status rows with spinners, expandable tool cards (args JSON + output), then
  the reply text.
- `session_id` is stored in `localStorage` and reused across reloads.
  "New chat" clears the conversation and starts a fresh session.
