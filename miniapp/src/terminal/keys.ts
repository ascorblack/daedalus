// Which keys a focused terminal keeps for the program in it, and which the app takes.
//
// A terminal wants nearly every key: Ctrl+K kills a line in the shell, Ctrl+\ quits, Ctrl+R searches
// history. So while a terminal has focus, the app's own shortcuts step aside (`insideTerminal`), and
// only this short list is taken back. Keys are matched by `code`, the physical key, not by `key`:
// on a Russian layout Ctrl+` produces "ё" and Ctrl+Shift+C produces "С", and the shortcut has to work
// whatever layout is active.

export type TerminalAction =
  | "toggle-dock"
  | "copy"
  | "paste"
  | "search"
  | "font-bigger"
  | "font-smaller"
  | "font-reset"
  | "previous-mark"
  | "next-mark";

/** The fields of a `KeyboardEvent` this reads. */
export type KeyLike = Pick<KeyboardEvent, "code" | "ctrlKey" | "shiftKey" | "altKey" | "metaKey">;

export type KeyContext = {
  /** macOS, where Cmd is the copy/paste modifier and Ctrl belongs to the terminal. */
  mac: boolean;
  /** The alternate screen is up (vim, htop, less): Ctrl+↑/↓ go to the program, not to the marks. */
  altScreen: boolean;
};

/**
 * The app's action for a key pressed in a terminal, or null when the key belongs to the program.
 * Wired into `attachCustomKeyEventHandler`, which is told `false` (do not send) whenever this is not null.
 */
export function reservedKey(e: KeyLike, context: KeyContext): TerminalAction | null {
  if (e.altKey) return null;
  if (context.mac && e.metaKey && !e.ctrlKey) {
    if (e.code === "KeyC") return "copy";
    if (e.code === "KeyV") return "paste";
    return null;
  }
  if (!e.ctrlKey || e.metaKey) return null;
  if (e.code === "Backquote" && !e.shiftKey) return "toggle-dock";
  if (e.shiftKey && e.code === "KeyC") return "copy";
  if (e.shiftKey && e.code === "KeyV") return "paste";
  if (e.shiftKey && e.code === "KeyF") return "search";
  if (e.code === "Equal" || e.code === "NumpadAdd") return "font-bigger";
  if (e.code === "Minus" || e.code === "NumpadSubtract") return "font-smaller";
  if ((e.code === "Digit0" || e.code === "Numpad0") && !e.shiftKey) return "font-reset";
  if (!context.altScreen && !e.shiftKey) {
    if (e.code === "ArrowUp") return "previous-mark";
    if (e.code === "ArrowDown") return "next-mark";
  }
  return null;
}

/** Whether a key event's target is inside a terminal, where the app's global shortcuts stand aside. */
export function insideTerminal(target: EventTarget | null): boolean {
  const element = target as { closest?: (selector: string) => unknown } | null;
  return !!element && typeof element.closest === "function" && !!element.closest(".xterm, [data-terminal]");
}

/** Whether the platform is macOS (the copy/paste modifier is Cmd there). */
export function isMac(): boolean {
  const platform = (navigator as { userAgentData?: { platform?: string } }).userAgentData?.platform ?? navigator.platform ?? "";
  return /mac/i.test(platform);
}
