// The layout is the operator's, not the session's: which panes are open, how wide they were dragged,
// whether the sidebar is a column or a strip. All of it is remembered per browser and none of it
// travels with a session, so leaving for Settings and coming back does not mean reopening anything.

import { useCallback, useRef, useState } from "react";
import { t } from "./i18n";

export function readLayout(key: string): string | null {
  try {
    return localStorage.getItem(`daedalus.session.${key}`);
  } catch {
    return null;
  }
}

export function writeLayout(key: string, value: string): void {
  try {
    localStorage.setItem(`daedalus.session.${key}`, value);
  } catch {
    /* private mode: the layout lasts for the visit */
  }
}

/** A pane width the operator dragged, remembered per browser. */
export function usePaneWidth(key: string, initial: number, min: number, max: number): [number, (w: number) => void] {
  const storageKey = `daedalus.width.${key}`;
  const [width, setWidth] = useState(() => {
    try {
      const v = Number(localStorage.getItem(storageKey));
      return v >= min && v <= max ? v : initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (w: number) => {
      const clamped = Math.round(Math.min(max, Math.max(min, w)));
      setWidth(clamped);
      try {
        localStorage.setItem(storageKey, String(clamped));
      } catch {
        /* private mode */
      }
    },
    [storageKey, min, max],
  );
  return [width, set];
}

/** The strip between two panes: drag it to resize (pointer events, so mouse and touch alike). */
export function PaneHandle({ side, onDrag }: { side: "left" | "right"; onDrag: (dx: number) => void }) {
  const last = useRef<number | null>(null);
  return (
    <div
      className={`pane-handle wide-only ${side}`}
      role="separator"
      aria-orientation="vertical"
      aria-label={t("session.resize")}
      onPointerDown={(e) => {
        last.current = e.clientX;
        (e.target as HTMLElement).setPointerCapture(e.pointerId);
        document.body.classList.add("resizing");
      }}
      onPointerMove={(e) => {
        if (last.current === null) return;
        const dx = e.clientX - last.current;
        last.current = e.clientX;
        if (dx) onDrag(dx);
      }}
      onPointerUp={(e) => {
        last.current = null;
        (e.target as HTMLElement).releasePointerCapture(e.pointerId);
        document.body.classList.remove("resizing");
      }}
      onPointerCancel={() => {
        last.current = null;
        document.body.classList.remove("resizing");
      }}
    />
  );
}

// ── the sidebar: a column, or a strip of icons ───────────────────────────────────────────────

const SIDEBAR = "daedalus.sidebar";

/** Below this the column would take a third of the window, so the strip is the default there. */
export const SIDEBAR_COLUMN_MIN = 1280;

/** Whether the sidebar is the strip: what the operator chose, or, having chosen nothing, what the window allows. */
export function sidebarCollapsed(stored: string | null, viewportWidth: number): boolean {
  if (stored === "collapsed") return true;
  if (stored === "open") return false;
  return viewportWidth < SIDEBAR_COLUMN_MIN;
}

export function readSidebar(viewportWidth: number): boolean {
  let stored: string | null = null;
  try {
    stored = localStorage.getItem(SIDEBAR);
  } catch {
    /* private mode */
  }
  return sidebarCollapsed(stored, viewportWidth);
}

export function rememberSidebar(collapsed: boolean): void {
  try {
    localStorage.setItem(SIDEBAR, collapsed ? "collapsed" : "open");
  } catch {
    /* private mode: the choice lasts for the visit */
  }
}
