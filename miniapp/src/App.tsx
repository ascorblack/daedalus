import { useEffect, useState } from "react";
import { telegram } from "./api";
import { useToast } from "./components";
import { SessionsScreen } from "./screens/Sessions";
import { SessionScreen } from "./screens/Session";
import { ProposalsScreen } from "./screens/Proposals";
import { SchedulesScreen } from "./screens/Schedules";
import { UsageScreen } from "./screens/Usage";
import { SettingsScreen } from "./screens/Settings";

type Tab = "sessions" | "proposals" | "schedules" | "usage" | "settings";

const TABS: { id: Tab; label: string; glyph: string }[] = [
  { id: "sessions", label: "Bots", glyph: "◉" },
  { id: "proposals", label: "Changes", glyph: "⑂" },
  { id: "schedules", label: "Cron", glyph: "◷" },
  { id: "usage", label: "Usage", glyph: "▤" },
  { id: "settings", label: "Settings", glyph: "⚙" },
];

export function App() {
  const [tab, setTab] = useState<Tab>("sessions");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [toast, showToast] = useToast();

  useEffect(() => {
    const tg = telegram();
    if (!tg) return;
    tg.ready();
    tg.expand();
    const apply = () => {
      document.documentElement.dataset.scheme = tg.colorScheme;
      for (const [key, value] of Object.entries(tg.themeParams ?? {})) {
        document.documentElement.style.setProperty(`--tg-theme-${key.replace(/_/g, "-")}`, value);
      }
    };
    apply();
    tg.onEvent("themeChanged", apply);
  }, []);

  return (
    <div className="app">
      {sessionId ? (
        <SessionScreen id={sessionId} onBack={() => setSessionId(null)} toast={showToast} />
      ) : (
        <>
          <div className="topbar">
            <h1>Daedalus</h1>
            <div className="spacer" />
          </div>
          <div className="screen">
            {tab === "sessions" && <SessionsScreen onOpen={setSessionId} toast={showToast} />}
            {tab === "proposals" && <ProposalsScreen toast={showToast} />}
            {tab === "schedules" && <SchedulesScreen toast={showToast} onOpen={setSessionId} />}
            {tab === "usage" && <UsageScreen />}
            {tab === "settings" && <SettingsScreen toast={showToast} />}
          </div>
          <nav className="tabbar">
            {TABS.map((t) => (
              <button key={t.id} className={tab === t.id ? "active" : ""} onClick={() => setTab(t.id)}>
                <span className="glyph">{t.glyph}</span>
                {t.label}
              </button>
            ))}
          </nav>
        </>
      )}
      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
