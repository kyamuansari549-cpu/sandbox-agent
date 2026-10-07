import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Sidebar from "./components/Sidebar.jsx";
import StatsStrip from "./components/StatsStrip.jsx";
import EmptyState from "./components/EmptyState.jsx";
import Composer from "./components/Composer.jsx";
import Timeline from "./components/Timeline.jsx";
import {
  checkHealth,
  streamChat,
  listSessions,
  getSession,
} from "./api.js";

const SESSION_KEY = "sandbox-agent-session";

function newSessionId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function loadSessionId() {
  try {
    const saved = localStorage.getItem(SESSION_KEY);
    if (saved) return saved;
  } catch {
    /* storage unavailable */
  }
  const id = newSessionId();
  try {
    localStorage.setItem(SESSION_KEY, id);
  } catch {
    /* storage unavailable */
  }
  return id;
}

function persistSessionId(id) {
  try {
    localStorage.setItem(SESSION_KEY, id);
  } catch {
    /* storage unavailable */
  }
}

let uidCounter = 0;
function uid() {
  uidCounter += 1;
  return `${Date.now()}-${uidCounter}`;
}

function fmtTime(ts) {
  return new Date(ts).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Convert backend history `display` turns into the live message shape so the
 *  same Timeline/ToolCard components render read-only history. */
function historyToMessages(display) {
  return (display || []).map((t) => {
    const ts = t.ts ? Date.parse(t.ts) : Date.now();
    if (t.role === "user") {
      return {
        id: uid(),
        role: "user",
        text: t.content || "",
        ts: Number.isNaN(ts) ? Date.now() : ts,
      };
    }
    return {
      id: uid(),
      role: "assistant",
      steps: (t.tools || []).map((tool) => ({
        id: uid(),
        kind: "tool",
        name: tool.name,
        args: tool.args || {},
        result: tool.output ?? "",
      })),
      answer: t.content || "",
      error: null,
      streaming: false,
      ts: Number.isNaN(ts) ? Date.now() : ts,
    };
  });
}

export default function App() {
  const [messages, setMessages] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [activeId, setActiveId] = useState(loadSessionId);
  const [health, setHealth] = useState({ state: "checking", model: null });
  const [streaming, setStreaming] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const abortRef = useRef(null);
  const chatRef = useRef(null);

  const refreshSessions = useCallback(() => {
    listSessions()
      .then(setSessions)
      .catch(() => {
        /* backend down — sessions list stays empty, chat still works */
      });
  }, []);

  // Backend health + session list on mount.
  useEffect(() => {
    let cancelled = false;
    checkHealth()
      .then((h) => {
        if (!cancelled) setHealth({ state: "ok", model: h.model || null });
      })
      .catch(() => {
        if (!cancelled) setHealth({ state: "fail", model: null });
      });
    refreshSessions();
    return () => {
      cancelled = true;
    };
  }, [refreshSessions]);

  const loadSession = useCallback(
    (id) => {
      if (abortRef.current) abortRef.current.abort();
      setStreaming(false);
      setActiveId(id);
      persistSessionId(id);
      setLoadingHistory(true);
      getSession(id)
        .then((data) => {
          setMessages(historyToMessages(data.display));
        })
        .catch(() => {
          // Unknown/expired session or backend down — start blank.
          setMessages([]);
        })
        .finally(() => setLoadingHistory(false));
    },
    []
  );

  // Load the persisted session's history on mount.
  useEffect(() => {
    loadSession(activeId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-scroll to bottom as new events arrive.
  useEffect(() => {
    const el = chatRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const sendMessage = useCallback(
    (text) => {
      if (streaming) return;
      const clean = text.trim();
      if (!clean) return;

      const userMsg = { id: uid(), role: "user", text: clean, ts: Date.now() };
      const asstMsg = {
        id: uid(),
        role: "assistant",
        steps: [],
        answer: null,
        error: null,
        streaming: true,
        ts: Date.now(),
      };
      setMessages((prev) => [...prev, userMsg, asstMsg]);
      setStreaming(true);

      const controller = new AbortController();
      abortRef.current = controller;
      const asstId = asstMsg.id;

      const applyEvent = (ev) => {
        if (ev.type === "session" && ev.session_id) {
          setActiveId(ev.session_id);
          persistSessionId(ev.session_id);
        }
        setMessages((prev) =>
          prev.map((m) => {
            if (m.id !== asstId) return m;
            switch (ev.type) {
              case "status":
                return {
                  ...m,
                  steps: [
                    ...m.steps,
                    { id: uid(), kind: "status", text: ev.message || "" },
                  ],
                };
              case "tool_call":
                return {
                  ...m,
                  steps: [
                    ...m.steps,
                    {
                      id: uid(),
                      kind: "tool",
                      name: ev.name,
                      args: ev.args || {},
                      result: null,
                    },
                  ],
                };
              case "tool_result": {
                const steps = m.steps.slice();
                for (let i = steps.length - 1; i >= 0; i--) {
                  const s = steps[i];
                  if (
                    s.kind === "tool" &&
                    s.name === ev.name &&
                    s.result == null
                  ) {
                    steps[i] = { ...s, result: ev.output ?? "" };
                    break;
                  }
                }
                return { ...m, steps };
              }
              case "answer":
                return { ...m, answer: (m.answer || "") + (ev.content ?? "") };
              case "error":
                return {
                  ...m,
                  error: ev.message || "Something went wrong.",
                  streaming: false,
                };
              case "done":
                return { ...m, streaming: false };
              default:
                return m;
            }
          })
        );
      };

      streamChat({
        message: clean,
        sessionId: activeId,
        onEvent: applyEvent,
        signal: controller.signal,
      })
        .catch((err) => {
          if (controller.signal.aborted) return;
          setMessages((prev) =>
            prev.map((m) =>
              m.id === asstId
                ? {
                    ...m,
                    error: err.message || "Request failed.",
                    streaming: false,
                  }
                : m
            )
          );
        })
        .finally(() => {
          setStreaming(false);
          abortRef.current = null;
          refreshSessions();
        });
    },
    [streaming, activeId, refreshSessions]
  );

  const newChat = useCallback(() => {
    if (abortRef.current) abortRef.current.abort();
    const id = newSessionId();
    persistSessionId(id);
    setActiveId(id);
    setMessages([]);
    setStreaming(false);
    setLoadingHistory(false);
  }, []);

  const stats = useMemo(() => {
    let tools = 0;
    let files = 0;
    let turns = 0;
    for (const m of messages) {
      if (m.role === "user") {
        turns += 1;
      } else {
        for (const s of m.steps || []) {
          if (s.kind === "tool") {
            tools += 1;
            if (s.name === "write_file") files += 1;
          }
        }
      }
    }
    return { tools, files, turns };
  }, [messages]);

  const connected = health.state === "ok";
  const showStats = messages.length > 0 || streaming;

  return (
    <div className="app">
      <Sidebar
        sessions={sessions}
        activeId={activeId}
        onSelect={loadSession}
        onNewChat={newChat}
        model={health.model}
        connected={connected}
      />
      <div className="main">
        {showStats && (
          <StatsStrip
            tools={stats.tools}
            files={stats.files}
            turns={stats.turns}
          />
        )}
        <main className="chat" ref={chatRef}>
          <div className="chat-inner">
            {loadingHistory ? (
              <div className="history-loading">
                <span className="spinner" aria-hidden="true" />
                Loading chat…
              </div>
            ) : messages.length === 0 ? (
              <EmptyState onPick={sendMessage} />
            ) : (
              messages.map((m) =>
                m.role === "user" ? (
                  <div key={m.id} className="msg-row user">
                    <div className="bubble">{m.text}</div>
                    <div className="ts">{fmtTime(m.ts)}</div>
                  </div>
                ) : (
                  <div key={m.id} className="msg-row assistant">
                    <Timeline steps={m.steps} streaming={m.streaming} />
                    {m.answer ? (
                      <div className="answer">{m.answer}</div>
                    ) : null}
                    {m.error ? (
                      <div className="error-row">{m.error}</div>
                    ) : null}
                    <div className="ts">{fmtTime(m.ts)}</div>
                  </div>
                )
              )
            )}
          </div>
        </main>
        <Composer onSend={sendMessage} streaming={streaming} />
      </div>
    </div>
  );
}
