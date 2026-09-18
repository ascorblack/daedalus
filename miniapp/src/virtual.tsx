// A windowed list for the conversation: only the turns near the viewport are in the DOM, the rest
// are two spacers of the right height.
//
// Turns have no common height — a one-line answer and a forty-step run are the same list — so the
// heights are measured as they render and remembered by key; a turn that has never been on screen
// is assumed to be `estimate` tall. Two things keep that honest: the spacers are recomputed from
// the measurements on every layout, and the item the reader is looking at is held in place when a
// guess above it turns out to be wrong.
//
// Below `threshold` items nothing is windowed and the children are rendered exactly as they were,
// with no wrapper: a short conversation is not worth a scroll anchor.

import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";

/** The nearest ancestor that really scrolls: the sidebar's body in the shell, the screen on a phone.
 *
 *  Nearest *scrolling*, not nearest scrollable: the sidebar's column holds a screen that is allowed
 *  to scroll and does not, inside a body that does. Taking the first ``overflow: auto`` ancestor
 *  there gives an element whose scrollTop is always zero, and a window that never moves with the
 *  reader. */
export function scrollParent(el: HTMLElement | null): HTMLElement | null {
  let candidate: HTMLElement | null = null;
  for (let p = el?.parentElement ?? null; p; p = p.parentElement) {
    const overflow = getComputedStyle(p).overflowY;
    if (overflow !== "auto" && overflow !== "scroll") continue;
    candidate ??= p;
    if (p.scrollHeight > p.clientHeight + 4) return p;
  }
  return candidate ?? ((document.scrollingElement as HTMLElement | null) ?? null);
}

export type WindowedProps = {
  /** One stable key per item, in order. */
  keys: string[];
  render: (index: number) => ReactNode;
  /** The element that scrolls. */
  scroller: RefObject<HTMLElement | null>;
  /** Height assumed for an item that has not been measured yet. */
  estimate?: number;
  /** How much above and below the viewport is rendered anyway, in pixels. */
  overscan?: number;
  /** The gap the list's own layout puts between items, so the spacers land where the items would. */
  gap?: number;
  threshold?: number;
  /** True while the screen is pinned to the bottom: the anchor must not fight the pin. */
  pinned?: () => boolean;
  /** True while the reader has a finger or a wheel on the list: the anchor must not fight them either. */
  dragging?: () => boolean;
  /** The first item is in the window: whatever comes before it, if anything does, is wanted now. */
  onTop?: () => void;
};

export function Windowed({ keys, render, scroller, estimate = 260, overscan = 900, gap = 14, threshold = 60, pinned, dragging, onTop }: WindowedProps) {
  const sizes = useRef(new Map<string, number>());
  /** The width every stored height was measured at: at another width they describe nothing. */
  const width = useRef(0);
  /** The item the reader is looking at and where the list put it, so it can be put back there. */
  const anchor = useRef<{ key: string; offset: number } | null>(null);
  const [, bump] = useState(0);
  const [range, setRange] = useState({ start: 0, end: keys.length });
  const windowed = keys.length > threshold;

  // Where each item starts, from the top of the list, with the gaps counted in. An item nobody has
  // seen yet is assumed to be as tall as the ones that have been measured, so the list does not
  // grow under the reader as they scroll into it.
  const offsets = useRef<number[]>([]);
  if (windowed) {
    let seen = 0;
    for (const h of sizes.current.values()) seen += h;
    const guess = sizes.current.size ? seen / sizes.current.size : estimate;
    const out = new Array<number>(keys.length + 1);
    out[0] = 0;
    for (let i = 0; i < keys.length; i++) out[i + 1] = out[i] + (sizes.current.get(keys[i]) ?? guess) + gap;
    offsets.current = out;
  }

  const recompute = useCallback(() => {
    const host = scroller.current;
    const off = offsets.current;
    if (!host || off.length !== keys.length + 1) return;
    const top = host.scrollTop - overscan;
    const bottom = host.scrollTop + host.clientHeight + overscan;
    let start = 0;
    while (start < keys.length && off[start + 1] < top) start++;
    let end = start;
    while (end < keys.length && off[end] < bottom) end++;
    end = Math.max(end, start + 1);
    // The oldest item the list holds is on screen, so the page before it is what the reader is
    // reaching for. Asking on the rendered range rather than on a distance in pixels is what makes
    // this reliable: a re-measured guess above the reader moves the pixels and does not move this.
    if (start === 0) onTop?.();
    setRange((r) => (r.start === start && r.end === end ? r : { start, end }));
  }, [keys.length, overscan, scroller, onTop]);

  // A new list (a message arrived, an older page was put in front) moves every offset below it.
  useEffect(() => {
    if (windowed) recompute();
    else setRange((r) => (r.start === 0 && r.end === keys.length ? r : { start: 0, end: keys.length }));
  }, [keys, windowed, recompute]);

  useEffect(() => {
    const host = scroller.current;
    if (!host || !windowed) return;
    let frame = 0;
    const on = () => {
      // What the reader is looking at, so neither a re-measured guess above it nor a page of older
      // messages put in front of it moves the page under them.
      let best: HTMLElement | null = null;
      for (const slot of host.querySelectorAll<HTMLElement>("[data-slot]")) {
        best = slot;
        if (slot.offsetTop + slot.offsetHeight > host.scrollTop) break;
      }
      if (best) {
        const key = best.dataset.slot!;
        const i = keys.indexOf(key);
        if (i >= 0) anchor.current = { key, offset: offsets.current[i] };
      }
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        recompute();
      });
    };
    host.addEventListener("scroll", on, { passive: true });
    return () => {
      host.removeEventListener("scroll", on);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [keys, scroller, windowed, recompute]);

  // Measure what is on screen, and hold the reader's place if the spacers moved under them.
  useLayoutEffect(() => {
    const host = scroller.current;
    if (!host || !windowed) return;
    // A turn is as tall as the list is wide. A rotation, a pane opening, a desktop window resized:
    // every height remembered — and the average the unmeasured ones are guessed at — describes a
    // width that is gone, so the spacers would put the reader screens away from where they were.
    const w = host.clientWidth;
    if (w && width.current && w !== width.current) {
      sizes.current.clear();
      anchor.current = null;
    }
    if (w) width.current = w;
    let dirty = false;
    for (const slot of host.querySelectorAll<HTMLElement>("[data-slot]")) {
      const key = slot.dataset.slot!;
      const h = slot.offsetHeight;
      if (h > 0 && sizes.current.get(key) !== h) {
        sizes.current.set(key, h);
        dirty = true;
      }
    }
    const held = anchor.current;
    // While a finger or a wheel is on the list the reader is driving it; adding to `scrollTop` under
    // them is felt as the list pulling back, and it is what kept a flick to the top from arriving.
    if (held && !pinned?.() && !dragging?.()) {
      const i = keys.indexOf(held.key);
      const now = i >= 0 ? offsets.current[i] : undefined;
      if (now !== undefined && now !== held.offset) {
        host.scrollTop += now - held.offset;
        anchor.current = { key: held.key, offset: now };
      }
    }
    if (dirty) bump((n) => n + 1);
  });

  // Images and code blocks take their height after the first layout; the spacers follow them.
  useEffect(() => {
    const host = scroller.current;
    if (!host || !windowed || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => bump((n) => n + 1));
    for (const slot of host.querySelectorAll<HTMLElement>("[data-slot]")) ro.observe(slot);
    return () => ro.disconnect();
  }, [scroller, windowed, range]);

  if (!windowed) return <>{keys.map((_, i) => <Fragment key={keys[i]}>{render(i)}</Fragment>)}</>;

  const off = offsets.current;
  const total = off[keys.length] - gap;
  const start = Math.min(range.start, Math.max(0, keys.length - 1));
  const end = Math.min(Math.max(range.end, start + 1), keys.length);
  const padTop = start > 0 ? off[start] - gap : 0;
  const padBottom = end < keys.length ? total - off[end] : 0;
  const slots: ReactNode[] = [];
  for (let i = start; i < end; i++) {
    slots.push(
      <div key={keys[i]} data-slot={keys[i]} className="turn-slot">
        {render(i)}
      </div>,
    );
  }
  return (
    <>
      {padTop > 0 && <div style={{ height: padTop }} aria-hidden />}
      {slots}
      {padBottom > 0 && <div style={{ height: padBottom }} aria-hidden />}
    </>
  );
}

// A windowed list for rows that are already inside something else: the sessions in a folder, where
// the folder's own header, its root line and its empty state are rendered by the caller and only
// the rows between them are windowed.
//
// Nothing is plumbed through the shell for this. The rows are found in the host by the selector the
// caller gives, the element that scrolls is found by walking up from the host, and the position of
// the list is read from the first row that is actually on screen rather than assumed — so a folder
// below another one whose window just changed is right on the next frame either way.
//
// Heights are measured as rows render and remembered by key; a row nobody has seen is as tall as the
// average of the ones that have been. The row the reader has focused is kept rendered wherever it
// is, because a focused element that unmounts takes the focus to the body with it.

export type WindowedRowsProps = {
  /** One stable key per row, in order. */
  keys: string[];
  render: (index: number) => ReactNode;
  /** The element the rows are rendered into, and how to find them in it. */
  host: RefObject<HTMLElement | null>;
  rowSelector: string;
  /** Height assumed for a row nobody has measured yet. */
  estimate?: number;
  /** How much above and below the viewport is rendered anyway, in pixels. */
  overscan?: number;
  /** Below this many rows nothing is windowed and the rows are rendered exactly as they were. */
  threshold?: number;
};

export function WindowedRows({ keys, render, host, rowSelector, estimate = 64, overscan = 400, threshold = 24 }: WindowedRowsProps) {
  const sizes = useRef(new Map<string, number>());
  const scroller = useRef<HTMLElement | null>(null);
  /** The row the reader is on, so the window never unmounts it from under them. */
  const focused = useRef(-1);
  const [range, setRange] = useState({ start: 0, end: keys.length });
  const held = useRef(range);
  held.current = range;
  const windowed = keys.length > threshold;

  /** Every row's height: measured where it has been on screen, and the estimate where it has not.
   *  A fixed estimate rather than a running average of what has been seen: these rows are one or two
   *  lines and nothing else, so the estimate is right to a pixel or two, and a guess that does not
   *  move is a spacer that does not move the list under the reader as they scroll into it. */
  const heights = useCallback(() => keys.map((key) => sizes.current.get(key) ?? estimate), [keys, estimate]);

  /** The element that scrolls, looked up again while it is not one.
   *
   *  The host is the caller's own element and its ref is attached after this component's effects
   *  have run, so the first look-up finds nothing; and the sidebar's body only starts scrolling once
   *  there are rows in it. Both are answered by asking again rather than by assuming. */
  const resolve = useCallback(() => {
    const found = scroller.current;
    if (found && found.isConnected && found.scrollHeight > found.clientHeight + 4) return found;
    scroller.current = scrollParent(host.current) ?? found;
    return scroller.current;
  }, [host]);

  const recompute = useCallback(() => {
    const scroll = resolve();
    const section = host.current;
    if (!scroll || !section || !windowed) return;
    const rows = section.querySelectorAll<HTMLElement>(rowSelector);
    if (rows.length === 0) return;
    const hs = heights();
    // Where row zero would be, in the scroller's own coordinates, taken from a row that is really
    // there: the rows above it are as tall as they have been measured to be.
    const base = scroll.getBoundingClientRect().top - scroll.scrollTop;
    let above = 0;
    for (let i = 0; i < held.current.start && i < hs.length; i++) above += hs[i];
    const listTop = rows[0].getBoundingClientRect().top - base - above;
    const viewTop = scroll.scrollTop - overscan;
    const viewBottom = scroll.scrollTop + scroll.clientHeight + overscan;
    let start = 0;
    let y = listTop;
    while (start < keys.length - 1 && y + hs[start] < viewTop) {
      y += hs[start];
      start += 1;
    }
    let end = start;
    let bottom = y;
    while (end < keys.length && bottom < viewBottom) {
      bottom += hs[end];
      end += 1;
    }
    end = Math.max(end, start + 1);
    if (focused.current >= 0 && focused.current < keys.length) {
      start = Math.min(start, focused.current);
      end = Math.max(end, focused.current + 1);
    }
    setRange((r) => (r.start === start && r.end === end ? r : { start, end }));
  }, [keys.length, heights, host, overscan, resolve, rowSelector, windowed]);

  useEffect(() => {
    if (windowed) recompute();
    else setRange((r) => (r.start === 0 && r.end === keys.length ? r : { start: 0, end: keys.length }));
  }, [keys, windowed, recompute]);

  useEffect(() => {
    const scroll = resolve();
    if (!scroll || !windowed) return;
    const target: EventTarget = scroll === document.scrollingElement ? window : scroll;
    let frame = 0;
    const on = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        recompute();
      });
    };
    target.addEventListener("scroll", on, { passive: true });
    window.addEventListener("resize", on);
    return () => {
      target.removeEventListener("scroll", on);
      window.removeEventListener("resize", on);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [keys.length, windowed, resolve, recompute]);

  // Measure what is on screen. A row's height is taken from where the next one starts, so the margin
  // between them is part of it and the spacers keep the list exactly as tall as it was.
  useLayoutEffect(() => {
    const section = host.current;
    if (!section || !windowed) return;
    const rows = [...section.querySelectorAll<HTMLElement>(rowSelector)];
    let dirty = false;
    for (let i = 0; i < rows.length; i++) {
      const key = keys[held.current.start + i];
      if (key === undefined) break;
      const rect = rows[i].getBoundingClientRect();
      const next = rows[i + 1];
      const h = Math.round(next ? next.getBoundingClientRect().top - rect.top : rect.height);
      if (h > 0 && sizes.current.get(key) !== h) {
        sizes.current.set(key, h);
        dirty = true;
      }
    }
    if (dirty) recompute();
  });

  // Where the reader's focus is, so the window keeps that row however far they scroll away from it.
  useEffect(() => {
    const section = host.current;
    if (!section || !windowed) return;
    const on = (e: FocusEvent) => {
      const rows = [...section.querySelectorAll<HTMLElement>(rowSelector)];
      const at = rows.findIndex((row) => row === e.target || row.contains(e.target as Node));
      focused.current = at < 0 ? -1 : held.current.start + at;
    };
    section.addEventListener("focusin", on);
    return () => section.removeEventListener("focusin", on);
  }, [host, rowSelector, windowed]);

  if (!windowed) return <>{keys.map((_, i) => <Fragment key={keys[i]}>{render(i)}</Fragment>)}</>;

  const hs = heights();
  const start = Math.min(range.start, Math.max(0, keys.length - 1));
  const end = Math.min(Math.max(range.end, start + 1), keys.length);
  let padTop = 0;
  for (let i = 0; i < start; i++) padTop += hs[i];
  let padBottom = 0;
  for (let i = end; i < keys.length; i++) padBottom += hs[i];
  const slots: ReactNode[] = [];
  for (let i = start; i < end; i++) slots.push(<Fragment key={keys[i]}>{render(i)}</Fragment>);
  return (
    <>
      {padTop > 0 && <div style={{ height: Math.round(padTop) }} aria-hidden />}
      {slots}
      {padBottom > 0 && <div style={{ height: Math.round(padBottom) }} aria-hidden />}
    </>
  );
}
