/* Backend API client for the SandboxAgent web app.
 *
 * Contract:
 *   GET  {BASE}/api/health -> { status, model }
 *   POST {BASE}/api/chat  { message, session_id, model? } -> SSE stream (text/event-stream)
 *     events (in order):
 *       run            { type:"run", run_id }                        ALWAYS first
 *       session        { type:"session", session_id }                 only for brand-new sessions
 *       status         { type:"status", message }
 *       tool_call      { type:"tool_call", name, args }
 *       tool_result    { type:"tool_result", name, output }
 *       token          { type:"token", content }                      APPEND to the in-progress answer
 *       usage          { type:"usage", prompt_tokens, completion_tokens }  one per LLM call
 *       approval_required { type:"approval_required", run_id, command, reason }  loop paused
 *       approval_resolved { type:"approval_resolved", decision }
 *       answer         { type:"answer", content }                      SET (overwrite) the answer
 *       error          { type:"error", message }
 *       stopped        { type:"stopped" }                              user cancelled
 *       done           { type:"done", run_id, usage, stopped, model }  ALWAYS last
 *     each event is `data: <json>\n\n`
 *   POST {BASE}/api/runs/{run_id}/stop -> { ok:true } (404 unknown)
 *   POST {BASE}/api/runs/{run_id}/approve { decision:"approve"|"deny" } -> { ok:true } (404/409)
 *   GET  {BASE}/api/download?path=<relative path> -> file download (403 traversal, 404 missing)
 *   GET  {BASE}/api/models -> { models:[...], default:"qwen2.5:7b" } (models may be [] when Ollama is down)
 *   GET  {BASE}/api/sessions -> [{ session_id, created_at, turns, preview }] (newest first)
 *   GET  {BASE}/api/sessions/{id} -> { session_id, created_at, display: [
 *     { role:"user"|"assistant", content, tools:[{ name, args, output }], usage?, model?, ts } ] }
 *   GET  {BASE}/api/tasks -> { tasks:[{ id, name, prompt, interval_seconds, model, enabled,
 *     created_at, last_run_at, last_status }] } (newest first)
 *   POST {BASE}/api/tasks { name, prompt, interval_seconds>=60, model? } -> 201 { task } (400 invalid)
 *   DELETE {BASE}/api/tasks/{id} -> { ok:true } (404)
 *   GET  {BASE}/api/tasks/{id}/runs -> { runs:[{ run_id, started_at, status, summary }] } (newest first)
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

export async function streamChat({ message, sessionId, model, onEvent, signal }) {
  const res = await fetch(`${BASE}/api/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({ message, session_id: sessionId, model }),
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

/** Ask the backend to stop a running agent loop. The stream then emits
 *  `stopped` followed by `done` with stopped:true. */
export async function stopRun(runId) {
  const res = await fetch(`${BASE}/api/runs/${encodeURIComponent(runId)}/stop`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`Stop failed (HTTP ${res.status})`);
  return res.json();
}

/** Resolve a paused approval request: decision is "approve" or "deny". */
export async function approveRun(runId, decision) {
  const res = await fetch(
    `${BASE}/api/runs/${encodeURIComponent(runId)}/approve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision }),
    }
  );
  if (!res.ok) throw new Error(`Approval failed (HTTP ${res.status})`);
  return res.json();
}

/** Models available in Ollama. Returns { models, default }; models may be []
 *  when Ollama is down — the default is still usable. */
export async function fetchModels() {
  const res = await fetch(`${BASE}/api/models`, { signal: AbortSignal.timeout(8000) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  return {
    models: Array.isArray(data.models) ? data.models : [],
    default: data.default || "qwen2.5:7b",
  };
}

/** Download URL for a file the agent wrote (relative path inside the agent
 *  workspace). Used as an anchor href — the backend serves the file. */
export function downloadUrl(path) {
  return `${BASE}/api/download?path=${encodeURIComponent(path)}`;
}

/** Scheduled tasks, newest first. */
export async function listTasks() {
  const res = await fetch(`${BASE}/api/tasks`, { signal: AbortSignal.timeout(8000) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  return Array.isArray(data.tasks) ? data.tasks : [];
}

/** Create a scheduled task. interval_seconds must be >= 60. */
export async function createTask({ name, prompt, interval_seconds, model }) {
  const res = await fetch(`${BASE}/api/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, prompt, interval_seconds, model }),
  });
  if (!res.ok) {
    let detail = "";
    try {
      const data = await res.json();
      detail = data.detail || data.message || "";
    } catch {
      /* ignore */
    }
    throw new Error(detail || `Create failed (HTTP ${res.status})`);
  }
  return res.json();
}

/** Delete a scheduled task. */
export async function deleteTask(id) {
  const res = await fetch(`${BASE}/api/tasks/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`Delete failed (HTTP ${res.status})`);
  return res.json();
}

/** Past runs of one task, newest first. */
export async function getTaskRuns(id) {
  const res = await fetch(`${BASE}/api/tasks/${encodeURIComponent(id)}/runs`, {
    signal: AbortSignal.timeout(10000),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  return Array.isArray(data.runs) ? data.runs : [];
}
