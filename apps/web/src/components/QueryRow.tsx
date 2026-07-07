import { useState, type ReactNode } from "react";

// Shared prompt row: "<prompt>" label, wide monospace input (Enter submits),
// endpoint being called shown right-aligned.
export function QueryRow({
  prompt,
  endpoint,
  value,
  onChange,
  onSubmit,
  placeholder,
  busy,
  children,
}: {
  prompt: string;
  endpoint: string;
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  placeholder: string;
  busy: boolean;
  children?: ReactNode;
}) {
  const [focused, setFocused] = useState(false);

  return (
    <div className="query-row">
      <label className="prompt">
        <span className="prompt-label">{prompt}</span>
        <span className="prompt-input-wrap">
          <input
            className="prompt-input"
            type="text"
            value={value}
            placeholder={placeholder}
            spellCheck={false}
            autoComplete="off"
            onChange={(event) => onChange(event.target.value)}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !busy) onSubmit();
            }}
          />
          {value === "" && !focused && (
            <span className="prompt-cursor" aria-hidden="true">
              ▌
            </span>
          )}
        </span>
      </label>
      {children}
      <span className="endpoint">{endpoint}</span>
    </div>
  );
}

export function InlineError({ message }: { message: string }) {
  return (
    <p className="inline-error" role="alert">
      ! {message}
    </p>
  );
}

export function Loading({ label }: { label: string }) {
  return <p className="loading">{label}</p>;
}

export function SecLink({ href }: { href: string }) {
  return (
    <a className="sec-link" href={href} target="_blank" rel="noopener noreferrer">
      view in SEC filing ↗
    </a>
  );
}
