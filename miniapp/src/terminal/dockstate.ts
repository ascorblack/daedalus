// What the session dock remembers per session, and the rules for changing it. Pure, so the rules are
// tested without a browser; the component only renders the state and calls these.
//
// A tab is a view onto a terminal, not the terminal: closing a tab detaches and the terminal runs on,
// and a terminal the session still has comes back as a tab the next time the dock opens with none.

export type DockState = {
  open: boolean;
  /** The dock's height in px, clamped when it is applied (the window may have shrunk since). */
  height: number;
  tabs: string[];
  /** The tab in the left (or only) pane. */
  active: string | null;
  /** The tab in the right pane when the dock is split. */
  split: string | null;
};

export const DOCK_MIN = 120;
/** At most this share of the conversation's column; the conversation keeps the rest. */
export const DOCK_MAX_SHARE = 0.8;
export const DOCK_DEFAULT = 280;

export const EMPTY: DockState = { open: false, height: DOCK_DEFAULT, tabs: [], active: null, split: null };

const key = (sessionId: string) => `daedalus.dock.${sessionId}`;

function sane(raw: unknown): DockState {
  if (!raw || typeof raw !== "object") return EMPTY;
  const r = raw as Partial<DockState>;
  const tabs = Array.isArray(r.tabs) ? r.tabs.filter((x): x is string => typeof x === "string").slice(0, 32) : [];
  const unique = [...new Set(tabs)];
  const active = typeof r.active === "string" && unique.includes(r.active) ? r.active : unique[0] ?? null;
  const split = typeof r.split === "string" && unique.includes(r.split) && r.split !== active ? r.split : null;
  const height = typeof r.height === "number" && Number.isFinite(r.height) ? Math.max(DOCK_MIN, Math.round(r.height)) : DOCK_DEFAULT;
  return { open: !!r.open, height, tabs: unique, active, split };
}

export function loadDock(sessionId: string): DockState {
  try {
    const text = localStorage.getItem(key(sessionId));
    return text ? sane(JSON.parse(text)) : EMPTY;
  } catch {
    return EMPTY;
  }
}

export function saveDock(sessionId: string, state: DockState): void {
  try {
    if (!state.open && !state.tabs.length && state.height === DOCK_DEFAULT) localStorage.removeItem(key(sessionId));
    else localStorage.setItem(key(sessionId), JSON.stringify(state));
  } catch {
    /* remembered for this page only */
  }
}

/** The height the dock may have in a column this tall. */
export function clampHeight(height: number, column: number): number {
  const max = Math.max(DOCK_MIN, Math.floor(column * DOCK_MAX_SHARE));
  return Math.max(DOCK_MIN, Math.min(max, Math.round(height)));
}

/** Show a terminal in the dock: a tab for it (appended once) and the left pane. */
export function openTab(state: DockState, id: string): DockState {
  const tabs = state.tabs.includes(id) ? state.tabs : [...state.tabs, id];
  if (state.split === id) return { ...state, open: true, tabs };
  return { ...state, open: true, tabs, active: id };
}

/** Close a tab: the terminal keeps running. The neighbour takes its pane. */
export function closeTab(state: DockState, id: string): DockState {
  const at = state.tabs.indexOf(id);
  if (at < 0) return state;
  const tabs = state.tabs.filter((t) => t !== id);
  let active = state.active;
  let split = state.split;
  if (split === id) split = null;
  if (active === id) {
    // The split pane's terminal moves over rather than leaving an empty left pane beside it.
    if (split) {
      active = split;
      split = null;
    } else active = tabs[Math.min(at, tabs.length - 1)] ?? null;
  }
  return { ...state, tabs, active, split };
}

/** Put a terminal beside the active one, or close the split. `other` is what the right pane shows. */
export function setSplit(state: DockState, other: string | null): DockState {
  if (other === null || other === state.active) return { ...state, split: null };
  const tabs = state.tabs.includes(other) ? state.tabs : [...state.tabs, other];
  return { ...state, open: true, tabs, split: other };
}

/** The tab to put in the right pane when splitting: the next one that is not already shown, or none. */
export function splitCandidate(state: DockState): string | null {
  const at = state.active ? state.tabs.indexOf(state.active) : -1;
  const order = [...state.tabs.slice(at + 1), ...state.tabs.slice(0, Math.max(0, at))];
  return order.find((id) => id !== state.active) ?? null;
}

/**
 * Drop tabs whose terminal the host no longer lists (removed, or ended and pruned). A terminal that
 * exited stays listed, and keeps its tab with its exit banner until the operator removes it.
 */
export function prune(state: DockState, listed: string[]): DockState {
  const known = new Set(listed);
  let next = state;
  for (const id of state.tabs) if (!known.has(id)) next = closeTab(next, id);
  return next;
}

export type ToggleOutcome = { state: DockState; create: boolean };

/**
 * Ctrl+` and the header button. Opening a dock that has no tabs brings in the session's running
 * terminals; with none at all, the caller creates one (as an editor's terminal panel does).
 */
export function toggleDock(state: DockState, running: string[]): ToggleOutcome {
  if (state.open) return { state: { ...state, open: false }, create: false };
  if (state.tabs.length) return { state: { ...state, open: true }, create: false };
  if (!running.length) return { state: { ...state, open: true }, create: true };
  return { state: { ...state, open: true, tabs: [...running], active: running[0], split: null }, create: false };
}

/** A terminal was restarted under a new id: its tab (and pane) follow it. */
export function replaceTab(state: DockState, from: string, to: string): DockState {
  const tabs = state.tabs.map((t) => (t === from ? to : t));
  return { ...state, tabs: [...new Set(tabs)], active: state.active === from ? to : state.active, split: state.split === from ? to : state.split };
}

// ── the sandbox choice ──────────────────────────────────────────────────────────────────────

const SANDBOX_KEY = "daedalus.term.sandbox";

/** Whether new terminals open in the sandbox. One choice per device, not per session: it is how the
 * operator likes to work, and a session they open next should not quietly drop it. */
export function loadSandboxChoice(): boolean {
  try {
    return localStorage.getItem(SANDBOX_KEY) === "1";
  } catch {
    return false;
  }
}

export function saveSandboxChoice(on: boolean): void {
  try {
    if (on) localStorage.setItem(SANDBOX_KEY, "1");
    else localStorage.removeItem(SANDBOX_KEY);
  } catch {
    /* remembered for this page only */
  }
}

/** What an environment says about the sandbox: whether a terminal there can have it, and if not, why. */
export type SandboxOffer = { ok: boolean; reason: string };

export function sandboxOffer(env: { available: boolean; sandbox: string } | undefined): SandboxOffer {
  if (!env || !env.available) return { ok: false, reason: "" };
  return env.sandbox === "ok" ? { ok: true, reason: "" } : { ok: false, reason: env.sandbox };
}

/** The toggle is offered when any available environment can sandbox; otherwise it shows the first reason. */
export function sandboxToggle(envs: { available: boolean; sandbox: string }[]): SandboxOffer {
  const offers = envs.map(sandboxOffer);
  if (offers.some((o) => o.ok)) return { ok: true, reason: "" };
  return { ok: false, reason: offers.find((o) => o.reason)?.reason ?? "" };
}
