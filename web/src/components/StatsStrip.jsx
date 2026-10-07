export default function StatsStrip({ tools, files, turns }) {
  const stats = [
    { label: "Tools used", value: tools },
    { label: "Files created", value: files },
    { label: "Turns", value: turns },
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
