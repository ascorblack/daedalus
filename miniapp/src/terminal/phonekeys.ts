// What the phone's terminal sends for the keys a soft keyboard does not have, and how its sticky
// modifiers behave. Pure, so every byte is tested without a browser.
//
// A phone keyboard has no Esc, no Tab, no Ctrl and no arrows, and a shell without them is a shell
// that cannot leave vim or stop a runaway command. The row above the keyboard supplies them. Ctrl and
// Alt cannot be held down with the letter as on a hardware keyboard, so a tap arms them for the next
// key, and a second tap in quick succession locks them until tapped again.

/** One key of the row: what it is called on the key cap and what it sends. */
export type PhoneKey = { id: PhoneKeyId; cap: string; modifier?: "ctrl" | "alt" };

export type PhoneKeyId =
  | "esc" | "tab" | "ctrl" | "alt" | "up" | "down" | "left" | "right" | "slash" | "pipe" | "tilde" | "dash"
  | "ctrl-c" | "ctrl-d" | "ctrl-z" | "ctrl-l" | "ctrl-r" | "shift-tab" | "home" | "end" | "pgup" | "pgdn";

/** The row, in order. The first twelve are the mock-up's; after them, the combinations a thumb
 *  reaches for most in a shell, so ^C is one tap and not two. Key caps are the keys' own names, the
 *  same in every language, as they are printed on a keyboard. */
export const PHONE_KEYS: PhoneKey[] = [
  { id: "esc", cap: "Esc" },
  { id: "tab", cap: "Tab" },
  { id: "ctrl", cap: "Ctrl", modifier: "ctrl" },
  { id: "alt", cap: "Alt", modifier: "alt" },
  { id: "up", cap: "↑" },
  { id: "down", cap: "↓" },
  { id: "left", cap: "←" },
  { id: "right", cap: "→" },
  { id: "slash", cap: "/" },
  { id: "pipe", cap: "|" },
  { id: "tilde", cap: "~" },
  { id: "dash", cap: "-" },
  { id: "ctrl-c", cap: "^C" },
  { id: "ctrl-d", cap: "^D" },
  { id: "ctrl-z", cap: "^Z" },
  { id: "ctrl-l", cap: "^L" },
  { id: "ctrl-r", cap: "^R" },
  { id: "shift-tab", cap: "⇧Tab" },
  { id: "home", cap: "Home" },
  { id: "end", cap: "End" },
  { id: "pgup", cap: "PgUp" },
  { id: "pgdn", cap: "PgDn" },
];

export type Mods = { ctrl: boolean; alt: boolean };

const NONE: Mods = { ctrl: false, alt: false };

/** The xterm modifier parameter: 1 plus Shift 1, Alt 2, Ctrl 4. */
function modParam(mods: Mods, shift = false): number {
  return 1 + (shift ? 1 : 0) + (mods.alt ? 2 : 0) + (mods.ctrl ? 4 : 0);
}

/** The control character Ctrl makes of one character, or null when Ctrl leaves it alone. The table is
 *  xterm's: letters and `@[\]^_` by their ASCII value less 64, and the digits and punctuation that
 *  terminals map the same way (Ctrl+Space and Ctrl+2 are NUL, Ctrl+/ is the undo key of readline). */
export function controlOf(ch: string): string | null {
  if (ch.length !== 1) return null;
  const code = ch.toUpperCase().charCodeAt(0);
  if (code >= 64 && code <= 95) return String.fromCharCode(code - 64);
  const special: Record<string, string> = { " ": "\x00", "2": "\x00", "3": "\x1b", "4": "\x1c", "5": "\x1d", "6": "\x1e", "7": "\x1f", "8": "\x7f", "/": "\x1f", "?": "\x7f", "-": "\x1f", "~": "\x1e" };
  return special[ch] ?? null;
}

/**
 * Apply armed modifiers to what the soft keyboard typed. They act on the first character only — the
 * key they were armed for — and the rest passes as typed: a keyboard that delivers a whole word at
 * once must not have every letter of it turned into a control code. Control input (Enter, Backspace,
 * an escape sequence) takes Alt as a prefix and is otherwise left as it is.
 */
export function applyModifiers(data: string, mods: Mods): string {
  if (!data || (!mods.ctrl && !mods.alt)) return data;
  const first = [...data][0];
  const rest = data.slice(first.length);
  let head = first;
  if (mods.ctrl && !/^[\x00-\x1f\x7f]/.test(first)) head = controlOf(first) ?? first;
  if (mods.alt) head = `\x1b${head}`;
  return head + rest;
}

/**
 * The bytes a key of the row sends. Arrows follow the cursor-keys mode the program asked for
 * (`ESC O A` in application mode, which is what vim and less set, `ESC [ A` otherwise); with a
 * modifier armed they take xterm's modified form (`ESC [ 1 ; 5 A` for Ctrl+↑), which has no
 * application variant.
 */
export function keyBytes(id: PhoneKeyId, options: { appCursor: boolean; mods?: Mods }): string {
  const mods = options.mods ?? NONE;
  const modified = mods.ctrl || mods.alt;
  const cursor = (letter: string) => (modified ? `\x1b[1;${modParam(mods)}${letter}` : options.appCursor ? `\x1bO${letter}` : `\x1b[${letter}`);
  const tilde = (n: number) => (modified ? `\x1b[${n};${modParam(mods)}~` : `\x1b[${n}~`);
  const alt = (bytes: string) => (mods.alt ? `\x1b${bytes}` : bytes);
  switch (id) {
    case "esc":
      return "\x1b";
    case "tab":
      return alt("\t");
    case "shift-tab":
      return "\x1b[Z";
    case "up":
      return cursor("A");
    case "down":
      return cursor("B");
    case "right":
      return cursor("C");
    case "left":
      return cursor("D");
    case "home":
      return cursor("H");
    case "end":
      return cursor("F");
    case "pgup":
      return tilde(5);
    case "pgdn":
      return tilde(6);
    case "slash":
      return applyModifiers("/", mods);
    case "pipe":
      return applyModifiers("|", mods);
    case "tilde":
      return applyModifiers("~", mods);
    case "dash":
      return applyModifiers("-", mods);
    case "ctrl-c":
      return alt("\x03");
    case "ctrl-d":
      return alt("\x04");
    case "ctrl-z":
      return alt("\x1a");
    case "ctrl-l":
      return alt("\x0c");
    case "ctrl-r":
      return alt("\x12");
    default:
      return "";
  }
}

// ── sticky modifiers ─────────────────────────────────────────────────────────────────────────

/** A second tap within this long locks the modifier instead of disarming it. */
export const DOUBLE_TAP_MS = 350;

export type StickyState = "off" | "once" | "locked";

/** Ctrl or Alt on a touch screen: tap arms it for one key, a quick second tap locks it, and a tap on
 *  a locked one lets it go. */
export class StickyModifier {
  private value: StickyState = "off";
  private armedAt = -Infinity;

  get state(): StickyState {
    return this.value;
  }

  get on(): boolean {
    return this.value !== "off";
  }

  tap(now: number): StickyState {
    if (this.value === "off") {
      this.value = "once";
      this.armedAt = now;
    } else if (this.value === "once") {
      this.value = now - this.armedAt < DOUBLE_TAP_MS ? "locked" : "off";
    } else {
      this.value = "off";
    }
    return this.value;
  }

  /** A key used it: armed once, it disarms; locked, it stays. */
  consume(): void {
    if (this.value === "once") this.value = "off";
  }

  reset(): void {
    this.value = "off";
  }
}

// ── the compose line ─────────────────────────────────────────────────────────────────────────

/**
 * What the compose line sends. With bracketed paste on, the text goes as one paste — a shell then
 * takes several lines as one edit rather than running each — and any escape inside it is dropped,
 * so a pasted `ESC [ 201 ~` cannot end the paste early and have the rest run as typed. Without it,
 * line breaks become the Enter a terminal expects. Enter is pressed after the text when asked.
 */
export function composeBytes(text: string, options: { bracketed: boolean; enter: boolean }): string {
  const clean = text.replace(/\x1b/g, "");
  const body = clean.replace(/\r\n|\n/g, "\r");
  const sent = options.bracketed ? `\x1b[200~${body}\x1b[201~` : body;
  return sent + (options.enter ? "\r" : "");
}

// ── pinch ────────────────────────────────────────────────────────────────────────────────────

export const PINCH_MIN = 9;
export const PINCH_MAX = 22;

/** The font size a pinch ends on: the size it began at, scaled, whole and within reason. */
export function pinchFont(base: number, scale: number): number {
  if (!Number.isFinite(scale) || scale <= 0) return base;
  return Math.max(PINCH_MIN, Math.min(PINCH_MAX, Math.round(base * scale)));
}

// ── the selection layer ──────────────────────────────────────────────────────────────────────

/** The part of an xterm.js buffer the selection layer reads. */
export type BufferLike = {
  length: number;
  viewportY: number;
  getLine(y: number): { translateToString(trimRight?: boolean): string; isWrapped: boolean } | undefined;
};

/**
 * The text the long-press layer shows: the rows on screen and up to `history` rows above them, with
 * a line the terminal wrapped joined back into one, so a copied command is the command and not two
 * halves of it. Trailing blank rows are dropped.
 */
export function selectableText(buffer: BufferLike, rows: number, history = 200): string {
  const end = Math.min(buffer.length, buffer.viewportY + rows);
  let start = Math.max(0, buffer.viewportY - history);
  // Start on a line of its own, not in the middle of one that wrapped onto the first row read.
  while (start > 0 && buffer.getLine(start)?.isWrapped) start -= 1;
  const lines: string[] = [];
  for (let y = start; y < end; y++) {
    const line = buffer.getLine(y);
    if (!line) continue;
    // A row that continues on the next one keeps its trailing spaces: they are part of the line.
    const text = line.translateToString(!buffer.getLine(y + 1)?.isWrapped);
    if (line.isWrapped && lines.length) lines[lines.length - 1] += text;
    else lines.push(text);
  }
  while (lines.length && lines[lines.length - 1].trim() === "") lines.pop();
  return lines.join("\n");
}
