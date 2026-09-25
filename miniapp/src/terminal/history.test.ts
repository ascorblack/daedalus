// @vitest-environment jsdom
// Codex pushes its finished output into the terminal's history by scrolling a top-anchored region up
// (`CSI 1;n r` then `CSI n S`). Ghostty, behind every snapshot, keeps those lines; stock xterm.js
// deletes them. These run the same bytes through @xterm/headless with and without the fix, and once
// through the browser build the app ships, because the fix reaches into internals and the pinned
// version must be the one that has them.

import { Terminal as Headless } from "@xterm/headless";
import { Terminal as Browser } from "@xterm/xterm";
import { describe, expect, it } from "vitest";
import { keepScrolledHistory } from "./history";

const CSI = "\x1b[";

function headless(cols: number, rows: number, fixed: boolean): Headless {
  const term = new Headless({ cols, rows, scrollback: 1000, allowProposedApi: true });
  if (fixed) expect(keepScrolledHistory(term).installed).toBe(true);
  return term;
}

function write(term: { write(data: string, callback: () => void): void }, data: string): Promise<void> {
  return new Promise((resolve) => term.write(data, resolve));
}

/** Every line of the buffer, scrollback first, with trailing blanks trimmed and trailing empty lines dropped. */
function lines(term: Headless | Browser): string[] {
  const buffer = term.buffer.active;
  const out: string[] = [];
  for (let y = 0; y < buffer.length; y++) out.push(buffer.getLine(y)!.translateToString(true));
  while (out.length && out[out.length - 1] === "") out.pop();
  return out;
}

const shell = (n: number) => Array.from({ length: n }, (_, i) => `shell line ${i}\r\n`).join("");
// The reproduction: ten lines of shell, then a region over the top three rows scrolled up
// by three, the region reset and the screen cleared for the live area.
const PROBE = `${shell(10)}${CSI}1;3r${CSI}3S${CSI}r${CSI}1;1H${CSI}Jviewport`;

describe("scrolling a top-anchored region up", () => {
  it("keeps the lines that leave the top, as Ghostty does", async () => {
    const term = headless(80, 24, true);
    await write(term, PROBE);
    const all = lines(term);
    expect(all.slice(0, 3)).toEqual(["shell line 0", "shell line 1", "shell line 2"]);
    expect(term.buffer.active.baseY).toBe(3);
    expect(all[3]).toBe("viewport");
  });

  it("is what stock xterm.js gets wrong, so the test has teeth", async () => {
    const term = headless(80, 24, false);
    await write(term, PROBE);
    expect(lines(term)).toEqual(["viewport"]);
    expect(term.buffer.active.baseY).toBe(0);
  });

  it("keeps a whole inline transcript in order above a live area that stays put", async () => {
    // What Codex does turn after turn: its composer lives in the bottom four rows, and every
    // finished line is inserted above it by scrolling rows 1–6 up and writing into the gap.
    const rows = 10;
    const term = headless(40, rows, true);
    await write(term, `${CSI}7;1Hstatus${CSI}8;1H> composer${CSI}10;1Hfooter`);
    const transcript = Array.from({ length: 30 }, (_, i) => `history ${i}`);
    for (let i = 0; i < transcript.length; i += 2) {
      await write(term, `${CSI}1;6r${CSI}2S${CSI}5;1H${transcript[i]}${CSI}6;1H${transcript[i + 1]}${CSI}r${CSI}8;11H`);
    }
    const all = lines(term);
    const base = term.buffer.active.baseY;
    // Everything the program wrote is somewhere in the buffer, in order: the older lines in the
    // scrollback, the newest still on the screen above the live area.
    expect(all.filter((l) => l.startsWith("history "))).toEqual(transcript);
    expect(all.slice(base + 6, base + 10)).toEqual(["status", "> composer", "", "footer"]);
    // The cursor is where the program left it, not dragged by the scroll.
    expect(term.buffer.active.cursorY).toBe(7);
    expect(term.buffer.active.cursorX).toBe(10);
  });

  it("erases with the current background, as SU does", async () => {
    const term = headless(20, 6, true);
    await write(term, `${shell(3)}${CSI}1;3r${CSI}44m${CSI}1S${CSI}0m${CSI}r`);
    const blank = term.buffer.active.getLine(term.buffer.active.baseY + 2)!.getCell(0)!;
    expect(blank.isBgPalette()).toBe(true);
    expect(blank.getBgColor()).toBe(4);
  });

  it("scrolls at most the region's height however large the count", async () => {
    const term = headless(20, 6, true);
    await write(term, `${shell(3)}${CSI}1;3r${CSI}100000S${CSI}r`);
    expect(term.buffer.active.baseY).toBe(3);
  });
});

describe("what the fix leaves to xterm.js", () => {
  it("does not touch a region that starts below the top row", async () => {
    const fixed = headless(20, 8, true);
    const stock = headless(20, 8, false);
    const bytes = `${shell(6)}${CSI}3;5r${CSI}2S${CSI}r`;
    await write(fixed, bytes);
    await write(stock, bytes);
    expect(lines(fixed)).toEqual(lines(stock));
    expect(fixed.buffer.active.baseY).toBe(0);
  });

  it("does not give the alternate screen a history", async () => {
    const fixed = headless(20, 6, true);
    const stock = headless(20, 6, false);
    const bytes = `${CSI}?1049h${shell(4)}${CSI}1;3r${CSI}2S`;
    await write(fixed, bytes);
    await write(stock, bytes);
    expect(fixed.buffer.active.type).toBe("alternate");
    expect(lines(fixed)).toEqual(lines(stock));
    await write(fixed, `${CSI}?1049l`);
    expect(fixed.buffer.active.baseY).toBe(0);
  });
});

describe("the browser build the app ships", () => {
  it("has the internals the fix needs, and keeps the history with it", async () => {
    // The page runs @xterm/xterm, not the headless build; a version bump that renames what the fix
    // reaches for must fail here rather than quietly drop Codex's history again.
    const term = new Browser({ cols: 80, rows: 24, scrollback: 1000, allowProposedApi: true });
    const fix = keepScrolledHistory(term);
    expect(fix.installed).toBe(true);
    await write(term, PROBE);
    expect(lines(term).slice(0, 4)).toEqual(["shell line 0", "shell line 1", "shell line 2", "viewport"]);
    fix.dispose();
    term.dispose();
  });

  it("stays out of the way on a build without them", () => {
    let registered = 0;
    const parser = { registerCsiHandler: () => ((registered += 1), { dispose: () => undefined }) };
    expect(keepScrolledHistory({ parser }).installed).toBe(false);
    expect(registered).toBe(0);
  });
});
