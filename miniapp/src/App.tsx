import { Component, Suspense, lazy, type ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, SessionList, SessionSummary, telegram, TerminalList } from "./api";
import { StatusLabel } from "./components";
import { ConfirmHost, Sheet, ToastHost, toast as showToast } from "./dialogs";
import type { AuthConfig } from "./screens/Login";
import type { OnboardingState } from "./screens/AddModel";
import * as passkeys from "./passkeys";
import { back, migrateLegacyLocation, navigate, pathFor, projectHome, projectPagePath, recallScroll, rememberScroll, sessionPath, useRoute } from "./router";
import { Counts, MoreSheet, Palette, PaletteItem, TabBar, go, screenTitle, useMedia, useShortcuts } from "./shell";
import { Sidebar, useSidebar } from "./sidebar";
import { NavMenu } from "./navmenu";
import { shortcutFor } from "./navigation";
import { readSidebar, rememberSidebar, usePaneWidth } from "./layout";
import { Capabilities, SelfDevMode, visibleScreens } from "./capabilities";
import { ProjectSwitcher, rememberProject, storedProject, useProjects } from "./projects";
import { projectPath } from "./folders";
import { ChangeStrip } from "./change";
import { MaintenanceNotice } from "./maintenance";
import { SCREENS } from "./router";
import { peek, useOffline, useQuery } from "./store";
import { t, useLang } from "./i18n";
import { startPresence } from "./presence";
import { insideTerminal } from "./terminal/keys";
import { startEvents } from "./events";
import { useSummary } from "./notifications";
import { NotificationToasts } from "./toasts";
import { listenForOpen, syncPush } from "./push";
import { Icon } from "./icons";
import { focusView } from "./project/focus";
import { MainEntry } from "./main/MainEntry";

// One screen per chunk: opening the app downloads the shell and the screen it lands on, not the
// settings, the usage charts and the conversation view as well. The service worker keeps each
// chunk once it has been used, so a screen visited before opens offline too.
//
// `lazy` remembers the promise it was given, rejection included, so a chunk that failed to arrive
// once never arrives at all: re-rendering the screen replays the same rejection and only a reload
// recovers. The target here is a phone on a flaky link, where a failed chunk is a normal event and
// not a broken build, so the loader is retried a couple of times before the boundary sees it.
function screen<T>(load: () => Promise<T>): () => Promise<T> {
  return async () => {
    for (let attempt = 0; ; attempt++) {
      try {
        return await load();
      } catch (e) {
        if (attempt >= 2) throw e;
        await new Promise((r) => setTimeout(r, 400 * (attempt + 1)));
      }
    }
  };
}

/** A chunk that is not where the page thinks it is: the build moved under an open page. */
function isChunkError(message: string): boolean {
  return /dynamically imported|Importing a module script failed|error loading dynamically imported/i.test(message);
}

const StartScreen = lazy(screen(() => import("./screens/Start").then((m) => ({ default: m.StartScreen }))));
const InboxScreen = lazy(screen(() => import("./screens/Inbox").then((m) => ({ default: m.InboxScreen }))));
const BoardScreen = lazy(screen(() => import("./screens/Board").then((m) => ({ default: m.BoardScreen }))));
const SessionScreen = lazy(screen(() => import("./screens/Session").then((m) => ({ default: m.SessionScreen }))));
const VoiceScreen = lazy(screen(() => import("./screens/Voice").then((m) => ({ default: m.VoiceScreen }))));
const ProposalsScreen = lazy(screen(() => import("./screens/Proposals").then((m) => ({ default: m.ProposalsScreen }))));
const SchedulesScreen = lazy(screen(() => import("./screens/Schedules").then((m) => ({ default: m.SchedulesScreen }))));
const UsageScreen = lazy(screen(() => import("./screens/Usage").then((m) => ({ default: m.UsageScreen }))));
const SettingsScreen = lazy(screen(() => import("./screens/Settings").then((m) => ({ default: m.SettingsScreen }))));
const HealthScreen = lazy(screen(() => import("./screens/Settings").then((m) => ({ default: m.HealthScreen }))));
const MemoryScreen = lazy(screen(() => import("./screens/Memory").then((m) => ({ default: m.MemoryScreen }))));
const TerminalsScreen = lazy(screen(() => import("./screens/Terminals").then((m) => ({ default: m.TerminalsScreen }))));
const TerminalFullScreen = lazy(screen(() => import("./screens/TerminalFull").then((m) => ({ default: m.TerminalFullScreen }))));
const ServicesScreen = lazy(screen(() => import("./screens/Services").then((m) => ({ default: m.ServicesScreen }))));
const LoginScreen = lazy(screen(() => import("./screens/Login").then((m) => ({ default: m.LoginScreen }))));
const ProjectScreen = lazy(screen(() => import("./project/ProjectScreen").then((m) => ({ default: m.ProjectScreen }))));
const ProjectSidebar = lazy(screen(() => import("./project/ProjectSidebar").then((m) => ({ default: m.ProjectSidebar }))));
const MainScreen = lazy(screen(() => import("./main/MainScreen").then((m) => ({ default: m.MainScreen }))));
const OnboardingScreen = lazy(screen(() => import("./screens/AddModel").then((m) => ({ default: m.OnboardingScreen }))));

/** The conversation is what the operator opens next, whatever screen they landed on: fetch it while the browser is idle. */
function prefetchSession(): void {
  const idle = (window as { requestIdleCallback?: (cb: () => void) => void }).requestIdleCallback;
  const pull = () => void import("./screens/Session").catch(() => undefined);  // a prefetch that fails is not an error: the screen retries when it is opened
  if (idle) idle(pull);
  else window.setTimeout(pull, 2000);
}

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (this.state.error) {
      // A chunk that will not load after three tries is not a transient link: the page is running a
      // build whose files are no longer on the server. Clearing the error would replay the same
      // failure — what recovers it is fetching the page again.
      const stale = isChunkError(this.state.error.message);
      return (
        <div className="empty">
          <b>{t(stale ? "app.stale.title" : "app.broken.title")}</b>
          <div>{stale ? t("app.stale.body") : this.state.error.message}</div>
          <button className="btn" onClick={() => (stale ? location.reload() : this.setState({ error: null }))}>
            {t(stale ? "app.stale.action" : "common.retry")}
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

export function App() {
  useLang();
  const route = useRoute();
  const wide = useWide();
  const [picking, setPicking] = useState(false);
  const [more, setMore] = useState(false);
  const [palette, setPalette] = useState(false);
  // Which project the operator is looking at ("" is all of them). A lens over every list of agents
  // rather than a destination, so it lives in the shell and not in the route.
  const [project, setProject] = useState(storedProject);
  const [switching, setSwitching] = useState(false);
  const pickProject = useCallback((id: string) => {
    rememberProject(id);
    setProject(id);
  }, []);
  // The sidebar: a column of sessions, or a strip. Beside the Agents screen — which is the same list,
  // whole — it is always the strip, so the list is never drawn twice.
  const [sidebarCollapsed, toggleSidebar] = useSidebar(readSidebar, rememberSidebar);
  const [sidebarWidth, setSidebarWidth] = usePaneWidth("sidebar", 272, 232, 360);
  const [menu, setMenu] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const openPalette = useCallback(() => setPalette(true), []);
  const offline = useOffline();
  useEffect(prefetchSession, []);
  // Inside Telegram every request carries initData; outside, the browser needs a token or the session cookie.
  const [authed, setAuthed] = useState<boolean | null>(() => (telegram()?.initData ? true : null));
  // Nothing in the app works without a model, so the app asks for one before it shows anything else.
  const [onboarding, setOnboarding] = useState<OnboardingState | null>(null);
  // What this window shows goes to the host from the moment it may ask anything at all.
  useEffect(() => (authed ? startPresence() : undefined), [authed]);
  // The host's events drive the badge and the lists from here on; the polls below are the net under it.
  useEffect(() => (authed ? startEvents() : undefined), [authed]);
  // A tap on a pushed notification while the app is open moves the app instead of opening another;
  // and a device the host forgot is registered again, since the browser still thinks it is.
  useEffect(() => listenForOpen((path) => navigate(path)), []);
  useEffect(() => {
    if (authed) void syncPush();
  }, [authed]);
  useEffect(() => {
    if (!authed) return;
    api
      .get<OnboardingState>("/api/onboarding")
      .then(setOnboarding)
      .catch(() => setOnboarding({ has_model: true } as OnboardingState)); // an older bot has no such route: let the app through
  }, [authed]);
  const notifications = useSummary(!!authed);
  useAppBadge(notifications.unseen);
  const projects = useProjects();
  const projectList = projects.data ?? [];
  // A project removed elsewhere must not leave the shell filtering by something that is gone.
  useEffect(() => {
    if (project && projects.data && !projects.data.some((p) => p.id === project)) pickProject("");
  }, [project, projects.data, pickProject]);
  // What this installation can do decides what the app offers. Until the answer arrives the nav is the
  // one a server install has: hiding a destination and putting it back a moment later reads as a glitch.
  // A minute rather than five: the mode never changes, but whether a change of the agent's own is
  // waiting for a restart does, and that is a banner the operator should not have to reload to see.
  const caps = useQuery<Capabilities>(authed ? "/api/capabilities" : null, { pollMs: 60000, staleMs: 20000 });
  // The answer is read defensively: a bot too old to have the route, or one that answers something
  // this app does not recognise, must not take the whole shell down over a nav label.
  const selfdev: SelfDevMode = caps.data?.selfdev?.mode ?? "server";
  const proposals = useQuery<{ status: string }[]>(authed && selfdev !== "off" ? "/api/proposals" : null, { pollMs: 60000, staleMs: 30000 });
  const counts: Counts = { inbox: notifications.unseen, changes: (proposals.data ?? []).filter((p) => p.status === "pending").length };
  useShortcuts(openPalette, selfdev);
  // Two more on a desktop: the menu and the sidebar, both with a modifier so a text field never eats them.
  useEffect(() => {
    if (!wide) return;
    const onKey = (e: KeyboardEvent) => {
      // Ctrl+\ quits a program in a terminal; the sidebar waits until the terminal loses focus.
      if (insideTerminal(e.target)) return;
      const which = shortcutFor(e);
      if (!which) return;
      e.preventDefault();
      if (which === "menu") setMenu((m) => !m);
      else toggleSidebar();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [wide, toggleSidebar]);

  useEffect(() => {
    if (authed !== null) return;
    api
      .get("/api/auth/me")
      .then(() => setAuthed(true))
      .catch(() => setAuthed(false));
  }, [authed]);

  // The shell is exactly as tall as the browser really shows. dvh is a unit the WebView computes from
  // its own idea of the viewport, which on Android (Telegram's WebView, a soft keyboard, a collapsing
  // URL bar) is taller than the visible area: the page then gets a scroll of its own and the header or
  // the composer is pushed out of sight until the reader drags the whole page back.
  useEffect(() => {
    const vv = window.visualViewport;
    const apply = () => {
      const height = vv ? vv.height : window.innerHeight;
      if (height > 0) document.documentElement.style.setProperty("--vh", `${Math.round(height)}px`);
      // The keyboard on iOS scrolls the page instead of resizing it; put it back.
      if (window.scrollY !== 0) window.scrollTo(0, 0);
    };
    apply();
    vv?.addEventListener("resize", apply);
    vv?.addEventListener("scroll", apply);
    window.addEventListener("resize", apply);
    window.addEventListener("orientationchange", apply);
    return () => {
      vv?.removeEventListener("resize", apply);
      vv?.removeEventListener("scroll", apply);
      window.removeEventListener("resize", apply);
      window.removeEventListener("orientationchange", apply);
    };
  }, []);

  useEffect(() => {
    const tg = telegram();
    migrateLegacyLocation(tg?.initDataUnsafe?.start_param);
    if (!tg?.initData) {
      // Outside Telegram the system decides, unless the reader picked a scheme (?scheme=dark sticks).
      const wanted = new URLSearchParams(window.location.search).get("scheme");
      try {
        if (wanted === "dark" || wanted === "light") localStorage.setItem("daedalus.scheme", wanted);
        else if (wanted === "auto") localStorage.removeItem("daedalus.scheme");
      } catch {
        /* private mode */
      }
      const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
      const apply = () => {
        let forced: string | null = null;
        try {
          forced = localStorage.getItem("daedalus.scheme");
        } catch {
          /* private mode */
        }
        document.documentElement.dataset.scheme = forced === "dark" || forced === "light" ? forced : mq?.matches ? "dark" : "light";
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

  // A project's focus mode: entering a project's route swaps the sessions column for the project's own
  // and puts the project in the centre. The decision is made here and nowhere else, so every other
  // screen keeps the shell it always had.
  const focusProject = route.screen === "project" ? route.project : null;
  // The main orchestrator's chat is a conversation like any other: on a phone it takes the screen.
  const mainChat = route.screen === "main";
  const focusChat = (!!focusProject && (route.page === null || route.page === "s")) || mainChat;
  // Telegram's own back button leaves a detail; the vertical swipe must not close the app mid-chat.
  const inDetail = !!route.session || !!route.detail || !!focusProject || mainChat;
  useEffect(() => {
    const tg = telegram();
    if (!tg?.initData || !tg.BackButton) return;
    if (!inDetail) {
      tg.BackButton.hide();
      tg.enableVerticalSwipes?.();
      return;
    }
    const onBack = () => back(focusProject || mainChat ? pathFor("agents") : pathFor(route.screen));
    tg.BackButton.onClick(onBack);
    tg.BackButton.show();
    tg.disableVerticalSwipes?.();
    return () => tg.BackButton?.offClick(onBack);
  }, [inDetail, route.screen, focusProject, mainChat]);

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
    setMenu(false);
  }, [route.screen, route.session]);

  // Stable, so the Agents list can skip a folder that did not change between two polls: a new
  // function on every render of the shell would defeat every memo below it.
  const open = useCallback((id: string) => navigate(sessionPath(id)), []);
  const closeSession = () => back(pathFor("agents"));
  const paletteItems = (): PaletteItem[] => {
    const sessions = peek<SessionList>("/api/sessions")?.sessions ?? [];
    // The terminals as the last listing left them: the Terminals screen's (with previews) or the
    // full view's. Nothing is fetched for the palette; a terminal opened a second ago may be missing.
    const listing = peek<TerminalList>("/api/terminals?preview=6") ?? peek<TerminalList>("/api/terminals");
    const termEnvs = listing?.envs ?? [];
    const envUp = (name: string) => termEnvs.some((e) => e.env === name && e.available);
    const lens = projectList.find((p) => p.id === project) ?? null;
    const newTerminal = (env: "container" | "host") => async () => {
      const { containerFolder, openFreeTerminal } = await import("./screens/Terminals");
      const place = env === "container" ? containerFolder(lens, sessions) : null;
      const home = termEnvs.find((e) => e.env === "host")?.home;
      await openFreeTerminal(env, env === "container" ? place?.path : home || undefined, place?.projectId ?? null, showToast);
    };
    const terminalItems: PaletteItem[] = [
      ...(listing?.terminals ?? []).filter((r) => r.status === "running").map((r) => ({ id: `t-${r.id}`, label: t("term.palette.open", { title: r.title }), hint: r.cwd, icon: "terminal" as const, run: () => navigate(pathFor("terminals", r.id)) })),
      // Before the first listing nothing is known about the environments: offer the container one,
      // which is the one an installation has; the host one only once it is known to answer.
      ...(!listing || envUp("container") ? [{ id: "new-term-container", label: t("term.palette.container"), icon: "terminal" as const, run: () => void newTerminal("container")() }] : []),
      ...(envUp("host") ? [{ id: "new-term-host", label: t("term.palette.host"), icon: "lock" as const, run: () => void newTerminal("host")() }] : []),
    ];
    return [
      { id: "new-agent", label: t("shell.search.newagent"), icon: "plus", run: () => navigate(pathFor("agents", null, { new: "1" })) },
      { id: "projects", label: t("shell.projects"), hint: projectList.find((p) => p.id === project)?.name ?? t("shell.projects.all"), icon: "folder", run: () => setSwitching(true) },
      ...projectList.map((p) => ({ id: `p-${p.id}`, label: t("shell.search.workin", { name: p.name }), hint: projectPath(p), icon: "folder" as const, run: () => pickProject(p.id) })),
      ...projectList.filter((p) => !p.system && !p.settings.ephemeral).map((p) => ({ id: `open-${p.id}`, label: t("focus.palette", { name: p.name }), icon: "conductor" as const, run: () => navigate(projectHome(p.id)) })),
      ...projectList.filter((p) => !p.system && !p.settings.ephemeral).map((p) => ({ id: `team-${p.id}`, label: t("shell.search.team", { name: p.name }), icon: "bots" as const, run: () => navigate(projectPagePath(p.id, "team")) })),
      ...projectList.filter((p) => !p.system && !p.settings.ephemeral).map((p) => ({ id: `board-${p.id}`, label: t("shell.search.board", { name: p.name }), icon: "board" as const, run: () => navigate(projectPagePath(p.id, "board")) })),
      ...visibleScreens(SCREENS, selfdev).map((s) => ({ id: `go-${s}`, label: t("shell.search.goto", { name: screenTitle(s) }), icon: "back" as const, run: () => navigate(pathFor(s)) })),
      ...sessions.map((s) => ({ id: `s-${s.id}`, label: s.title, hint: s.model ?? "", icon: "bots" as const, run: () => open(s.id) })),
      ...terminalItems,
    ];
  };

  if (authed === null) return <div className="app"><div className="empty">{t("common.loading")}</div></div>;
  if (authed === false) {
    // Outside the shell on purpose: the shell's wide layout reserves the rail's column, and a login page has no rail.
    return (
      <Suspense fallback={<div className="gate"><div className="empty">{t("common.loading")}</div></div>}>
        <LoginScreen onDone={() => setAuthed(true)} />
      </Suspense>
    );
  }
  if (onboarding === null) return <div className="app"><div className="empty">{t("common.loading")}</div></div>;
  if (!onboarding.has_model) {
    // Outside the shell, like the login page: the wide shell is a grid whose first column belongs
    // to the rail, and a screen drawn in it without one sits beside the rail's width of nothing.
    return (
      <>
        <Suspense fallback={<div className="gate"><div className="empty">{t("common.loading")}</div></div>}>
          <OnboardingScreen toast={showToast} onDone={() => setOnboarding({ ...onboarding, has_model: true })} />
        </Suspense>
        <ToastHost />
        <ConfirmHost />
      </>
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
        {route.screen === "agents" && <StartScreen onOpen={open} toast={showToast} project={project} projects={projectList} onProjects={wide ? undefined : () => setSwitching(true)} />}
        {route.screen === "voice" && <VoiceScreen onOpen={open} toast={showToast} />}
        {route.screen === "inbox" && <InboxScreen onOpen={open} toast={showToast} />}
        {route.screen === "board" && <BoardScreen onOpen={open} toast={showToast} selected={route.detail} project={projectList.find((p) => p.id === project) ?? null} />}
        {route.screen === "changes" &&
          (selfdev === "off" ? (
            <div className="empty">
              <b>{t("app.selfdev.off.title")}</b>
              <div>{t("app.selfdev.off.body")}</div>
            </div>
          ) : (
            <ProposalsScreen toast={showToast} selected={route.detail} />
          ))}
        {route.screen === "schedules" && <SchedulesScreen toast={showToast} onOpen={open} selected={route.detail} />}
        {route.screen === "terminals" && !route.detail && <TerminalsScreen toast={showToast} project={project} projects={projectList} />}
        {route.screen === "terminals" && route.detail && <TerminalFullScreen id={route.detail} beside={route.query.get("with")} toast={showToast} />}
        {route.screen === "services" && <ServicesScreen onOpen={open} toast={showToast} />}
        {route.screen === "memory" && <MemoryScreen toast={showToast} onOpen={open} />}
        {route.screen === "usage" && <UsageScreen onOpen={open} />}
        {route.screen === "health" && <HealthScreen toast={showToast} />}
        {route.screen === "settings" && <SettingsScreen toast={showToast} section={route.detail} />}
        {route.screen === "main" && <MainScreen toast={showToast} />}
        {route.screen === "project" &&
          (!route.project ? (
            <div className="empty"><b>{t("team.noproject")}</b></div>
          ) : (
            <ProjectScreen projectId={route.project} page={route.page} inner={route.inner} toast={showToast} wide={wide} />
          ))}
      </ErrorBoundary>
    );
  }

  const strip = sidebarCollapsed;
  // The main orchestrator is pinned above every project, in both columns, folded or not.
  const pinned = <MainEntry current={mainChat} strip={strip} />;
  // A terminal full screen takes the column the way a conversation does: no scrolling page around it
  // and, on a phone, no tab bar under it.
  const terminalOpen = route.screen === "terminals" && !!route.detail;
  const menuButtonEl = (
    <button ref={menuButton} className={`sidebar-menu ${strip ? "iconbtn quiet" : ""}`} onClick={() => setMenu((m) => !m)} title={t("nav.menu.title")} aria-label={t("nav.menu")} aria-haspopup="menu" aria-expanded={menu}>
      <Icon name="more" size={18} />
      {!strip && <span className="sidebar-text">{t("nav.menu")}</span>}
      {!strip && <kbd>⌘⇧M</kbd>}
      {(counts.inbox ?? 0) + (counts.changes ?? 0) > 0 && <span className="badge-dot" aria-hidden />}
    </button>
  );
  return (
    <div className="app" style={wide ? { ["--sidebar-w" as string]: `${strip ? 48 : sidebarWidth}px` } : undefined}>
      {wide && focusProject && (
        <ErrorBoundary key={focusProject}>
        <Suspense fallback={<nav className="sidebar project-sidebar" />}>
          <ProjectSidebar
            projectId={focusProject}
            view={focusView(route.page, route.inner)}
            terminal={route.page === "terminals" ? route.query.get("t") : null}
            collapsed={strip}
            onToggle={toggleSidebar}
            width={sidebarWidth}
            onWidth={setSidebarWidth}
            menu={menuButtonEl}
            toast={showToast}
            pinned={pinned}
          />
        </Suspense>
        </ErrorBoundary>
      )}
      {wide && !focusProject && (
        <Sidebar
          screen={route.screen}
          session={sessionId}
          counts={counts}
          selfdev={selfdev}
          collapsed={strip}
          onToggle={toggleSidebar}
          width={sidebarWidth}
          onWidth={setSidebarWidth}
          onPalette={openPalette}
          projects={projectList}
          project={project}
          onProjects={() => setSwitching(true)}
          onOpen={open}
          toast={showToast}
          menuOpen={menu}
          onMenu={() => setMenu((m) => !m)}
          menuButton={menuButton}
          pinned={pinned}
        />
      )}
      {wide && menu && <NavMenu screen={route.screen} counts={counts} selfdev={selfdev} onClose={() => setMenu(false)} opener={menuButton.current} />}
      <div ref={main} className={`main ${sessionId || focusChat || terminalOpen ? "chat-open" : ""}`}>
        <MaintenanceNotice />
        {offline && <div className="offline-strip" role="status">{t("app.offline")}</div>}
        <ChangeStrip caps={caps} />
        <PasskeyNudge />
        <Suspense fallback={<div className="empty">{t("common.loading")}</div>}>{content}</Suspense>
      </div>
      {palette && <Palette items={paletteItems()} onClose={() => setPalette(false)} />}
      {switching && <ProjectSwitcher projects={projectList} current={project} onPick={pickProject} onClose={() => setSwitching(false)} toast={showToast} />}
      {!wide && !sessionId && !focusChat && !terminalOpen && <TabBar screen={route.screen} counts={counts} selfdev={selfdev} onMore={() => setMore((m) => !m)} moreOpen={more} />}
      {more && <MoreSheet screen={route.screen} counts={counts} selfdev={selfdev} onClose={() => setMore(false)} />}
      {picking && sessionId && <SessionPicker exclude={sessionId} onPick={(id) => { navigate(sessionPath(sessionId, id)); setPicking(false); }} onClose={() => setPicking(false)} />}
      <NotificationToasts />
      <ToastHost />
      <ConfirmHost />
    </div>
  );
}

/** The unseen count on the installed app's icon, where the platform draws one. */
function useAppBadge(unseen: number): void {
  useEffect(() => {
    const nav = navigator as Navigator & { setAppBadge?: (n?: number) => Promise<void>; clearAppBadge?: () => Promise<void> };
    // Refused outside an installed app on some platforms; a badge is a courtesy, never an error.
    const done = unseen > 0 ? nav.setAppBadge?.(unseen) : nav.clearAppBadge?.();
    done?.catch(() => undefined);
  }, [unseen]);
}

/** After a pairing link, the browser is signed in but holds nothing of its own: offer it a passkey, once. */
function PasskeyNudge() {
  const [show, setShow] = useState(false);
  useEffect(() => {
    if (telegram()?.initData || !passkeys.supported()) return;
    try {
      if (localStorage.getItem("daedalus.passkeyNudge") === "off") return;
    } catch {
      /* private mode */
    }
    api
      .get<AuthConfig>("/api/auth/config")
      .then((c) => setShow(c.passkeys === 0))
      .catch(() => setShow(false));
  }, []);
  if (!show) return null;
  const dismiss = () => {
    try {
      localStorage.setItem("daedalus.passkeyNudge", "off");
    } catch {
      /* private mode */
    }
    setShow(false);
  };
  return (
    <div className="nudge-strip" role="status">
      <span>{t("app.passkey.nudge")}</span>
      <a href={pathFor("settings", "security")} onClick={(e) => (go(e, pathFor("settings", "security")), dismiss())}>{t("app.passkey.add")}</a>
      <button className="linkbtn" onClick={dismiss}>{t("app.passkey.later")}</button>
    </div>
  );
}

/** Whether the layout is the wide one (rail beside the screen): the same breakpoint as the stylesheet. */
function useWide(): boolean {
  return useMedia("(min-width: 1024px)");
}

/** Which session to open beside the current one. */
function SessionPicker({ exclude, onPick, onClose }: { exclude: string | null; onPick: (id: string) => void; onClose: () => void }) {
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [filter, setFilter] = useState("");
  useEffect(() => {
    api.get<SessionList>("/api/sessions").then((listing) => setSessions(listing.sessions)).catch(() => setSessions([]));
  }, []);
  const q = filter.trim().toLowerCase();
  const items = (sessions ?? []).filter((s) => s.id !== exclude && (!q || s.title.toLowerCase().includes(q) || s.id.includes(q)));
  return (
    <Sheet title={t("app.beside.title")} onClose={onClose} size="narrow">
      <input className="field" autoFocus placeholder={t("app.beside.filter")} value={filter} onChange={(e) => setFilter(e.target.value)} style={{ marginBottom: 8 }} />
      {sessions === null && <div className="empty">{t("common.loading")}</div>}
      {sessions !== null && items.length === 0 && <div className="empty">{t("app.beside.empty")}</div>}
      {items.map((s) => (
        <button key={s.id} className="menu-item" onClick={() => onPick(s.id)}>
          <span className="grow truncate">{s.title}</span>
          <StatusLabel status={s.status} />
        </button>
      ))}
    </Sheet>
  );
}
