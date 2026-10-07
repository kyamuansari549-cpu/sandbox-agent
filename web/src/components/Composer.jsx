import { useState } from "react";

export default function Composer({ onSend, streaming }) {
  const [value, setValue] = useState("");

  const submit = () => {
    const text = value.trim();
    if (!text || streaming) return;
    onSend(text);
    setValue("");
  };

  return (
    <div className="composer-wrap">
      {streaming && <div className="working">Working…</div>}
      <div className="composer">
        <input
          className="composer-input"
          type="text"
          placeholder="Ask SandboxAgent to do something…"
          value={value}
          disabled={streaming}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
        />
        <button
          className="btn primary"
          onClick={submit}
          disabled={streaming || !value.trim()}
        >
          Send
        </button>
      </div>
    </div>
  );
}
