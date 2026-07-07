import { useEffect, useState } from "react";
import { FindTab } from "./components/FindTab";
import { PassagesTab } from "./components/PassagesTab";
import { ResearchTab } from "./components/ResearchTab";
import { TopBar } from "./components/TopBar";

export type Tab = "find" | "research" | "passages";

const TAB_KEYS: Record<string, Tab> = { "1": "find", "2": "research", "3": "passages" };

export function App() {
  const [tab, setTab] = useState<Tab>("find");

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
      const next = TAB_KEYS[event.key];
      if (next) setTab(next);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  // Inactive tabs stay mounted (hidden) so queries and results survive
  // switching between tabs.
  return (
    <div className="app">
      <TopBar tab={tab} onTab={setTab} />
      <main className="tab-host">
        <section hidden={tab !== "find"}>
          <FindTab />
        </section>
        <section hidden={tab !== "research"}>
          <ResearchTab />
        </section>
        <section hidden={tab !== "passages"}>
          <PassagesTab />
        </section>
      </main>
    </div>
  );
}
