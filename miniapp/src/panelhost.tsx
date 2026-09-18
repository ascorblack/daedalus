// The right panel: the session's workspace beside the conversation. Four tabs — Details, Files,
// Preview, Jobs — a toolbar of its own, a drag handle on its left edge, expand-to-full and close.
// On a desktop it is a column of the chat grid; on a phone the same tabs in a full-height sheet.
// The state is a value (panel.ts); this file draws it and wires the pointer and the keys.

import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Sheet, useLayer } from "./dialogs";
import { Icon } from "./icons";
import {
  PANEL_CLOSED,
  PANEL_TABS,
  PanelEntry,
  PanelState,
  PanelTab,
  applyPanelQuery,
  canGoBack,
  canGoForward,
  clampPanelPct,
  closePanel,
  crumbsOf,
  currentEntry,
  goBack,
  goForward,
  openFile,
  openTab,
  panelQuery,
  readPanelPct,
  readPanelQuery,
  readPanelTab,
  rememberPanelPct,
  rememberPanelTab,
  toggleExpanded,
  togglePanel,
} from "./panel";
import { navigate, pathFor } from "./router";
import { PreviewSource, Viewer, ViewerInfo } from "./preview";
import { HtmlNavigation } from "./htmlpreview";
import { SPLIT_MIN, TREE_W, TREE_W_MIN, TREE_W_MAX } from "./explorer";
import { usePaneWidth } from "./layout";
import { PaneHandle } from "./layout";
import { t } from "./i18n";

export type PanelHostProps = {
  state: PanelState;
  onTab: (tab: PanelTab) => void;
  onClose: () => void;
  onExpand: () => void;
  onBack: () => void;
  onForward: () => void;
  /** The root crumb: the project's name, or "workspace". */
  root: string;
  /** Where the current file downloads from, for open-in-new. */
  downloadUrl: (entry: PanelEntry) => string;
  /** The Details, Files and Jobs tabs, rendered by the screen that owns their data. */
  details: ReactNode;
  files: ReactNode;
  jobs: ReactNode;
  /** A number on a tab: subagents working on Details, jobs on Jobs. */
  badges?: Partial<Record<PanelTab, number>>;
  /** Phones: the tabs in a full sheet instead of a column. */
  sheet?: boolean;
  /** Desktop: drag the left edge; `dx` is in pixels. */
  onDrag?: (dx: number) => void;
};

/** What the panel keeps for itself: a reload counter for the viewer, the phone-width toggle,
 *  viewer info, HTML navigation, the closing state, and its id. The tab and body prefix uses that
 *  same id — two panels are open at once on a wide screen, and an id that is not this panel's own
 *  would point a reader's screen reader at the other one. */
type Local = { gen: number; reload: () => void; narrow: boolean; toggleNarrow: () => void; info: ViewerInfo | null; setInfo: (info: ViewerInfo) => void; nav: HtmlNavigation | null; setNav: (nav: HtmlNavigation | null) => void; id: string; ids: string; closing: boolean };
type HostProps = PanelHostProps & { local: Local };

export function Panel(props: PanelHostProps) {
  const { state, sheet } = props;
  const [last, setLast] = useState(state);
  useEffect(() => {
    if (state.tab !== null) { setLast(state); return; }
    const delay = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 180;
    const timer = window.setTimeout(() => setLast(state), delay);
    return () => window.clearTimeout(timer);
  }, [state]);
  const shown = state.tab === null ? last : state;
  const shownProps = { ...props, state: shown };
  const [gen, setGen] = useState(0);
  const [narrow, setNarrow] = useState(false);
  const [info, setInfo] = useState<ViewerInfo | null>(null);
  const [nav, setNav] = useState<HtmlNavigation | null>(null);
  const id = useId();
  const local: Local = { closing: state.tab === null, info, setInfo, nav, setNav, id, ids: id, gen, reload: () => setGen((g) => g + 1), narrow, toggleNarrow: () => setNarrow((n) => !n) };
  if (shown.tab === null) return null;
  if (sheet) {
    return (
      <Sheet size="full" className={`panel-sheet ${local.closing ? "panel-closing" : ""}`} ariaLabel={t("panel.label")} onClose={props.onClose} head={<Tabs {...shownProps} local={local} inSheet />}>
        <Toolbar {...shownProps} local={local} />
        <Body {...shownProps} local={local} />
      </Sheet>
    );
  }
  return <Column {...shownProps} local={local} />;
}

/** The desktop host: a column with the handle, a layer for Escape (expanded → restore, then close). */
function Column(props: HostProps) {
  const { state } = props;
  const box = useRef<HTMLElement>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setVisible(true));
    return () => cancelAnimationFrame(raf);
  }, []);
  // Escape reaches the panel only when nothing above it is open (a sheet, a menu) and the reader is
  // not in a text field elsewhere — the composer's own Escape (clearing a slash command) stays its own.
  useLayer(() => {
    const active = document.activeElement as HTMLElement | null;
    const typing = !!active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.isContentEditable);
    if (typing && !box.current?.contains(active)) return;
    if (state.expanded) props.onExpand();
    else props.onClose();
  });
  return (
    <aside ref={box} inert={props.local.closing} className={`panel ${props.local.closing ? "panel-closing" : ""} ${state.expanded ? "full" : ""} ${visible ? "shown" : ""} ${props.local.narrow ? "phone-width" : ""}`} aria-label={t("panel.label")}>
      {props.onDrag && !state.expanded && <PaneHandle side="right" onDrag={props.onDrag} />}
      <Tabs {...props} />
      <Toolbar {...props} />
      <Body {...props} />
    </aside>
  );
}

const TAB_ICON: Record<PanelTab, "settings" | "folder" | "eye" | "terminal"> = { details: "settings", files: "folder", preview: "eye", jobs: "terminal" };

function Tabs({ state, onTab, onClose, onExpand, badges, inSheet, local }: HostProps & { inSheet?: boolean }) {
  const strip = useRef<HTMLDivElement>(null);
  const onKey = (e: React.KeyboardEvent) => {
    const i = PANEL_TABS.indexOf(state.tab!);
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      const next = PANEL_TABS[(i + (e.key === "ArrowRight" ? 1 : PANEL_TABS.length - 1)) % PANEL_TABS.length];
      onTab(next);
      strip.current?.querySelector<HTMLElement>(`[data-tab="${next}"]`)?.focus();
      e.preventDefault();
    }
  };
  return (
    <div className="panel-tabs">
      <div ref={strip} className="panel-tablist" role="tablist" aria-label={t("panel.label")} onKeyDown={onKey}>
        {PANEL_TABS.map((tab) => {
          const n = badges?.[tab] ?? 0;
          return (
            <button key={tab} role="tab" id={`${local.ids}-tab-${tab}`} aria-controls={`${local.ids}-body`} data-tab={tab} className={`panel-tab ${state.tab === tab ? "on" : ""}`} aria-selected={state.tab === tab} tabIndex={state.tab === tab ? 0 : -1} onClick={() => onTab(tab)}>
              <Icon name={TAB_ICON[tab]} size={14} />
              <span>{t(`panel.tab.${tab}`)}</span>
              {n > 0 && <span className="count">{n}</span>}
            </button>
          );
        })}
      </div>
      {!inSheet && (
        <div className="panel-actions">
          <button className={`iconbtn small ${state.expanded ? "on" : ""}`} onClick={onExpand} aria-label={t(state.expanded ? "panel.restore" : "panel.expand")} title={t(state.expanded ? "panel.restore" : "panel.expand")} aria-pressed={state.expanded}>
            <Icon name={state.expanded ? "panel" : "expand"} size={16} />
          </button>
          <button className="iconbtn small" onClick={onClose} aria-label={t("panel.close")} title={t("panel.close")}>
            <Icon name="close" size={16} />
          </button>
        </div>
      )}
    </div>
  );
}

/** The Preview tab's toolbar: history, the breadcrumb, open-in-new, phone width, download. The
 *  other tabs carry their own controls in their bodies and draw no toolbar. */
function Toolbar({ state, onBack, onForward, onTab, root, downloadUrl, sheet, local }: HostProps) {
  const entry = currentEntry(state);
  if (state.tab !== "preview") return null;
  const html = local.info?.kind === "html" ? local.nav : null;
  const crumbs = entry ? crumbsOf(html?.path ?? entry.path) : [];
  return (
    <div className="panel-toolbar">
      <button className="iconbtn small" onClick={html ? html.back ?? undefined : onBack} disabled={html ? !html.back : !canGoBack(state)} aria-label={t("panel.back")} title={t("panel.back")}><Icon name="back" size={16} /></button>
      <button className="iconbtn small" onClick={html ? html.forward ?? undefined : onForward} disabled={html ? !html.forward : !canGoForward(state)} aria-label={t("panel.forward")} title={t("panel.forward")}><Icon name="forward" size={16} /></button>
      <button className="iconbtn small" onClick={html ? html.reload : local.reload} aria-label={t("panel.reload")} title={t("panel.reload")}><Icon name="reload" size={16} /></button>
      <div className="panel-crumbs" aria-label={t("panel.crumbs")}>
        <button className="crumb" onClick={() => onTab("files")} title={t("panel.tab.files")}>{root}</button>
        {crumbs.map((c, i) => (
          <span key={i} className={i === crumbs.length - 1 ? "crumb last" : "crumb"}>
            <span className="sep">›</span>
            {c}
          </span>
        ))}
        {!entry && <span className="sub">{t("panel.preview.none")}</span>}
      </div>
      {entry && (
        <a className="iconbtn small" href={html ? downloadUrl({ ...entry, path: html.path.split("#")[0] }) : downloadUrl(entry)} target="_blank" rel="noreferrer" aria-label={t("panel.opennew")} title={t("panel.opennew")}><Icon name="external" size={16} /></a>
      )}
      {entry && <a className="iconbtn small" href={downloadUrl(html ? { ...entry, path: html.path.split("#")[0] } : entry)} download={entry.path.split("/").pop()} aria-label={t("common.download")} title={t("common.download")}><Icon name="download" size={16} /></a>}
      {!sheet && (
        <button className={`iconbtn small ${local.narrow ? "on" : ""}`} aria-pressed={local.narrow} onClick={local.toggleNarrow} aria-label={t("panel.phonewidth")} title={t("panel.phonewidth")}><Icon name="phone" size={16} /></button>
      )}
    </div>
  );
}

function Body(props: HostProps) {
  const { state, local } = props;
  const entry = currentEntry(state);
  const box = useRef<HTMLDivElement>(null);
  const [wide, setWide] = useState(false);
  const [width, setWidth] = usePaneWidth("tree", TREE_W, TREE_W_MIN, TREE_W_MAX);
  useEffect(() => {
    if (!box.current) return;
    const observer = new ResizeObserver(([e]) => setWide(e.contentRect.width >= SPLIT_MIN));
    observer.observe(box.current);
    return () => observer.disconnect();
  }, []);
  const [visited, setVisited] = useState(state.tab === "files");
  const split = wide && state.tab === "preview";
  useEffect(() => { if (state.tab === "files" || split) setVisited(true); }, [state.tab, split]);
  const progress = local.nav?.busy ? null : local.info?.progress;
  const loading = state.tab === "preview" && (local.info?.loading || local.nav?.busy);
  return (
    <div ref={box} id={`${local.ids}-body`} role="tabpanel" aria-labelledby={`${local.ids}-tab-${state.tab}`} tabIndex={-1} className={`panel-body tab-${state.tab} ${split ? "split" : ""}`}>
      {loading && <div className={`preview-progress ${progress == null ? "busy" : ""}`} role="progressbar" aria-label={t("common.loading")} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress == null ? undefined : Math.round(progress * 100)}><i style={progress == null ? undefined : { width: `${progress * 100}%` }} /></div>}
      {state.tab === "details" && props.details}
      <div className="panel-files" hidden={state.tab !== "files" && !split} style={split ? { width } : undefined}>{(visited || state.tab === "files" || split) && props.files}{split && <PaneHandle side="left" onDrag={(dx) => setWidth(width + dx)} />}</div>
      {state.tab === "preview" && (entry ? <Viewer key={`${entry.base}:${entry.path}:${entry.lines ?? ""}:${local.gen}`} src={entry as PreviewSource} onInfo={local.setInfo} onNavigation={local.setNav} className="panel-viewer" /> : <div className="empty">{t("panel.preview.empty")}</div>)}
      {state.tab === "jobs" && props.jobs}
    </div>
  );
}

// ── the state, held by the screen ────────────────────────────────────────────────────────────

export type PanelControls = {
  state: PanelState;
  open: (tab: PanelTab) => void;
  close: () => void;
  toggle: () => void;
  expand: () => void;
  back: () => void;
  forward: () => void;
  openFile: (entry: PanelEntry) => void;
};

/** A pane's panel. With `route`, the URL carries the open tab and the previewed path, so a link
 *  reproduces the view and the browser's Back closes what was opened; the second pane of a dual
 *  view keeps its panel to itself. Below 1920 px only one pane's panel is open at a time. */
export function usePanel(sessionId: string, base: string, opts: { route: URLSearchParams | null; beside?: string | null; pane?: "left" | "right" }): PanelControls {
  const { route, beside, pane } = opts;
  const [state, setState] = useState<PanelState>(() => {
    const fromRoute = route ? readPanelQuery(route) : null;
    if (fromRoute) return applyPanelQuery(PANEL_CLOSED, fromRoute, base);
    // The second pane keeps its panel closed until asked while the window has room for one only.
    if (pane === "right" && window.innerWidth < DUAL_BOTH_MIN) return PANEL_CLOSED;
    const tab = readPanelTab(window.innerWidth);
    return tab ? openTab(PANEL_CLOSED, tab) : PANEL_CLOSED;
  });
  const last = useRef<PanelTab | null>(state.tab);
  if (state.tab) last.current = state.tab;
  // What this pane last wrote into the URL, so a route change it caused is not applied back to it.
  const written = useRef<string>(route ? routeKey(route, base) : "");
  const commit = useCallback(
    (next: PanelState, push = false) => {
      setState(next);
      rememberPanelTab(next.tab);
      if (!route) return;
      const q = panelQuery(next);
      written.current = queryString(q);
      navigate(pathFor("agents", sessionId, { with: beside ?? undefined, ...q }), { replace: !push });
    },
    [route, sessionId, beside],
  );
  // The route moved under the pane (Back, a link, the other pane): follow it.
  useEffect(() => {
    if (!route) return;
    const q = readPanelQuery(route);
    const incoming = routeKey(route, base);
    if (incoming === written.current) return;
    written.current = incoming;
    setState((s) => {
      const next = applyPanelQuery(s, q, base);
      rememberPanelTab(next.tab);
      return next;
    });
  }, [route, base]);
  // One panel at a time below 1920: opening this pane's closes the other's.
  useEffect(() => {
    if (!pane) return;
    const on = (e: Event) => {
      if ((e as CustomEvent<string>).detail !== pane && window.innerWidth < DUAL_BOTH_MIN) setState((s) => closePanel(s));
    };
    window.addEventListener(PANEL_EVENT, on);
    return () => window.removeEventListener(PANEL_EVENT, on);
  }, [pane]);
  const announce = useCallback(() => {
    if (pane) window.dispatchEvent(new CustomEvent(PANEL_EVENT, { detail: pane }));
  }, [pane]);
  const stateRef = useRef(state);
  stateRef.current = state;
  const open = useCallback(
    (tab: PanelTab) => {
      const s = stateRef.current;
      if (s.tab === null) announce();
      commit(openTab(s, tab), s.tab === null);
    },
    [commit, announce],
  );
  return {
    state,
    open,
    close: useCallback(() => commit(closePanel(stateRef.current)), [commit]),
    toggle: useCallback(() => {
      const s = stateRef.current;
      if (s.tab === null) announce();
      commit(togglePanel(s, last.current), s.tab === null);
    }, [commit, announce]),
    expand: useCallback(() => {
      const s = stateRef.current;
      if (s.tab !== null) {
        commit(toggleExpanded(s));
        return;
      }
      announce();
      commit({ ...openTab(s, last.current ?? "details"), expanded: true }, true);
    }, [commit, announce]),
    back: useCallback(() => commit(goBack(stateRef.current)), [commit]),
    forward: useCallback(() => commit(goForward(stateRef.current)), [commit]),
    openFile: useCallback(
      (entry: PanelEntry) => {
        const s = stateRef.current;
        if (s.tab === null) announce();
        commit(openFile(s, entry), s.tab === null);
      },
      [commit, announce],
    ),
  };
}

const PANEL_EVENT = "daedalus:panel-open";
/** From here two panes can each keep a panel open. */
export const DUAL_BOTH_MIN = 1920;

/** The panel part of a route, normalised the way this pane writes it. */
function routeKey(route: URLSearchParams, base: string): string {
  const q = readPanelQuery(route);
  return queryString(panelQuery(q ? applyPanelQuery(PANEL_CLOSED, q, base) : PANEL_CLOSED));
}

function queryString(q: Record<string, string | null>): string {
  return Object.entries(q)
    .filter(([, v]) => v)
    .map(([k, v]) => `${k}=${v}`)
    .join("&");
}

/** The panel's share of the chat area, dragged and remembered as a percentage. */
export function usePanelWidth(): [number, (dx: number, areaWidth: number) => void] {
  const [pct, setPct] = useState(() => readPanelPct());
  const drag = useCallback((dx: number, areaWidth: number) => {
    setPct((p) => {
      const next = clampPanelPct(p - (dx / Math.max(1, areaWidth)) * 100, areaWidth);
      rememberPanelPct(next);
      return next;
    });
  }, []);
  return [pct, drag];
}
