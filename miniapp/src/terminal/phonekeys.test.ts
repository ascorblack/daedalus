import { describe, expect, it } from "vitest";
import { applyModifiers, BufferLike, composeBytes, controlOf, DOUBLE_TAP_MS, keyBytes, PHONE_KEYS, pinchFont, selectableText, StickyModifier } from "./phonekeys";

const plain = { ctrl: false, alt: false };

describe("the keys a phone keyboard lacks", () => {
  it("starts with the mock-up's twelve, in its order, and every key sends something", () => {
    expect(PHONE_KEYS.slice(0, 12).map((k) => k.cap)).toEqual(["Esc", "Tab", "Ctrl", "Alt", "↑", "↓", "←", "→", "/", "|", "~", "-"]);
    for (const key of PHONE_KEYS) {
      if (key.modifier) continue;
      expect(keyBytes(key.id, { appCursor: false }), key.id).not.toBe("");
    }
    expect(new Set(PHONE_KEYS.map((k) => k.id)).size).toBe(PHONE_KEYS.length);
  });

  it("sends arrows in the cursor-keys mode the program asked for", () => {
    expect(keyBytes("up", { appCursor: false })).toBe("\x1b[A");
    expect(keyBytes("down", { appCursor: false })).toBe("\x1b[B");
    expect(keyBytes("right", { appCursor: false })).toBe("\x1b[C");
    expect(keyBytes("left", { appCursor: false })).toBe("\x1b[D");
    expect(keyBytes("up", { appCursor: true })).toBe("\x1bOA");
    expect(keyBytes("left", { appCursor: true })).toBe("\x1bOD");
    expect(keyBytes("home", { appCursor: true })).toBe("\x1bOH");
    expect(keyBytes("end", { appCursor: false })).toBe("\x1b[F");
  });

  it("gives an armed modifier xterm's modified form, which has no application variant", () => {
    expect(keyBytes("up", { appCursor: true, mods: { ctrl: true, alt: false } })).toBe("\x1b[1;5A");
    expect(keyBytes("left", { appCursor: false, mods: { ctrl: false, alt: true } })).toBe("\x1b[1;3D");
    expect(keyBytes("right", { appCursor: false, mods: { ctrl: true, alt: true } })).toBe("\x1b[1;7C");
    expect(keyBytes("pgup", { appCursor: false })).toBe("\x1b[5~");
    expect(keyBytes("pgdn", { appCursor: false, mods: { ctrl: true, alt: false } })).toBe("\x1b[6;5~");
  });

  it("sends Esc, Tab, Shift+Tab and the one-tap combinations", () => {
    expect(keyBytes("esc", { appCursor: false })).toBe("\x1b");
    expect(keyBytes("tab", { appCursor: false })).toBe("\t");
    expect(keyBytes("shift-tab", { appCursor: false })).toBe("\x1b[Z");
    expect(keyBytes("ctrl-c", { appCursor: false })).toBe("\x03");
    expect(keyBytes("ctrl-d", { appCursor: false })).toBe("\x04");
    expect(keyBytes("ctrl-z", { appCursor: false })).toBe("\x1a");
    expect(keyBytes("ctrl-l", { appCursor: false })).toBe("\x0c");
    expect(keyBytes("ctrl-r", { appCursor: false })).toBe("\x12");
    expect(keyBytes("ctrl-c", { appCursor: false, mods: { ctrl: false, alt: true } })).toBe("\x1b\x03");
  });

  it("sends the punctuation keys as typed, and through an armed modifier", () => {
    expect(keyBytes("pipe", { appCursor: false })).toBe("|");
    expect(keyBytes("slash", { appCursor: false, mods: { ctrl: true, alt: false } })).toBe("\x1f");
    expect(keyBytes("dash", { appCursor: false, mods: { ctrl: false, alt: true } })).toBe("\x1b-");
  });
});

describe("Ctrl and Alt on what the soft keyboard typed", () => {
  it("maps letters and xterm's punctuation to control codes", () => {
    expect(controlOf("c")).toBe("\x03");
    expect(controlOf("C")).toBe("\x03");
    expect(controlOf("a")).toBe("\x01");
    expect(controlOf("z")).toBe("\x1a");
    expect(controlOf("@")).toBe("\x00");
    expect(controlOf("[")).toBe("\x1b");
    expect(controlOf("\\")).toBe("\x1c");
    expect(controlOf("]")).toBe("\x1d");
    expect(controlOf("^")).toBe("\x1e");
    expect(controlOf("_")).toBe("\x1f");
    expect(controlOf(" ")).toBe("\x00");
    expect(controlOf("?")).toBe("\x7f");
    expect(controlOf("ж")).toBeNull();
    expect(controlOf("ab")).toBeNull();
  });

  it("acts on the first character only, and prefixes Alt with ESC", () => {
    expect(applyModifiers("c", { ctrl: true, alt: false })).toBe("\x03");
    expect(applyModifiers("cd", { ctrl: true, alt: false })).toBe("\x03d");
    expect(applyModifiers("b", { ctrl: false, alt: true })).toBe("\x1bb");
    expect(applyModifiers("x", { ctrl: true, alt: true })).toBe("\x1b\x18");
    expect(applyModifiers("\r", { ctrl: true, alt: false })).toBe("\r");
    expect(applyModifiers("\r", { ctrl: false, alt: true })).toBe("\x1b\r");
    expect(applyModifiers("ж", { ctrl: true, alt: false })).toBe("ж");
    expect(applyModifiers("ls", plain)).toBe("ls");
    expect(applyModifiers("", { ctrl: true, alt: true })).toBe("");
  });

  it("keeps a character outside the basic plane whole", () => {
    expect(applyModifiers("😀a", { ctrl: false, alt: true })).toBe("\x1b😀a");
  });
});

describe("a sticky modifier", () => {
  it("arms for one key", () => {
    const ctrl = new StickyModifier();
    expect(ctrl.tap(0)).toBe("once");
    expect(ctrl.on).toBe(true);
    ctrl.consume();
    expect(ctrl.state).toBe("off");
  });

  it("locks on a quick second tap and stays through keys until tapped again", () => {
    const ctrl = new StickyModifier();
    ctrl.tap(1000);
    expect(ctrl.tap(1000 + DOUBLE_TAP_MS - 1)).toBe("locked");
    ctrl.consume();
    ctrl.consume();
    expect(ctrl.state).toBe("locked");
    expect(ctrl.tap(5000)).toBe("off");
  });

  it("disarms on a slow second tap", () => {
    const alt = new StickyModifier();
    alt.tap(0);
    expect(alt.tap(DOUBLE_TAP_MS + 1)).toBe("off");
  });
});

describe("the compose line", () => {
  it("sends one bracketed paste when the program asked for it", () => {
    expect(composeBytes("echo a\necho b", { bracketed: true, enter: true })).toBe("\x1b[200~echo a\recho b\x1b[201~\r");
    expect(composeBytes("ls", { bracketed: true, enter: false })).toBe("\x1b[200~ls\x1b[201~");
  });

  it("cannot be closed early by a paste end inside the text", () => {
    expect(composeBytes("a\x1b[201~rm -rf x", { bracketed: true, enter: false })).toBe("\x1b[200~a[201~rm -rf x\x1b[201~");
  });

  it("sends plain text with Enter where there is no bracketed paste", () => {
    expect(composeBytes("git status", { bracketed: false, enter: true })).toBe("git status\r");
    expect(composeBytes("a\r\nb", { bracketed: false, enter: false })).toBe("a\rb");
  });
});

describe("a pinch", () => {
  it("scales the font it began at, whole and between 9 and 22", () => {
    expect(pinchFont(13, 1.5)).toBe(20);
    expect(pinchFont(13, 0.5)).toBe(9);
    expect(pinchFont(13, 3)).toBe(22);
    expect(pinchFont(13, 1)).toBe(13);
    expect(pinchFont(13, Number.NaN)).toBe(13);
    expect(pinchFont(13, 0)).toBe(13);
  });
});

function buffer(rows: [string, boolean][], viewportY: number): BufferLike {
  return {
    length: rows.length,
    viewportY,
    getLine: (y) => (rows[y] ? { translateToString: (trim?: boolean) => (trim ? rows[y][0].replace(/\s+$/, "") : rows[y][0]), isWrapped: rows[y][1] } : undefined),
  };
}

describe("the selection layer's text", () => {
  it("reads the screen and the history above it, joining wrapped rows", () => {
    const rows: [string, boolean][] = [["old", false], ["$ echo aaaa", false], ["bbbb  ", true], ["aaaabbbb", false], ["", false]];
    expect(selectableText(buffer(rows, 1), 4, 200)).toBe("old\n$ echo aaaabbbb\naaaabbbb");
  });

  it("keeps the spaces at a wrap and never starts halfway through a wrapped line", () => {
    const rows: [string, boolean][] = [["one  ", false], ["two", true], ["three", false]];
    expect(selectableText(buffer(rows, 1), 2, 0)).toBe("one  two\nthree");
  });

  it("reads no more history than asked", () => {
    const rows: [string, boolean][] = Array.from({ length: 500 }, (_, i) => [`line ${i}`, false]);
    const text = selectableText(buffer(rows, 476), 24, 200);
    expect(text.split("\n")).toHaveLength(224);
    expect(text.startsWith("line 276\n")).toBe(true);
    expect(text.endsWith("line 499")).toBe(true);
  });
});
