function relTime(iso) {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return new Date(t).toLocaleDateString([], { month: "short", day: "numeric" });
}

export default function Sidebar({
  sessions,
  activeId,
  onSelect,
  onNewChat,
  view,
  onViewChange,
  models,
  modelsLoaded,
  selectedModel,
  defaultModel,
  onModelChange,
  connected,
}) {
  const options = models && models.length > 0 ? models : [defaultModel];
  const offline = modelsLoaded && (!models || models.length === 0);

  return (
    <aside className="sidebar">
      <div className="side-brand">
        <span className="brand-mark" aria-hidden="true">
          S
        </span>
        <span className="brand-name">SandboxAgent</span>
      </div>

      <div className="side-new">
        <button className="btn new-chat-btn" onClick={onNewChat}>
          <span className="plus" aria-hidden="true">
            +
          </span>
          New chat
        </button>
      </div>

      <div className="view-tabs" role="tablist" aria-label="Main view">
        <button
          className={`view-tab${view === "chat" ? " active" : ""}`}
          onClick={() => onViewChange("chat")}
        >
          Chat
        </button>
        <button
          className={`view-tab${view === "tasks" ? " active" : ""}`}
          onClick={() => onViewChange("tasks")}
        >
          Scheduled
        </button>
      </div>

      <div className="side-section">
        <div className="side-section-title">Chats</div>
        <div className="session-list">
          {sessions.length === 0 ? (
            <div className="session-empty">
              No chats yet. Start one above.
            </div>
          ) : (
            sessions.map((s) => (
              <button
                key={s.session_id}
                className={`session-item${
                  s.session_id === activeId ? " active" : ""
                }`}
                onClick={() => onSelect(s.session_id)}
                title={s.preview || "Chat"}
              >
                <span className="session-preview">
                  {s.preview || "New chat"}
                </span>
                <span className="session-meta">
                  {s.turns != null ? `${s.turns} turn${s.turns === 1 ? "" : "s"} · ` : ""}
                  {relTime(s.created_at)}
                </span>
              </button>
            ))
          )}
        </div>
      </div>

      <div className="side-footer">
        <div className="model-picker">
          <select
            className="model-select"
            value={selectedModel}
            onChange={(e) => onModelChange(e.target.value)}
            title="Ollama model for new agent runs"
            aria-label="Model"
          >
            {options.map((m) => (
              <option key={m} value={m}>
                {m === selectedModel ? `✓ ${m}` : m}
              </option>
            ))}
          </select>
          {offline && <span className="offline-hint">(Ollama offline)</span>}
        </div>
        <span className={`conn ${connected ? "ok" : "bad"}`}>
          <span className="dot" />
          {connected ? "Connected" : "Disconnected"}
        </span>
      </div>
    </aside>
  );
}
