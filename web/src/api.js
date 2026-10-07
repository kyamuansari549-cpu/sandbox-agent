/* Backend API client for the SandboxAgent web app.
 *
 * Contract:
 *   GET  {BASE}/api/health -> { status, model }
 *   POST {BASE}/api/chat  { message, session_id } -> SSE stream (text/event-stream)
 *     events: session | status | tool_call | tool_result | answer | error | done
 *     each event is `data: <json>\n\n`
 *   GET  {BASE}/api/sessions -> [{ session_id, created_at, turns, preview }] (newest first)
 *   GET  {BASE}/api/sessions/{id} -> { session_id, created_at, display: [
 *     { role: "user"|"assistant", content, tools: [{ name, args, output }], ts } ] }
 *
 * SSE is read with fetch() + a ReadableStream reader (POST bodies can't use
 * EventSource). Partial chunks are buffered and split on "\n\n".
 */

const BASE = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/+$/, "");

export function apiBase() {
  return BASE;
}

export async function checkHealth() {
  const res = await fetch(`${BASE}/api/health`, { signal: AbortSignal.timeout(8000) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function listSessions() {
  const res = await fetch(`${BASE}/api/sessions`, { signal: AbortSignal.timeout(8000) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  return Array.isArray(data) ? data : [];
}

export async function getSession(id) {
  const res = await fetch(`${BASE}/api/sessions/${encodeURIComponent(id)}`, {
    signal: AbortSignal.timeout(10000),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function streamChat({ message, sessionId, onEvent, signal }) {
  const res = await fetch(`${BASE}/api/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({ message, session_id: sessionId }),
    signal,
  });

  if (!res.ok) {
    let detail = "";
    try {
      detail = await res.text();
    } catch {
      /* ignore */
    }
    throw new Error(detail || `Request failed (HTTP ${res.status})`);
  }
  if (!res.body) {
    throw new Error("Streaming is not supported by this browser.");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const dispatch = (rawChunk) => {
    for (const line of rawChunk.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data:")) continue;
      const payload = trimmed.slice(5).trim();
      if (!payload) continue;
      try {
        onEvent(JSON.parse(payload));
      } catch {
        /* ignore malformed event payloads */
      }
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      dispatch(chunk);
    }
  }
  if (buffer.trim()) dispatch(buffer);
}
