// The right panel's state, with nothing of the browser in it: which tab is open, what the Preview
// tab has walked through, whether the panel covers the chat. The route carries the part a link
// should reproduce (`?panel=preview&path=reports/menu.md`), the browser remembers the width and the
// last open tab, and the component in panel.tsx is the only thing that turns either into pixels.

export type PanelTab = "details" | "files" | "preview" | "jobs" | "board" | "brief" | "wakeups" | "folders";

/** A session's own tabs: what the agent is, the files it works on, one of them open, its jobs. */
export const PANEL_TABS: PanelTab[] = ["details", "files", "preview", "jobs"];

/** A project's tabs, beside a session in the project's focus mode. */
export const PROJECT_TABS: PanelTab[] = ["board", "brief", "wakeups", "folders"];

/** What a panel is beside: an ordinary session, a project's orchestrator, or a session inside a
 *  project's focus mode (a staff member, or anyone else working in the project). */
export type PanelContext = "session" | "orchestrator" | "member";

/**
 * The tabs a panel offers, in order. The orchestrator writes no file and runs no job, so its panel
 * is the project's alone; a session inside a project keeps its own tabs and gains the project's,
 * so the board is one click away from whoever is working on it.
 */
export function tabsFor(context: PanelContext): PanelTab[] {
  if (context === "orchestrator") return PROJECT_TABS;
  if (context === "member") return [...PANEL_TABS, ...PROJECT_TABS];
  return PANEL_TABS;
}

/** One file the Preview tab showed: where the bytes come from, and the lines an answer cited, if any. */
export type PanelEntry = { base: string; path: string; lines?: string };

export type PanelState = {
  /** The open tab, or null while the panel is closed. */
  tab: PanelTab | null;
  /** What Preview has shown, oldest first; `at` points at the one on screen. */
  stack: PanelEntry[];
  at: number;
  /** Whether the panel covers the chat column. Closing the panel drops it. */
  expanded: boolean;
};

export const PANEL_CLOSED: PanelState = { tab: null, stack: [], at: -1, expanded: false };

export function isPanelTab(v: string | null | undefined, tabs: readonly PanelTab[] = [...PANEL_TABS, ...PROJECT_TABS]): v is PanelTab {
  return !!v && (tabs as readonly string[]).includes(v);
}

export function openTab(s: PanelState, tab: PanelTab): PanelState {
  return s.tab === tab ? s : { ...s, tab };
}

export function closePanel(s: PanelState): PanelState {
  return s.tab === null ? s : { ...s, tab: null, expanded: false };
}

/** The toggle the shortcut and the header button share: closed → the last tab (Details by default), open → closed. */
export function togglePanel(s: PanelState, last: PanelTab | null = null, first: PanelTab = "details"): PanelState {
  return s.tab === null ? openTab(s, last ?? first) : closePanel(s);
}

export function toggleExpanded(s: PanelState): PanelState {
  return s.tab === null ? s : { ...s, expanded: !s.expanded };
}

/** A file opened into Preview: goes on top of the history, forgetting anything that was "forward". */
export function openFile(s: PanelState, entry: PanelEntry): PanelState {
  const current = s.stack[s.at];
  if (current && sameEntry(current, entry)) return { ...s, tab: "preview" };
  const stack = [...s.stack.slice(0, s.at + 1), entry];
  return { ...s, tab: "preview", stack, at: stack.length - 1 };
}

export function canGoBack(s: PanelState): boolean {
  return s.at > 0;
}

export function canGoForward(s: PanelState): boolean {
  return s.at >= 0 && s.at < s.stack.length - 1;
}

export function goBack(s: PanelState): PanelState {
  return canGoBack(s) ? { ...s, tab: "preview", at: s.at - 1 } : s;
}

export function goForward(s: PanelState): PanelState {
  return canGoForward(s) ? { ...s, tab: "preview", at: s.at + 1 } : s;
}

export function currentEntry(s: PanelState): PanelEntry | null {
  return s.stack[s.at] ?? null;
}

function sameEntry(a: PanelEntry, b: PanelEntry): boolean {
  return a.base === b.base && a.path === b.path && (a.lines ?? "") === (b.lines ?? "");
}

// ── the route ────────────────────────────────────────────────────────────────────────────────

/** What a link carries: the open tab and the previewed file. `tab=` is read as the tab too, so a
 *  link written as `?panel=files&tab=preview&path=…` opens Preview on that path. */
export function readPanelQuery(query: URLSearchParams, tabs: readonly PanelTab[] = PANEL_TABS): { tab: PanelTab; path: string | null; lines?: string } | null {
  const tab = query.get("tab");
  const panel = query.get("panel");
  const which = isPanelTab(tab, tabs) ? tab : isPanelTab(panel, tabs) ? panel : null;
  if (!which) return null;
  const path = query.get("path");
  const lines = query.get("lines") ?? "";
  return { tab: which, path: path || null, ...(/^\d+(?:-\d+)?$/.test(lines) ? { lines } : {}) };
}

/** The query parameters the state writes; null for the ones to drop. */
export function panelQuery(s: PanelState): Record<string, string | null> {
  const current = currentEntry(s);
  return { panel: s.tab, path: s.tab === "preview" && current ? current.path : null, lines: s.tab === "preview" ? current?.lines ?? null : null, tab: null };
}

/** The state a route describes, over what the pane already holds: the tab from the link, the file
 *  on top of the history when the link names one the pane is not already showing. */
export function applyPanelQuery(s: PanelState, q: { tab: PanelTab; path: string | null; lines?: string } | null, base: string): PanelState {
  if (!q) return closePanel(s);
  let next = openTab(s, q.tab);
  if (q.path && (q.tab === "preview" || !currentEntry(next))) {
    const current = currentEntry(next);
    if (!current || current.path !== q.path || current.lines !== q.lines) next = openFile(next, { base, path: q.path, ...(q.lines ? { lines: q.lines } : {}) });
    if (q.tab !== "preview") next = openTab(next, q.tab);
  }
  return next;
}

// ── the width ────────────────────────────────────────────────────────────────────────────────

export const PANEL_MIN_PX = 360;
export const PANEL_MAX_PCT = 65;
export const PANEL_DEFAULT_PCT = 42;

/** The share of the chat area the panel takes, kept between the pixel floor and the percentage ceiling.
 *  With no room for both (a narrow pane), the floor wins: a panel too narrow to read is no panel. */
export function clampPanelPct(pct: number, areaWidth: number): number {
  if (!Number.isFinite(pct)) return PANEL_DEFAULT_PCT;
  const floor = areaWidth > 0 ? (PANEL_MIN_PX / areaWidth) * 100 : 0;
  const lo = Math.min(floor, PANEL_MAX_PCT);
  return Math.round(Math.min(PANEL_MAX_PCT, Math.max(lo, pct)) * 10) / 10;
}

const WIDTH_KEY = "daedalus.width.panel";

export function readPanelPct(storage: Pick<Storage, "getItem"> | null = safeStorage()): number {
  try {
    const v = Number(storage?.getItem(WIDTH_KEY));
    return v > 0 && v <= PANEL_MAX_PCT ? v : PANEL_DEFAULT_PCT;
  } catch {
    return PANEL_DEFAULT_PCT;
  }
}

export function rememberPanelPct(pct: number, storage: Pick<Storage, "setItem"> | null = safeStorage()): void {
  try {
    storage?.setItem(WIDTH_KEY, String(pct));
  } catch {
    /* private mode: the width lasts for the visit */
  }
}

// ── the last open tab ────────────────────────────────────────────────────────────────────────

const TAB_KEY = "daedalus.session.panel";

/** Below this the panel is closed unless the route or the operator opens it. */
export const PANEL_OPEN_MIN = 1280;

/** Which tab a session opens on with nothing in the route: what was open last time, or Details on
 *  a window wide enough for both; "0" is the operator having closed it. */
export function defaultPanelTab(stored: string | null, viewportWidth: number, tabs: readonly PanelTab[] = PANEL_TABS): PanelTab | null {
  if (stored === "0") return null;
  if (isPanelTab(stored, tabs)) return viewportWidth >= PANEL_OPEN_MIN ? stored : null;
  return viewportWidth >= PANEL_OPEN_MIN ? tabs[0] : null;
}

/** Where the last open tab is kept. A project's focus mode keeps its own: the board it left open
 *  beside the orchestrator is not a tab an ordinary session has, and Details is not the
 *  orchestrator's. */
export function panelTabKey(context: PanelContext): string {
  return context === "session" ? TAB_KEY : `${TAB_KEY}.project`;
}

export function readPanelTab(viewportWidth: number, storage: Pick<Storage, "getItem"> | null = safeStorage(), context: PanelContext = "session"): PanelTab | null {
  let stored: string | null = null;
  try {
    stored = storage?.getItem(panelTabKey(context)) ?? null;
  } catch {
    /* private mode */
  }
  return defaultPanelTab(stored, viewportWidth, tabsFor(context));
}

export function rememberPanelTab(tab: PanelTab | null, storage: Pick<Storage, "setItem"> | null = safeStorage(), context: PanelContext = "session"): void {
  try {
    storage?.setItem(panelTabKey(context), tab ?? "0");
  } catch {
    /* private mode */
  }
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

// ── the keyboard ─────────────────────────────────────────────────────────────────────────────

/** `⌘.` / `Ctrl+.` toggles the panel; with Shift it covers the chat. Anything else is not ours. */
export function panelShortcut(e: Pick<KeyboardEvent, "key" | "code" | "metaKey" | "ctrlKey" | "altKey" | "shiftKey">): "toggle" | "expand" | null {
  if (!(e.metaKey || e.ctrlKey) || e.altKey) return null;
  if (e.key !== "." && e.code !== "Period") return null;
  return e.shiftKey ? "expand" : "toggle";
}

// ── the breadcrumb ───────────────────────────────────────────────────────────────────────────

/** The folders of a path, then the file: `reports/menu-check.md` → `["reports", "menu-check.md"]`. */
export function crumbsOf(path: string): string[] {
  return path.split("/").filter(Boolean);
}
