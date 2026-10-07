function fmtTokens(n) {
  if (!Number.isFinite(n) || n < 0) return "0";
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;
}

/**
 * Session statistics strip. `tokens` is the session-wide prompt+completion
 * token total (defaults to 0 so existing call sites keep working).
 */
export default function StatsStrip({ tools, files, turns, tokens = 0 }) {
  const stats = [
    { label: "Tools used", value: tools },
    { label: "Files created", value: files },
    { label: "Turns", value: turns },
    { label: "Tokens", value: fmtTokens(tokens) },
  ];
  return (
    <div className="stats-strip" aria-label="Session statistics">
      {stats.map((s) => (
        <div className="stat" key={s.label}>
          <span className="stat-value">{s.value}</span>
          <span className="stat-label">{s.label}</span>
        </div>
      ))}
    </div>
  );
}
