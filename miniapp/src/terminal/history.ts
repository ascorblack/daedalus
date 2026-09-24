// Lines a program scrolls off the top of the screen belong in the scrollback, whichever way it
// scrolls them.
//
// Codex (and every ratatui "inline" application) keeps its live area at the bottom of the screen and
// pushes finished output above it into the terminal's history: it sets a scroll region anchored at
// the top row (`CSI 1;n r`) and scrolls it up (`CSI n S`). xterm and Ghostty — the emulator behind
// the terminal daemon, and so behind every snapshot — move the lines that leave the top of such a
// region into the scrollback, exactly as a line feed at the bottom of it does. xterm.js deletes them
// instead: its SU splices the lines out of the buffer. So in the browser Codex's transcript simply
// vanished as it was written, while a reconnect (a snapshot) brought it back — the live view and the
// restored view of one terminal disagreed about its history.
//
// The fix handles SU itself when the region starts at the top row of the normal screen: it scrolls the
// region one line at a time through xterm.js's own line-feed scroll, which already keeps the line when
// the region is top-anchored, erases with the current background as SU does, and leaves the cursor
// where it was. Everything else (a region lower down, the alternate screen, which has no history)
// falls through to xterm.js unchanged.
//
// That scroll is not public API. The internals it reaches (`_core._bufferService.scroll`,
// `_core._inputHandler._eraseAttrData`) are looked up once, and when any of them is missing — a
// different xterm.js build — the fix stays out of the way and says so through `installed`, which the
// tests assert on for the pinned version, so an upgrade that moves them fails a test instead of
// quietly dropping history again.

/** The part of a terminal's parser this needs (the same in `@xterm/xterm` and `@xterm/headless`). */
type ParserLike = {
  registerCsiHandler(id: { final: string }, callback: (params: (number | number[])[]) => boolean): { dispose(): void };
};

type BufferLike = { scrollTop: number; scrollBottom: number };
type Internals = {
  bufferService: { buffer: BufferLike; buffers: { normal: BufferLike }; scroll(eraseAttr: unknown, isWrapped?: boolean): void };
  inputHandler: { _eraseAttrData(): unknown };
};

/** xterm.js's internals behind a public `Terminal`, or null when this build does not have them. */
function internals(term: unknown): Internals | null {
  const core = (term as { _core?: Record<string, unknown> } | null)?._core;
  const bufferService = core?._bufferService as Internals["bufferService"] | undefined;
  const inputHandler = core?._inputHandler as Internals["inputHandler"] | undefined;
  if (!bufferService || typeof bufferService.scroll !== "function" || !bufferService.buffers?.normal) return null;
  if (!inputHandler || typeof inputHandler._eraseAttrData !== "function") return null;
  return { bufferService, inputHandler };
}

/**
 * Makes SU in a top-anchored scroll region keep the lines it scrolls away, as xterm and Ghostty do.
 * `installed` is false when this xterm.js build lacks the internals, in which case nothing changed.
 */
export function keepScrolledHistory(term: { parser: ParserLike }): { installed: boolean; dispose(): void } {
  const inner = internals(term);
  if (!inner) return { installed: false, dispose: () => undefined };
  const handle = term.parser.registerCsiHandler({ final: "S" }, (params) => {
    const buffer = inner.bufferService.buffer;
    if (buffer !== inner.bufferService.buffers.normal || buffer.scrollTop !== 0) return false;
    const first = params[0];
    const asked = typeof first === "number" && first > 0 ? first : 1;
    // Scrolling a region by more than its height is the same as by its height; the daemon clamps
    // parameters too, but a loop in the browser must not trust that.
    const count = Math.min(asked, buffer.scrollBottom - buffer.scrollTop + 1);
    const erase = inner.inputHandler._eraseAttrData();
    for (let i = 0; i < count; i++) inner.bufferService.scroll(erase);
    return true;
  });
  return { installed: true, dispose: () => handle.dispose() };
}
