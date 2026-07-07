import { useEffect, useState } from "react";
import type { Tab } from "../App";
import { API_BASE, onRequestMeta, type RequestMeta } from "../api";

const TABS: Array<{ key: string; tab: Tab; label: string }> = [
  { key: "1", tab: "find", label: "find" },
  { key: "2", tab: "research", label: "research" },
  { key: "3", tab: "passages", label: "passages" },
];

export function TopBar({ tab, onTab }: { tab: Tab; onTab: (tab: Tab) => void }) {
  const [meta, setMeta] = useState<RequestMeta | null>(null);
  useEffect(() => onRequestMeta(setMeta), []);

  const host = API_BASE.replace(/^https?:\/\//, "");

  return (
    <header className="topbar">
      <span className="wordmark">ATHENA</span>
      <nav className="tabs" aria-label="views">
        {TABS.map(({ key, tab: t, label }) => (
          <button
            key={t}
            className={`tab${tab === t ? " tab-active" : ""}`}
            onClick={() => onTab(t)}
            aria-current={tab === t ? "page" : undefined}
          >
            [{key}] {label}
          </button>
        ))}
      </nav>
      <div className="status" aria-live="polite">
        <span className="status-dim">base_url={host}</span>
        {meta === null ? (
          <span className="status-dim"> · idle</span>
        ) : meta.pending ? (
          <span className="status-accent"> · ···</span>
        ) : meta.status === null ? (
          <span> · network error</span>
        ) : (
          <span>
            {" "}
            · <span className={meta.status < 400 ? "status-accent" : ""}>
              {meta.status} {meta.statusText}
            </span>{" "}
            · {meta.ms}ms
          </span>
        )}
      </div>
    </header>
  );
}
