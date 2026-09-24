// Loading the terminal: xterm.js and its addons as one lazy chunk, and the bundled typeface before
// the first terminal opens.
//
// xterm.js measures a cell once, when it opens, from whatever font is in use at that moment. If the
// bundled font arrives a second later, the glyphs change width under a grid measured for the fallback
// and box drawing breaks apart. So the font is waited for — at most three seconds, because a terminal
// that never opens on a slow network is worse than one in a system monospace — and when it does not
// come in time the terminal is opened with the fallback stack alone, so nothing swaps under it later.

/** The bundled face, then the same monospace stack the rest of the app uses. */
export const FONT_STACK = '"JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace';
export const FALLBACK_STACK = 'ui-monospace, "SF Mono", Menlo, Consolas, monospace';
export const FONT_TIMEOUT_MS = 3000;

export type Kit = typeof import("./kit") & { fontFamily: string };

let pending: Promise<Kit> | null = null;

/** The font family to open terminals with: the bundled one if both weights loaded in time. */
export async function pickFontFamily(fonts: Pick<FontFaceSet, "load"> | undefined, timeoutMs = FONT_TIMEOUT_MS): Promise<string> {
  if (!fonts) return FALLBACK_STACK;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<false>((resolve) => {
    timer = setTimeout(() => resolve(false), timeoutMs);
  });
  const loaded = Promise.all([fonts.load('13px "JetBrains Mono"'), fonts.load('bold 13px "JetBrains Mono"')]).then(
    (faces) => faces.every((list) => list.length > 0),
    () => false,
  );
  try {
    return (await Promise.race([loaded, timeout])) ? FONT_STACK : FALLBACK_STACK;
  } finally {
    clearTimeout(timer);
  }
}

/** xterm.js, its addons and the font, loaded once and shared by every terminal on the page. */
export function loadTerminalKit(): Promise<Kit> {
  pending ??= Promise.all([import("./kit"), pickFontFamily(typeof document !== "undefined" ? document.fonts : undefined)])
    .then(([kit, fontFamily]) => ({ ...kit, fontFamily }))
    .catch((error) => {
      // A failed chunk load (a deploy replaced the files, the network dropped) must be retryable.
      pending = null;
      throw error;
    });
  return pending;
}
