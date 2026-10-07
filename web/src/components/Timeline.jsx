import ToolCard from "./ToolCard.jsx";

function StatusRow({ text, streaming }) {
  return (
    <div className="step-status">
      {streaming ? (
        <span className="spinner sm" aria-hidden="true" />
      ) : (
        <span className="status-dot" aria-hidden="true" />
      )}
      <span className="status-text">{text}</span>
    </div>
  );
}

export default function Timeline({ steps, streaming }) {
  if (!steps || steps.length === 0) {
    return streaming ? (
      <div className="timeline">
        <StatusRow text="Starting…" streaming={streaming} />
      </div>
    ) : null;
  }
  return (
    <div className="timeline">
      {steps.map((step) =>
        step.kind === "status" ? (
          <StatusRow key={step.id} text={step.text} streaming={streaming} />
        ) : (
          <ToolCard key={step.id} step={step} />
        )
      )}
    </div>
  );
}
