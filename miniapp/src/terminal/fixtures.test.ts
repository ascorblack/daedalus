// The terminal daemon and the browser read one byte stream, so they must agree about what it means.
// These read the daemon's own test data and hold @xterm/headless (the build the app pins, with the
// Ghostty width provider and the history fix the app installs) to it:
//
// - every snapshot the daemon's emulator writes, replayed into a fresh xterm.js, gives the screen and
//   the cursor the daemon's conformance cases expect — a snapshot is how a browser that attaches late
//   or after a resize learns the screen, so a snapshot xterm.js reads differently is a wrong screen;
// - every query the daemon answers, xterm.js answers the same way, except where the fixture records
//   a deliberate difference and why;
// - the queries the app swallows are exactly the ones the daemon answers, so no program ever gets two
//   replies or none;
// - a real Codex session, whose transcript lives in the terminal's history, keeps the same history
//   live in the browser as the daemon's snapshot restores.
//
// The widths themselves are held to Ghostty's grids in `unicode.test.ts`.

import { Terminal } from "@xterm/headless";
import { describe, expect, it } from "vitest";
import snapshots from "../../../ptyd/internal/emulator/ghostty/testdata/snapshots.json";
import cases from "../../../ptyd/internal/emulator/conformance/testdata/cases.json";
import replies from "../../../ptyd/internal/answer/testdata/xterm-replies.json";
import codex from "./testdata/codex-turns.json";
import { keepScrolledHistory } from "./history";
import { CSI_QUERIES, OSC_COLOURS, WINDOW_REPORTS, swallowQueries } from "./queries";
import { GHOSTTY_UNICODE_VERSION, ghosttyUnicode } from "./unicode";

type Snapshot = { name: string; cols: number; rows: number; vt: string; text: string[] };
type Case = { name: string; cursor?: number[] };
type Reply = { name: string; setup: string; query: string; cols: number; rows: number; reply: string; ptyd?: string; source?: string; px_w?: number };
type Recording = {
  cols: number;
  rows: number;
  events: string[][];
  checkpoints: { cp: string; cols: number; rows: number; snapshot: string }[];
};

/** A terminal configured as the app's view configures its own (`instance.ts`). */
function terminal(cols: number, rows: number, options: { history?: boolean; scrollback?: number } = {}): Terminal {
  const term = new Terminal({ cols, rows, scrollback: options.scrollback ?? 10_000, allowProposedApi: true, convertEol: false });
  term.unicode.register(ghosttyUnicode);
  term.unicode.activeVersion = GHOSTTY_UNICODE_VERSION;
  if (options.history !== false) expect(keepScrolledHistory(term).installed).toBe(true);
  return term;
}

function write(term: Terminal, data: string | Uint8Array): Promise<void> {
  return new Promise((resolve) => term.write(data, resolve));
}

function bytes(base64: string): Uint8Array {
  return Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
}

/** The whole buffer, history first, trailing blanks trimmed and trailing empty lines dropped. */
function lines(term: Terminal): string[] {
  const buffer = term.buffer.active;
  const out: string[] = [];
  for (let y = 0; y < buffer.length; y++) out.push(buffer.getLine(y)!.translateToString(true));
  while (out.length && out[out.length - 1] === "") out.pop();
  return out;
}

/**
 * The buffer as the daemon's conformance text writes it: a soft-wrapped row joined to the row it
 * continues, and trailing blanks trimmed even where they carry a colour (the daemon's text is
 * characters only; the colours are checked by the daemon's own runs).
 */
function logicalLines(term: Terminal): string[] {
  const buffer = term.buffer.active;
  const out: string[] = [];
  for (let y = 0; y < buffer.length; y++) {
    const line = buffer.getLine(y)!;
    // Trimmed only of cells never written: a wide character that did not fit leaves one such cell
    // before the wrap, which Ghostty's text does not have.
    const text = line.translateToString(true);
    if (line.isWrapped && out.length) out[out.length - 1] += text;
    else out.push(text);
  }
  const trimmed = out.map((l) => l.replace(/ +$/, ""));
  while (trimmed.length && trimmed[trimmed.length - 1] === "") trimmed.pop();
  return trimmed;
}

describe("the daemon's snapshots, read by xterm.js", () => {
  const byName = new Map((cases as Case[]).map((c) => [c.name, c]));
  for (const snap of snapshots as Snapshot[]) {
    it(`restores "${snap.name}"`, async () => {
      const term = terminal(snap.cols, snap.rows);
      await write(term, snap.vt);
      // A snapshot writes the history too; the conformance text is the whole of it.
      expect(logicalLines(term)).toEqual(snap.text);
      const expected = byName.get(snap.name);
      expect(expected, "every snapshot has its conformance case").toBeDefined();
      if (expected!.cursor) {
        const buffer = term.buffer.active;
        // While a wrap is pending xterm.js keeps the cursor one past the margin, where xterm and
        // Ghostty keep it on the last column (the replies fixture records the same quirk for CPR).
        // The next character lands on the next row either way.
        expect([Math.min(buffer.cursorX, snap.cols - 1), buffer.cursorY]).toEqual(expected!.cursor);
      }
      term.dispose();
    });
  }
});

async function reply(query: Reply, swallowing: boolean): Promise<string> {
  // The fixture was recorded with the character-size report turned on (the app's view leaves it off,
  // since the daemon answers it); the other window reports are answered by neither xterm.js build.
  const term = new Terminal({
    cols: query.cols,
    rows: query.rows,
    allowProposedApi: true,
    vtExtensions: { kittyKeyboard: true },
    windowOptions: { getWinSizeChars: true },
  });
  await write(term, query.setup);
  if (swallowing) swallowQueries(term.parser);
  const out: string[] = [];
  const listener = term.onData((d) => out.push(d));
  await write(term, query.query);
  listener.dispose();
  term.dispose();
  return out.join("");
}

describe("the daemon's answers, against xterm.js", () => {
  const all = (replies as { cases: Reply[] }).cases;
  // Recorded from the browser build rather than the headless one: the colours come from the app's
  // theme and the version string from @xterm/xterm, neither of which the headless build has.
  const headlessCanAnswer = (c: Reply) => c.source === undefined && c.name !== "XTVERSION";

  for (const query of all.filter(headlessCanAnswer)) {
    it(`xterm.js answers ${query.name} as recorded`, async () => {
      expect(await reply(query, false)).toBe(query.reply);
    });
  }

  it("records a reason wherever the daemon answers differently", () => {
    for (const query of all) {
      if (query.ptyd === undefined) continue;
      expect((query as { why?: string }).why, query.name).toBeTruthy();
    }
  });

  for (const query of all) {
    it(`an attached browser stays silent on ${query.name}`, async () => {
      expect(await reply(query, true)).toBe("");
    });
  }

  it("swallows no family of queries the daemon does not answer", () => {
    // Each swallowed CSI form must have at least one case in the daemon's fixture, or the app would
    // silence a question nobody answers and the program would wait for ever.
    const queries = all.map((c) => c.query);
    for (const id of CSI_QUERIES) {
      const pattern = new RegExp(`^\\x1b\\[${escape(id.prefix ?? "")}[0-9;]*${escape(id.intermediates ?? "")}${escape(id.final)}$`);
      expect(queries.some((q) => pattern.test(q)), JSON.stringify(id)).toBe(true);
    }
    for (const op of WINDOW_REPORTS) {
      if (op === 11 || op === 13 || op === 15 || op === 19 || op === 20) continue; // answered as xterm.js does: not at all
      expect(queries.some((q) => q === `\x1b[${op}t`), `XTWINOPS ${op}`).toBe(true);
    }
    for (const ident of OSC_COLOURS) {
      expect(queries.some((q) => q.startsWith(`\x1b]${ident};`)), `OSC ${ident}`).toBe(true);
    }
    expect(queries.some((q) => q.startsWith("\x1bP$q")), "DECRQSS").toBe(true);
  });
});

function escape(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\$]/g, "\\$&");
}

describe("a real Codex session's history", () => {
  const recording = codex as Recording;

  async function play(history: boolean): Promise<Map<string, string[]>> {
    const term = terminal(recording.cols, recording.rows, { history });
    const seen = new Map<string, string[]>();
    for (const [kind, data] of recording.events) {
      if (kind === "o") await write(term, bytes(data));
      else if (kind === "r") {
        const [cols, rows] = data.split("x").map(Number);
        term.resize(cols, rows);
      } else if (kind === "m") seen.set(data, lines(term));
    }
    seen.set("end", lines(term));
    term.dispose();
    return seen;
  }

  async function restored(snapshot: string, cols: number, rows: number): Promise<string[]> {
    const term = terminal(cols, rows);
    await write(term, bytes(snapshot));
    const out = lines(term);
    term.dispose();
    return out;
  }

  // The stub numbers its answers by request, and Codex makes requests of its own between turns, so
  // the lines are counted whatever turn number they carry: sixty per answer, three answers.
  const answers = (text: string[]) => text.filter((l) => /Line \d{3} of turn \d+:/.test(l)).length;

  it("keeps every answered line live, as the daemon's snapshot restores it", async () => {
    const live = await play(true);
    for (const cp of recording.checkpoints) {
      const restore = await restored(cp.snapshot, cp.cols, cp.rows);
      // A resize reflows xterm.js's history by its own policy and the daemon's by Ghostty's, and the
      // two policies differ in where they rewrap. Before any resize the two must agree line for line.
      if (cp.cp === "idle" || cp.cp.startsWith("turn-1") || cp.cp.startsWith("turn-2")) {
        expect(live.get(cp.cp), cp.cp).toEqual(restore);
      }
      // After it, what matters is that no answer went missing on either side.
      expect(answers(live.get(cp.cp)!), cp.cp).toBe(answers(restore));
    }
    expect(answers(live.get("turn-1")!)).toBe(60);
    expect(answers(live.get("turn-2")!)).toBe(120);
    expect(answers(live.get("end")!)).toBe(180);
  });

  it("is what stock xterm.js loses, so the comparison has teeth", async () => {
    const stock = await play(false);
    const end = stock.get("end")!;
    expect(answers(end)).toBeLessThan(180);
  });
});
