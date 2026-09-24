import { Terminal } from "@xterm/headless";
import { describe, expect, it } from "vitest";
import { onlyQueries, swallowQueries } from "./queries";

const ESC = "\x1b";

async function replies(input: string, swallowing: boolean): Promise<string[]> {
  const term = new Terminal({ cols: 80, rows: 24, allowProposedApi: true, vtExtensions: { kittyKeyboard: true } });
  if (swallowing) swallowQueries(term.parser);
  const out: string[] = [];
  term.onData((d) => out.push(d));
  await new Promise<void>((resolve) => term.write(input, resolve));
  term.dispose();
  return out;
}

// Every query xterm.js itself answers, so the test proves both that it would and that it no longer does.
const QUERIES: [string, string][] = [
  ["DA1", `${ESC}[c`],
  ["DA1 with zero", `${ESC}[0c`],
  ["DA2", `${ESC}[>c`],
  ["XTVERSION", `${ESC}[>q`],
  ["DSR status", `${ESC}[5n`],
  ["CPR", `${ESC}[6n`],
  ["DEC CPR", `${ESC}[?6n`],
  ["DECRQM ANSI", `${ESC}[4$p`],
  ["DECRQM DEC", `${ESC}[?2004$p`],
  ["kitty keyboard", `${ESC}[?u`],
  ["DECRQSS margins", `${ESC}P$qr${ESC}\\`],
  ["DECRQSS cursor style", `${ESC}P$q q${ESC}\\`],
];

describe("query swallowing", () => {
  for (const [name, sequence] of QUERIES) {
    it(`keeps xterm.js from answering ${name}`, async () => {
      expect((await replies(sequence, false)).length).toBeGreaterThan(0);
      expect(await replies(sequence, true)).toEqual([]);
    });
  }

  it("leaves everything else alone: text, colours, modes and the cursor style still apply", async () => {
    const term = new Terminal({ cols: 40, rows: 5, allowProposedApi: true });
    swallowQueries(term.parser);
    await new Promise<void>((resolve) => term.write(`${ESC}[31mred${ESC}[0m ${ESC}[?2004h${ESC}[2 q${ESC}[22t${ESC}]133;A${ESC}\\ok`, resolve));
    expect(term.buffer.active.getLine(0)!.translateToString(true)).toBe("red ok");
    expect(term.modes.bracketedPasteMode).toBe(true);
    expect(term.buffer.active.getLine(0)!.getCell(0)!.getFgColor()).toBe(1);
    term.dispose();
  });

  it("swallows an OSC colour sequence only when it asks and sets nothing", () => {
    expect(onlyQueries(11, "?")).toBe(true);
    expect(onlyQueries(10, "?;?")).toBe(true);
    expect(onlyQueries(11, "rgb:00/00/00")).toBe(false);
    expect(onlyQueries(10, "?;rgb:ff/ff/ff")).toBe(false);
    expect(onlyQueries(4, "1;?;2;?")).toBe(true);
    expect(onlyQueries(4, "1;?;2;#ff0000")).toBe(false);
    expect(onlyQueries(4, "1")).toBe(false);
    expect(onlyQueries(11, "")).toBe(false);
  });

  it("can be undone", async () => {
    const term = new Terminal({ cols: 80, rows: 24, allowProposedApi: true });
    const handle = swallowQueries(term.parser);
    handle.dispose();
    const out: string[] = [];
    term.onData((d) => out.push(d));
    await new Promise<void>((resolve) => term.write(`${ESC}[6n`, resolve));
    expect(out).toEqual([`${ESC}[1;1R`]);
    term.dispose();
  });
});
