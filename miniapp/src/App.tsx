import { Component, type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, SessionSummary, telegram } from "./api";
import { StatusLabel } from "./components";
import { ConfirmHost, Sheet, ToastHost, toast as showToast } from "./dialogs";
import { SessionsScreen } from "./screens/Sessions";
import { InboxScreen } from "./screens/Inbox";
import { BoardScreen } from "./screens/Board";
import { SessionScreen } from "./screens/Session";
import { ProposalsScreen } from "./screens/Proposals";
import { SchedulesScreen } from "./screens/Schedules";
import { UsageScreen } from "./screens/Usage";
import { HealthScreen, SettingsScreen } from "./screens/Settings";
import { MemoryScreen } from "./screens/Memory";
import { ServicesScreen } from "./screens/Services";
import { LoginScreen } from "./screens/Login";
import { back, migrateLegacyLocation, navigate, pathFor, recallScroll, rememberScroll, sessionPath, useRoute } from "./router";
import { Counts, MoreSheet, Rail, TabBar } from "./shell";
import { useQuery } from "./store";

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (this.state.error) {
      return (
        <div className="empty">
          <b>Something broke in this screen</b>
          <div>{this.state.error.message}</div>
          <button className="btn" onClick={() => this.setState({ error: null })}>
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

export function App() {
  const route = useRoute();
  const wide = useWide();
  const [picking, setPicking] = useState(false);
  const [more, setMore] = useState(false);
  // Inside Telegram every request carries initData; outside, the browser needs a token or the session cookie.
  const [authed, setAuthed] = useState<boolean | null>(() => (telegram()?.initData ? true : null));
  const inbox = useQuery<{ unread: number }>(authed ? "/api/inbox/unread" : null, { pollMs: 20000, staleMs: 5000 });
  const proposals = useQuery<{ status: string }[]>(authed ? "/api/proposals" : null, { pollMs: 60000, staleMs: 30000 });
  const counts: Counts = { inbox: inbox.data?.unread ?? 0, changes: (proposals.data ?? []).filter((p) => p.status === "pending").length };

  useEffect(() => {
    if (authed !== null) return;
    api
      .get("/api/auth/me")
      .then(() => setAuthed(true))
      .catch(() => setAuthed(false));
  }, [authed]);

  useEffect(() => {
    const tg = telegram();
    migrateLegacyLocation(tg?.initDataUnsafe?.start_param);
    if (!tg?.initData) {
      const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
      const apply = () => {
        document.documentElement.dataset.scheme = mq?.matches ? "dark" : "light";
      };
      apply();
      mq?.addEventListener("change", apply);
      return () => mq?.removeEventListener("change", apply);
    }
    document.documentElement.dataset.tg = "1";
    tg.ready();
    tg.expand();
    // Reopened from the background, the app may come back collapsed: ask for the full height again.
    const onViewport = () => {
      if (!tg.isExpanded) tg.expand();
    };
    tg.onEvent("viewportChanged", onViewport);
    tg.onEvent("activated", onViewport);
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
    return () => {
      tg.offEvent?.("themeChanged", onTheme);
      tg.offEvent?.("viewportChanged", onViewport);
      tg.offEvent?.("activated", onViewport);
    };
  }, []);

  // Telegram's own back button leaves a detail; the vertical swipe must not close the app mid-chat.
  const inDetail = !!route.session || (route.screen === "settings" && !!route.detail);
  useEffect(() => {
    const tg = telegram();
    if (!tg?.initData || !tg.BackButton) return;
    if (!inDetail) {
      tg.BackButton.hide();
      tg.enableVerticalSwipes?.();
      return;
    }
    const onBack = () => back(pathFor(route.screen));
    tg.BackButton.onClick(onBack);
    tg.BackButton.show();
    tg.disableVerticalSwipes?.();
    return () => tg.BackButton?.offClick(onBack);
  }, [inDetail, route.screen]);

  // The list screens come back where the reader left them.
  const main = useRef<HTMLDivElement>(null);
  const scrollKey = route.session ? null : `${route.screen}/${route.detail ?? ""}`;
  const lastKey = useRef<string | null>(null);
  useLayoutEffect(() => {
    const el = main.current;
    if (!el) return;
    if (lastKey.current && lastKey.current !== scrollKey) rememberScroll(lastKey.current, el.scrollTop);
    if (scrollKey && lastKey.current !== scrollKey) el.scrollTop = recallScroll(scrollKey);
    lastKey.current = scrollKey;
  }, [scrollKey]);
  useEffect(() => {
    const el = main.current;
    if (!el) return;
    const on = () => {
      if (scrollKey) rememberScroll(scrollKey, el.scrollTop);
    };
    el.addEventListener("scroll", on, { passive: true });
    return () => el.removeEventListener("scroll", on);
  }, [scrollKey]);

  useEffect(() => {
    setMore(false);
  }, [route.screen, route.session]);

  const open = (id: string) => navigate(sessionPath(id));
  const closeSession = () => back(pathFor("agents"));

  if (authed === null) return <div className="app"><div className="empty">Loading…</div></div>;
  if (authed === false) {
    return (
      <div className="app">
        <LoginScreen onDone={() => setAuthed(true)} />
      </div>
    );
  }

  const sessionId = route.session;
  const secondId = wide ? route.with : null;
  let content: ReactNode;
  if (sessionId && secondId) {
    content = (
      <div className="dual">
        <ErrorBoundary key={sessionId}>
          <SessionScreen id={sessionId} pane="left" onBack={() => navigate(sessionPath(secondId), { replace: true })} onOpen={(id) => navigate(sessionPath(id, secondId))} toast={showToast} onSplit={() => setPicking(true)} />
        </ErrorBoundary>
        <ErrorBoundary key={secondId}>
          <SessionScreen id={secondId} pane="right" onBack={() => navigate(sessionPath(sessionId), { replace: true })} onOpen={(id) => navigate(sessionPath(sessionId, id))} toast={showToast} />
        </ErrorBoundary>
      </div>
    );
  } else if (sessionId) {
    content = (
      <ErrorBoundary key={sessionId}>
        <SessionScreen id={sessionId} onBack={closeSession} onOpen={open} toast={showToast} onSplit={wide ? () => setPicking(true) : undefined} />
      </ErrorBoundary>
    );
  } else {
    content = (
      <ErrorBoundary key={route.screen}>
        {route.screen === "agents" && <SessionsScreen onOpen={open} toast={showToast} />}
        {route.screen === "inbox" && <InboxScreen onOpen={open} toast={showToast} />}
        {route.screen === "board" && <BoardScreen onOpen={open} toast={showToast} />}
        {route.screen === "changes" && <ProposalsScreen toast={showToast} />}
        {route.screen === "schedules" && <SchedulesScreen toast={showToast} onOpen={open} />}
        {route.screen === "services" && <ServicesScreen onOpen={open} toast={showToast} />}
        {route.screen === "memory" && <MemoryScreen toast={showToast} onOpen={open} />}
        {route.screen === "usage" && <UsageScreen />}
        {route.screen === "health" && <HealthScreen toast={showToast} />}
        {route.screen === "settings" && <SettingsScreen toast={showToast} section={route.detail} />}
      </ErrorBoundary>
    );
  }

  return (
    <div className="app">
      {wide && <Rail screen={route.screen} counts={counts} />}
      <div ref={main} className={`main ${sessionId ? "chat-open" : ""}`}>
        {content}
      </div>
      {!wide && !sessionId && <TabBar screen={route.screen} counts={counts} onMore={() => setMore((m) => !m)} moreOpen={more} />}
      {more && <MoreSheet screen={route.screen} counts={counts} onClose={() => setMore(false)} />}
      {picking && sessionId && <SessionPicker exclude={sessionId} onPick={(id) => { navigate(sessionPath(sessionId, id)); setPicking(false); }} onClose={() => setPicking(false)} />}
      <ToastHost />
      <ConfirmHost />
    </div>
  );
}

/** Whether the layout is the wide one (rail beside the screen): the same breakpoint as the stylesheet. */
export function useWide(): boolean {
  const query = "(min-width: 1024px)";
  const [wide, setWide] = useState(() => window.matchMedia?.(query).matches ?? false);
  useEffect(() => {
    const mq = window.matchMedia?.(query);
    if (!mq) return;
    const on = () => setWide(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return wide;
}

/** Which session to open beside the current one. */
function SessionPicker({ exclude, onPick, onClose }: { exclude: string | null; onPick: (id: string) => void; onClose: () => void }) {
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [filter, setFilter] = useState("");
  useEffect(() => {
    api.get<SessionSummary[]>("/api/sessions").then(setSessions).catch(() => setSessions([]));
  }, []);
  const q = filter.trim().toLowerCase();
  const items = (sessions ?? []).filter((s) => s.id !== exclude && (!q || s.title.toLowerCase().includes(q) || s.id.includes(q)));
  return (
    <Sheet title="Open beside" onClose={onClose} size="narrow">
      <input className="field" autoFocus placeholder="filter by title" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ marginBottom: 8 }} />
      {sessions === null && <div className="empty">Loading…</div>}
      {sessions !== null && items.length === 0 && <div className="empty">No other sessions.</div>}
      {items.map((s) => (
        <button key={s.id} className="menu-item" onClick={() => onPick(s.id)}>
          <span className="grow truncate">{s.title}</span>
          <StatusLabel status={s.status} />
        </button>
      ))}
    </Sheet>
  );
}
