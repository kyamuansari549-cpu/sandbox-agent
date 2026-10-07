import { useState } from "react";

/**
 * Message composer. While `streaming`, the Send button becomes a Stop button
 * that calls `onStop`. `blocked` disables sending (e.g. while an approval
 * modal is open) without hiding the input.
 */
export default function Composer({ onSend, onStop, streaming, blocked }) {
  const [value, setValue] = useState("");

  const submit = () => {
    const text = value.trim();
    if (!text || streaming || blocked) return;
    onSend(text);
    setValue("");
  };

  const disabled = streaming || blocked;

  return (
    <div className="composer-wrap">
      {streaming && <div className="working">Working…</div>}
      {blocked && !streaming && (
        <div className="working">Waiting for your approval above…</div>
      )}
      <div className="composer">
        <input
          className="composer-input"
          type="text"
          placeholder="Ask SandboxAgent to do something…"
          value={value}
          disabled={disabled}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
        />
        {streaming ? (
          <button className="btn stop" onClick={onStop}>
            Stop
          </button>
        ) : (
          <button
            className="btn primary"
            onClick={submit}
            disabled={blocked || !value.trim()}
          >
            Send
          </button>
        )}
      </div>
    </div>
  );
}
