import { useCallback, useEffect, useState } from "react";
import { listTasks, createTask, deleteTask, getTaskRuns } from "../api.js";

function relTime(iso) {
  if (!iso) return "never";
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
  return new Date(t).toLocaleDateString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 60 -> "every 1m", 3600 -> "every 1h", 86400 -> "every 1d". */
function humanizeInterval(sec) {
  if (!Number.isFinite(sec) || sec <= 0) return "";
  if (sec % 86400 === 0) {
    const d = sec / 86400;
    return `every ${d}d`;
  }
  if (sec % 3600 === 0) {
    const h = sec / 3600;
    return `every ${h}h`;
  }
  if (sec % 60 === 0) {
    const m = sec / 60;
    return `every ${m}m`;
  }
  return `every ${sec}s`;
}

function statusClass(status) {
  const s = String(status || "").toLowerCase();
  if (["success", "succeeded", "ok", "completed", "done"].includes(s)) return "ok";
  if (["failed", "failure", "error"].includes(s)) return "bad";
  return "idle";
}

function shortRunId(id) {
  const s = String(id || "");
  return s.length > 8 ? s.slice(0, 8) : s;
}

export default function ScheduledTasks({ defaultModel }) {
  const [tasks, setTasks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [runs, setRuns] = useState({});
  const [runsLoading, setRunsLoading] = useState({});
  const [form, setForm] = useState({ name: "", prompt: "", interval: "600", model: "" });
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState(null);
  const [deleting, setDeleting] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setTasks(await listTasks());
    } catch (e) {
      setError(e.message || "Failed to load scheduled tasks.");
    } finally {
      setLoading(false);
    }
  }, []);

  // Poll on view open.
  useEffect(() => {
    refresh();
  }, [refresh]);

  const toggleRuns = useCallback(
    async (id) => {
      if (expanded === id) {
        setExpanded(null);
        return;
      }
      setExpanded(id);
      if (!runs[id]) {
        setRunsLoading((s) => ({ ...s, [id]: true }));
        try {
          const list = await getTaskRuns(id);
          setRuns((s) => ({ ...s, [id]: list }));
        } catch {
          setRuns((s) => ({ ...s, [id]: [] }));
        } finally {
          setRunsLoading((s) => ({ ...s, [id]: false }));
        }
      }
    },
    [expanded, runs]
  );

  const submit = async (e) => {
    e.preventDefault();
    const interval = parseInt(form.interval, 10);
    if (!form.name.trim() || !form.prompt.trim()) {
      setFormError("Name and prompt are required.");
      return;
    }
    if (!Number.isFinite(interval) || interval < 60) {
      setFormError("Interval must be a number of seconds, at least 60.");
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      await createTask({
        name: form.name.trim(),
        prompt: form.prompt.trim(),
        interval_seconds: interval,
        model: form.model.trim() || undefined,
      });
      setForm({ name: "", prompt: "", interval: "600", model: "" });
      await refresh();
    } catch (err) {
      setFormError(err.message || "Failed to create task.");
    } finally {
      setSaving(false);
    }
  };

  const remove = async (id) => {
    setDeleting(id);
    try {
      await deleteTask(id);
      setTasks((t) => t.filter((x) => x.id !== id));
      if (expanded === id) setExpanded(null);
    } catch {
      /* keep the task; list refresh below restores truth */
    } finally {
      setDeleting(null);
    }
  };

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  return (
    <div className="tasks-view">
      <div className="tasks-inner">
        <div>
          <h1 className="tasks-title">Scheduled tasks</h1>
          <p className="tasks-sub">
            Recurring agent runs. The agent executes the prompt on the interval
            you set, even when you are away.
          </p>
        </div>

        <form className="task-form" onSubmit={submit}>
          <div className="task-form-title">New scheduled task</div>
          <div className="form-row">
            <div className="field grow">
              <label htmlFor="task-name">Name</label>
              <input
                id="task-name"
                className="field-input"
                type="text"
                placeholder="e.g. Morning market brief"
                value={form.name}
                onChange={set("name")}
              />
            </div>
            <div className="field">
              <label htmlFor="task-interval">Interval (seconds, ≥ 60)</label>
              <input
                id="task-interval"
                className="field-input narrow"
                type="number"
                min="60"
                step="1"
                value={form.interval}
                onChange={set("interval")}
              />
            </div>
            <div className="field">
              <label htmlFor="task-model">Model (optional)</label>
              <input
                id="task-model"
                className="field-input narrow"
                type="text"
                placeholder={defaultModel || "default"}
                value={form.model}
                onChange={set("model")}
              />
            </div>
          </div>
          <div className="field">
            <label htmlFor="task-prompt">Prompt</label>
            <textarea
              id="task-prompt"
              className="field-textarea"
              placeholder="What should the agent do each run?"
              value={form.prompt}
              onChange={set("prompt")}
            />
          </div>
          {formError && <div className="error-row">{formError}</div>}
          <div>
            <button className="btn primary" type="submit" disabled={saving}>
              {saving ? "Creating…" : "Create task"}
            </button>
          </div>
        </form>

        {loading ? (
          <div className="history-loading">
            <span className="spinner" aria-hidden="true" />
            Loading scheduled tasks…
          </div>
        ) : error ? (
          <div className="error-row">{error}</div>
        ) : tasks.length === 0 ? (
          <div className="task-empty">
            No scheduled tasks yet. Create one above.
          </div>
        ) : (
          <div className="task-list">
            {tasks.map((t) => (
              <div className="task-card" key={t.id}>
                <div className="task-head">
                  <div className="task-name">{t.name}</div>
                  <button
                    className="btn small danger-ghost"
                    onClick={() => remove(t.id)}
                    disabled={deleting === t.id}
                  >
                    {deleting === t.id ? "Deleting…" : "Delete"}
                  </button>
                </div>
                <div className="task-meta">
                  <span>{humanizeInterval(t.interval_seconds)}</span>
                  <span className="meta-sep">·</span>
                  <span className="mono">{t.model || "default model"}</span>
                  <span className="meta-sep">·</span>
                  <span className={t.enabled ? "enabled-yes" : "enabled-no"}>
                    {t.enabled ? "enabled" : "disabled"}
                  </span>
                </div>
                <div className="task-last">
                  <span
                    className={`run-dot ${statusClass(t.last_status)}`}
                    aria-hidden="true"
                  />
                  <span>
                    Last run: {relTime(t.last_run_at)}
                    {t.last_status ? ` · ${t.last_status}` : ""}
                  </span>
                </div>
                <button
                  className="task-runs-toggle"
                  onClick={() => toggleRuns(t.id)}
                >
                  <span className={`chev ${expanded === t.id ? "open" : ""}`}>
                    ▸
                  </span>
                  Past runs
                </button>
                {expanded === t.id && (
                  <div className="task-runs">
                    {runsLoading[t.id] ? (
                      <div className="history-loading slim">
                        <span className="spinner sm" aria-hidden="true" />
                        Loading runs…
                      </div>
                    ) : (runs[t.id] || []).length === 0 ? (
                      <div className="runs-empty">No runs yet.</div>
                    ) : (
                      runs[t.id].map((r) => (
                        <div className="run-row" key={r.run_id}>
                          <span
                            className={`run-dot ${statusClass(r.status)}`}
                            aria-hidden="true"
                          />
                          <span className="mono run-id">
                            {shortRunId(r.run_id)}
                          </span>
                          <span className="run-time">
                            {relTime(r.started_at)}
                          </span>
                          <span className="run-status">{r.status}</span>
                          {r.summary && (
                            <span className="run-summary">{r.summary}</span>
                          )}
                        </div>
                      ))
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
