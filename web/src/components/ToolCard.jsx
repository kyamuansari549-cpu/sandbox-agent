import { useState } from "react";
import { downloadUrl } from "../api.js";

/* Single-glyph tool markers (no emoji). */
const TOOL_ICONS = {
  run_command: "»",
  read_file: "r",
  write_file: "w",
  list_dir: "d",
  fetch_url: "u",
  web_search: "?",
};

const MAX_SUMMARY = 90;
const MAX_OUTPUT_PREVIEW = 1500;

function truncate(s, n) {
  if (s.length <= n) return s;
  return s.slice(0, n) + "…";
}

/** One-line human summary of a tool's arguments for the card header. */
function argSummary(name, args) {
  const a = args || {};
  switch (name) {
    case "web_search":
      return a.query ? String(a.query) : "";
    case "fetch_url":
      return a.url ? String(a.url) : "";
    case "run_command":
      return a.command ? truncate(String(a.command), MAX_SUMMARY) : "";
    case "read_file":
      return a.path ? String(a.path) : "";
    case "write_file": {
      const content = a.content ? String(a.content) : "";
      const where = a.path ? String(a.path) : "";
      return content ? `${where} · ${content.length} chars` : where;
    }
    case "list_dir":
      return a.path ? String(a.path) : ".";
    default:
      return truncate(JSON.stringify(a), MAX_SUMMARY);
  }
}

export default function ToolCard({ step }) {
  const [showArgs, setShowArgs] = useState(false);
  const [showOutput, setShowOutput] = useState(false);

  const icon = TOOL_ICONS[step.name] || "t";
  const summary = argSummary(step.name, step.args);
  const hasOutput = step.result != null && step.result !== "";
  const outputTruncated =
    hasOutput && step.result.length > MAX_OUTPUT_PREVIEW;
  const filePath =
    step.name === "write_file" && step.args && step.args.path
      ? String(step.args.path)
      : null;

  return (
    <div className="tool-card">
      <button
        className="tool-head"
        onClick={() => setShowArgs((v) => !v)}
        title="Toggle full arguments"
      >
        <span className="tool-icon" aria-hidden="true">{icon}</span>
        <span className="tool-name">{step.name}</span>
        {summary && <span className="tool-args">{summary}</span>}
        <span className={`chev ${showArgs ? "open" : ""}`}>▸</span>
      </button>

      {showArgs && (
        <pre className="tool-json">
          {JSON.stringify(step.args || {}, null, 2)}
        </pre>
      )}

      {step.awaitingApproval ? (
        <div className="tool-pending approval-wait">
          <span className="spinner sm" /> Waiting for your approval…
        </div>
      ) : hasOutput ? (
        <>
          <button
            className="tool-out-toggle"
            onClick={() => setShowOutput((v) => !v)}
          >
            <span className={`chev ${showOutput ? "open" : ""}`}>▸</span>
            Output
          </button>
          {showOutput && (
            <pre className="tool-output">
              {outputTruncated
                ? step.result.slice(0, MAX_OUTPUT_PREVIEW) +
                  `\n… [truncated, showing first ${MAX_OUTPUT_PREVIEW} chars]`
                : step.result}
            </pre>
          )}
        </>
      ) : (
        <div className="tool-pending">
          <span className="spinner sm" /> running…
        </div>
      )}

      {filePath && (
        <div className="tool-actions">
          <a
            className="tool-download"
            href={downloadUrl(filePath)}
            target="_blank"
            rel="noreferrer"
            download
          >
            Download file
          </a>
        </div>
      )}
    </div>
  );
}
