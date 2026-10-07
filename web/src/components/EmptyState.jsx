const EXAMPLES = [
  {
    title: "Explore the project",
    prompt: "List the files in this project and tell me what each one does",
  },
  {
    title: "Research the web",
    prompt: "Search the web for the latest Python release and summarize what's new",
  },
  {
    title: "Write and run code",
    prompt: "Write a Python script that prints the first 10 Fibonacci numbers, then run it",
  },
];

export default function EmptyState({ onPick }) {
  return (
    <div className="empty">
      <div className="empty-mark" aria-hidden="true">
        S
      </div>
      <h1 className="empty-title">SandboxAgent</h1>
      <p className="empty-sub">
        Your own AI agent with a computer. Give it a task in plain language —
        it thinks, picks tools, runs commands in a sandbox, reads files and
        searches the web, and you watch every step happen live.
      </p>
      <div className="empty-suggestions">
        {EXAMPLES.map((ex) => (
          <button
            key={ex.title}
            className="sugg"
            onClick={() => onPick(ex.prompt)}
          >
            <span className="sugg-title">{ex.title}</span>
            <span className="sugg-prompt">{ex.prompt}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
