import { Component, type ReactNode, useEffect, useState } from "react";
import { api, telegram } from "./api";
import { useToast } from "./components";
import { SessionsScreen } from "./screens/Sessions";
import { InboxScreen } from "./screens/Inbox";
import { BoardScreen } from "./screens/Board";
import { SessionScreen } from "./screens/Session";
import { ProposalsScreen } from "./screens/Proposals";
import { SchedulesScreen } from "./screens/Schedules";
import { UsageScreen } from "./screens/Usage";
import { SettingsScreen } from "./screens/Settings";

type Tab = "sessions" | "inbox" | "board" | "proposals" | "schedules" | "usage" | "settings";

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (this.state.error) {
      return (
        <div className="empty">
          <div>Something broke in this screen: {this.state.error.message}</div>
          <button className="btn" style={{ marginTop: 12 }} onClick={() => this.setState({ error: null })}>
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

const TABS: { id: Tab; label: string; glyph: string }[] = [
  { id: "sessions", label: "Bots", glyph: "◉" },
  { id: "inbox", label: "Inbox", glyph: "▣" },
  { id: "board", label: "Board", glyph: "☰" },
  { id: "proposals", label: "Changes", glyph: "⑂" },
  { id: "schedules", label: "Cron", glyph: "◷" },
  { id: "usage", label: "Usage", glyph: "▤" },
  { id: "settings", label: "Settings", glyph: "⚙" },
];

export function App() {
  const [tab, setTab] = useState<Tab>("sessions");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [toast, showToast] = useToast();
  const [unread, setUnread] = useState(0);

  useEffect(() => {
    const poll = () =>
      api
        .get<{ unread: number }>("/api/inbox/unread")
        .then((r) => setUnread(r.unread ?? 0))
        .catch(() => undefined);
    poll();
    const id = setInterval(poll, 20000);
    return () => clearInterval(id);
  }, [tab]);

  useEffect(() => {
    const tg = telegram();
    if (!tg?.initData) {
      const dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
      document.documentElement.dataset.scheme = dark ? "dark" : "light";
      return;
    }
    tg.ready();
    tg.expand();
    const apply = () => {
      document.documentElement.dataset.scheme = tg.colorScheme;
      for (const [key, value] of Object.entries(tg.themeParams ?? {})) {
        document.documentElement.style.setProperty(`--tg-theme-${key.replace(/_/g, "-")}`, value);
      }
    };
    const paint = () => {
      const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim() || "#000000";
      tg.setHeaderColor?.(bg);
      tg.setBackgroundColor?.(bg);
    };
    const onTheme = () => {
      apply();
      paint();
    };
    apply();
    paint();
    tg.onEvent("themeChanged", onTheme);
    return () => tg.offEvent?.("themeChanged", onTheme);
  }, []);

  // Telegram's own back button leaves a session; the vertical swipe must not close the app mid-chat.
  useEffect(() => {
    const tg = telegram();
    if (!tg?.initData || !tg.BackButton) return;
    if (!sessionId) {
      tg.BackButton.hide();
      tg.enableVerticalSwipes?.();
      return;
    }
    const back = () => setSessionId(null);
    tg.BackButton.onClick(back);
    tg.BackButton.show();
    tg.disableVerticalSwipes?.();
    return () => tg.BackButton?.offClick(back);
  }, [sessionId]);

  return (
    <div className="app">
      {sessionId ? (
        <ErrorBoundary key={sessionId}>
          <SessionScreen id={sessionId} onBack={() => setSessionId(null)} onOpen={setSessionId} toast={showToast} />
        </ErrorBoundary>
      ) : (
        <>
          <div className="topbar">
            <h1>Daedalus</h1>
            <div className="spacer" />
          </div>
          <div className="screen">
            <ErrorBoundary key={tab}>
              {tab === "sessions" && <SessionsScreen onOpen={setSessionId} toast={showToast} />}
              {tab === "inbox" && <InboxScreen onOpen={setSessionId} toast={showToast} onUnread={setUnread} />}
              {tab === "board" && <BoardScreen onOpen={setSessionId} toast={showToast} />}
              {tab === "proposals" && <ProposalsScreen toast={showToast} />}
              {tab === "schedules" && <SchedulesScreen toast={showToast} onOpen={setSessionId} />}
              {tab === "usage" && <UsageScreen />}
              {tab === "settings" && <SettingsScreen toast={showToast} />}
            </ErrorBoundary>
          </div>
          <nav className="tabbar">
            {TABS.map((t) => (
              <button key={t.id} className={tab === t.id ? "active" : ""} onClick={() => setTab(t.id)}>
                <span className="glyph" style={{ position: "relative" }}>
                  {t.glyph}
                  {t.id === "inbox" && unread > 0 && <span className="tab-badge">{unread > 99 ? "99+" : unread}</span>}
                </span>
                {t.label}
              </button>
            ))}
          </nav>
        </>
      )}
      {toast && <div className="toast" role="status" aria-live="polite">{toast}</div>}
    </div>
  );
}
