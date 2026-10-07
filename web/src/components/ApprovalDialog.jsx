/** Modal shown when the agent loop pauses for approval of a command. */

export default function ApprovalDialog({ approval, busy, onDecision }) {
  if (!approval) return null;

  return (
    <div
      className="approval-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Approval required"
    >
      <div className="approval-dialog">
        <div className="approval-title">Approval required</div>
        <p className="approval-reason">
          {approval.reason ||
            "The agent wants to run a command that needs your approval."}
        </p>
        <pre className="approval-command">{approval.command || ""}</pre>
        <div className="approval-actions">
          <button
            className="btn danger"
            onClick={() => onDecision("deny")}
            disabled={busy}
          >
            Deny
          </button>
          <button
            className="btn primary"
            onClick={() => onDecision("approve")}
            disabled={busy}
          >
            {busy ? "Sending…" : "Approve"}
          </button>
        </div>
      </div>
    </div>
  );
}
