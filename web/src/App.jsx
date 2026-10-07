import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Sidebar from "./components/Sidebar.jsx";
import StatsStrip from "./components/StatsStrip.jsx";
import EmptyState from "./components/EmptyState.jsx";
import Composer from "./components/Composer.jsx";
import Timeline from "./components/Timeline.jsx";
import ApprovalDialog from "./components/ApprovalDialog.jsx";
import ScheduledTasks from "./components/ScheduledTasks.jsx";
import {
  checkHealth,
  streamChat,
  listSessions,
  getSession,
  fetchModels,
  stopRun,
  approveRun,
} from "./api.js";

const SESSION_KEY = "sandbox-agent-session";
const MODEL_KEY = "sandbox-agent-model";
const FALLBACK_MODEL = "qwen2.5:7b";

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

function loadSavedModel() {
  try {
    return localStorage.getItem(MODEL_KEY) || FALLBACK_MODEL;
  } catch {
    return FALLBACK_MODEL;
  }
}

function persistModel(model) {
  try {
    localStorage.setItem(MODEL_KEY, model);
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

function fmtTokens(n) {
  if (!Number.isFinite(n) || n < 0) return "0";
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;
}

function usageTotal(usage) {
  if (!usage) return 0;
  return (usage.prompt_tokens || 0) + (usage.completion_tokens || 0);
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
      stopped: false,
      usage: t.usage || null,
      model: t.model || null,
      ts: Number.isNaN(ts) ? Date.now() : ts,
    };
  });
}

export default function App() {
  const [messages, setMessages] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [activeId, setActiveId] = useState(loadSessionId);
  const [health, setHealth] = useState({ state: "checking" });
  const [streaming, setStreaming] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [runId, setRunId] = useState(null);
  const [approval, setApproval] = useState(null);
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [view, setView] = useState("chat");
  const [models, setModels] = useState([]);
  const [modelsLoaded, setModelsLoaded] = useState(false);
  const [defaultModel, setDefaultModel] = useState(FALLBACK_MODEL);
  const [selectedModel, setSelectedModel] = useState(loadSavedModel);
  const abortRef = useRef(null);
  const chatRef = useRef(null);

  const refreshSessions = useCallback(() => {
    listSessions()
      .then(setSessions)
      .catch(() => {
        /* backend down — sessions list stays empty, chat still works */
      });
  }, []);

  // Backend health + session list + model list on mount.
  useEffect(() => {
    let cancelled = false;
    checkHealth()
      .then(() => {
        if (!cancelled) setHealth({ state: "ok" });
      })
      .catch(() => {
        if (!cancelled) setHealth({ state: "fail" });
      });
    fetchModels()
      .then(({ models: list, default: def }) => {
        if (cancelled) return;
        const d = def || FALLBACK_MODEL;
        setModels(list);
        setModelsLoaded(true);
        setDefaultModel(d);
        // Keep the saved choice when the backend still offers it; otherwise
        // fall back to the backend default.
        setSelectedModel((prev) => {
          if (list.length > 0 && !list.includes(prev)) {
            persistModel(d);
            return d;
          }
          return prev;
        });
      })
      .catch(() => {
        if (!cancelled) {
          setModels([]);
          setModelsLoaded(true);
        }
      });
    refreshSessions();
    return () => {
      cancelled = true;
    };
  }, [refreshSessions]);

  const resetRunState = useCallback(() => {
    setRunId(null);
    setApproval(null);
    setApprovalBusy(false);
  }, []);

  const loadSession = useCallback(
    (id) => {
      if (abortRef.current) abortRef.current.abort();
      setStreaming(false);
      resetRunState();
      setView("chat");
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
    [resetRunState]
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

  const handleModelChange = useCallback((model) => {
    setSelectedModel(model);
    persistModel(model);
  }, []);

  const sendMessage = useCallback(
    (text) => {
      if (streaming || approval) return;
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
        stopped: false,
        usage: null,
        model: null,
        ts: Date.now(),
      };
      setMessages((prev) => [...prev, userMsg, asstMsg]);
      setStreaming(true);
      setRunId(null);

      const controller = new AbortController();
      abortRef.current = controller;
      const asstId = asstMsg.id;

      const applyEvent = (ev) => {
        if (ev.type === "run" && ev.run_id) {
          setRunId(ev.run_id);
          return;
        }
        if (ev.type === "session" && ev.session_id) {
          setActiveId(ev.session_id);
          persistSessionId(ev.session_id);
          return;
        }
        if (ev.type === "approval_required") {
          setApproval({
            runId: ev.run_id,
            command: ev.command || "",
            reason: ev.reason || "",
          });
        }
        if (ev.type === "approval_resolved") {
          setApproval(null);
          setApprovalBusy(false);
        }
        if (
          ev.type === "done" ||
          ev.type === "error" ||
          ev.type === "stopped"
        ) {
          // The run is over (or dead) — no pending approval can survive it.
          setRunId(null);
          setApproval(null);
          setApprovalBusy(false);
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
                      awaitingApproval: false,
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
              case "approval_required": {
                // Mark the most recent pending tool as waiting on approval.
                const steps = m.steps.slice();
                for (let i = steps.length - 1; i >= 0; i--) {
                  const s = steps[i];
                  if (s.kind === "tool" && s.result == null) {
                    steps[i] = { ...s, awaitingApproval: true };
                    break;
                  }
                }
                return { ...m, steps };
              }
              case "approval_resolved": {
                const steps = m.steps.map((s) =>
                  s.kind === "tool" && s.awaitingApproval
                    ? { ...s, awaitingApproval: false }
                    : s
                );
                return { ...m, steps };
              }
              case "token":
                return { ...m, answer: (m.answer || "") + (ev.content ?? "") };
              case "answer":
                // SET, not append — idempotent with the token stream.
                return { ...m, answer: ev.content ?? "" };
              case "usage":
                return {
                  ...m,
                  usage: {
                    prompt_tokens:
                      (m.usage?.prompt_tokens || 0) + (ev.prompt_tokens || 0),
                    completion_tokens:
                      (m.usage?.completion_tokens || 0) +
                      (ev.completion_tokens || 0),
                  },
                };
              case "error":
                return {
                  ...m,
                  error: ev.message || "Something went wrong.",
                  streaming: false,
                };
              case "stopped":
                return { ...m, stopped: true, streaming: false };
              case "done":
                return {
                  ...m,
                  streaming: false,
                  model: ev.model || m.model,
                  usage: ev.usage
                    ? {
                        prompt_tokens: ev.usage.prompt_tokens || 0,
                        completion_tokens: ev.usage.completion_tokens || 0,
                      }
                    : m.usage,
                };
              default:
                return m;
            }
          })
        );
      };

      streamChat({
        message: clean,
        sessionId: activeId,
        model: selectedModel,
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
          setRunId(null);
          setApproval(null);
          setApprovalBusy(false);
          abortRef.current = null;
          refreshSessions();
        });
    },
    [streaming, approval, activeId, selectedModel, refreshSessions]
  );

  const stopStream = useCallback(() => {
    if (runId) {
      // The backend answers with `stopped` then `done`; those events end
      // the stream normally. No client-side abort — the stream must stay
      // open to receive them.
      stopRun(runId).catch(() => {
        /* surfaced via the stream's error event */
      });
    }
  }, [runId]);

  const resolveApproval = useCallback(
    (decision) => {
      if (!approval || approvalBusy) return;
      setApprovalBusy(true);
      approveRun(approval.runId, decision)
        .catch(() => {
          // Keep the dialog open; the loop will time out or the user can
          // stop the run from the composer.
        })
        .finally(() => setApprovalBusy(false));
      // The dialog closes on the `approval_resolved` event.
    },
    [approval, approvalBusy]
  );

  const newChat = useCallback(() => {
    if (abortRef.current) abortRef.current.abort();
    const id = newSessionId();
    persistSessionId(id);
    setActiveId(id);
    setMessages([]);
    setStreaming(false);
    resetRunState();
    setView("chat");
    setLoadingHistory(false);
  }, [resetRunState]);

  const stats = useMemo(() => {
    let tools = 0;
    let files = 0;
    let turns = 0;
    let tokens = 0;
    for (const m of messages) {
      if (m.role === "user") {
        turns += 1;
      } else {
        tokens += usageTotal(m.usage);
        for (const s of m.steps || []) {
          if (s.kind === "tool") {
            tools += 1;
            if (s.name === "write_file") files += 1;
          }
        }
      }
    }
    return { tools, files, turns, tokens };
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
        view={view}
        onViewChange={setView}
        models={models}
        modelsLoaded={modelsLoaded}
        selectedModel={selectedModel}
        defaultModel={defaultModel}
        onModelChange={handleModelChange}
        connected={connected}
      />
      <div className="main">
        {view === "tasks" ? (
          <ScheduledTasks defaultModel={selectedModel} />
        ) : (
          <>
            {showStats && (
              <StatsStrip
                tools={stats.tools}
                files={stats.files}
                turns={stats.turns}
                tokens={stats.tokens}
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
                        <div className="msg-footer">
                          <span className="ts">{fmtTime(m.ts)}</span>
                          {usageTotal(m.usage) > 0 && (
                            <span className="usage-line">
                              {fmtTokens(usageTotal(m.usage))} tokens
                              {m.model ? ` · ${m.model}` : ""}
                            </span>
                          )}
                          {m.stopped && (
                            <span className="stopped-note">Stopped by you</span>
                          )}
                        </div>
                      </div>
                    )
                  )
                )}
              </div>
            </main>
            <Composer
              onSend={sendMessage}
              onStop={stopStream}
              streaming={streaming}
              blocked={!!approval}
            />
          </>
        )}
      </div>
      <ApprovalDialog
        approval={approval}
        busy={approvalBusy}
        onDecision={resolveApproval}
      />
    </div>
  );
}
