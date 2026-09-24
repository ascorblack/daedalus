// The command marks against a real xterm.js buffer (@xterm/headless): the rows the daemon names land
// on the right lines after a snapshot, through a history that is being trimmed, and back from the
// alternate screen; the jumps go from prompt to prompt; the last output is the text between a
// command's start and end marks.

import { Terminal } from "@xterm/headless";
import { describe, expect, it } from "vitest";
import { CommandMarks, MarkTerminal } from "./marks";
import type { CommandEvent, MarksEvent } from "./protocol";

function setup(rows = 6, scrollback = 1000) {
  const term = new Terminal({ cols: 40, rows, scrollback, allowProposedApi: true });
  let changes = 0;
  const marks = new CommandMarks(term as unknown as MarkTerminal, () => changes++);
  const write = (data: string) =>
    new Promise<void>((resolve) =>
      term.write(data, () => {
        marks.parsed();
        resolve();
      }),
    );
  return { term, marks, write, changes: () => changes };
}

const prompt = (row: number): CommandEvent => ({ type: "command", phase: "prompt", abs_row: row });
const start = (n: number, row: number, promptRow: number | null, command = `cmd${n}`): CommandEvent => ({ type: "command", phase: "start", n, command, abs_row: row, prompt_row: promptRow });
const end = (n: number, row: number, endRow: number, exit: number | null, promptRow: number | null = null): CommandEvent => ({ type: "command", phase: "end", n, exit_code: exit, abs_row: row, end_row: endRow, prompt_row: promptRow });

/** A shell session as the daemon marks it: a prompt, the command typed, its output, how it ended. `row` is the cursor's absolute row. */
async function run(h: ReturnType<typeof setup>, at: { row: number }, n: number, command: string, output: string[], exit: number | null) {
  h.marks.apply(prompt(at.row));
  const promptRow = at.row;
  await h.write(`$ ${command}\r\n`);
  at.row++;
  h.marks.apply(start(n, at.row, promptRow, command));
  const outputRow = at.row;
  for (const line of output) {
    await h.write(`${line}\r\n`);
    at.row++;
  }
  h.marks.apply(end(n, outputRow, at.row, exit, promptRow));
}

function lineText(term: Terminal, y: number): string {
  return term.buffer.active.getLine(y)?.translateToString(true) ?? "";
}

describe("command marks", () => {
  it("marks each command on its prompt line with how it ended", async () => {
    const h = setup(24);
    h.marks.resync(0);
    h.marks.reset();
    const at = { row: 0 };
    await run(h, at, 1, "ls", ["a.txt", "b.txt"], 0);
    await run(h, at, 2, "false", [], 1);
    await run(h, at, 3, "sleep 1", [], null);
    h.marks.apply(prompt(at.row));
    await h.write("$ ");
    expect(h.marks.describe()).toEqual([
      { n: 1, line: 0, result: "ok" },
      { n: 2, line: 3, result: "failed" },
      { n: 3, line: 4, result: "unknown" },
    ]);
    expect(lineText(h.term, 0)).toBe("$ ls");
    expect(lineText(h.term, 3)).toBe("$ false");
    expect(h.marks.summary).toMatchObject({ active: true, ended: true, prompts: 4, last: { n: 3, result: "unknown" } });
  });

  it("shows a running command until it ends, and the same report twice changes nothing", async () => {
    const h = setup(24);
    h.marks.resync(0);
    h.marks.reset();
    h.marks.apply(prompt(0));
    await h.write("$ make\r\n");
    h.marks.apply(start(1, 1, 0, "make"));
    h.marks.apply(start(1, 1, 0, "make"));
    expect(h.marks.summary.last).toMatchObject({ n: 1, result: "running" });
    expect(h.marks.summary.ended).toBe(false);
    expect(h.marks.lastOutput()).toBeNull();
    await h.write("built\r\n");
    h.marks.apply(end(1, 1, 2, 2, 0));
    h.marks.apply(end(1, 1, 2, 2, 0));
    expect(h.marks.describe()).toEqual([{ n: 1, line: 0, result: "failed" }]);
    expect(h.marks.lastOutput()).toEqual({ n: 1, text: "built" });
  });

  it("places a command whose start was never seen from its end", async () => {
    const h = setup(24);
    h.marks.resync(0);
    h.marks.reset();
    await h.write("$ true\r\nok\r\n");
    h.marks.apply(end(7, 1, 2, 0, 0));
    expect(h.marks.describe()).toEqual([{ n: 7, line: 0, result: "ok" }]);
    expect(h.marks.lastOutput()).toEqual({ n: 7, text: "ok" });
  });

  it("copies the last finished command's output, joining a wrapped row to the one before it", async () => {
    const h = setup(10);
    h.marks.resync(0);
    h.marks.reset();
    const at = { row: 0 };
    await run(h, at, 1, "echo one", ["one"], 0);
    // 50 characters in 40 columns: one line of output on two rows.
    const long = "x".repeat(50);
    h.marks.apply(prompt(at.row));
    await h.write("$ echo long\r\n");
    at.row++;
    h.marks.apply(start(2, at.row, at.row - 1, "echo long"));
    await h.write(`${long}\r\nlast   \r\n\r\n`);
    at.row += 4;
    h.marks.apply(end(2, at.row - 4, at.row, 0));
    // A command that is still running does not count: its output is not the last output yet.
    h.marks.apply(prompt(at.row));
    await h.write("$ tail -f log\r\n");
    h.marks.apply(start(3, at.row + 1, at.row, "tail -f log"));
    await h.write("streaming\r\n");
    expect(h.marks.lastOutput()).toEqual({ n: 2, text: `${long}\nlast` });
  });

  it("places marks after a snapshot by the snapshot's first row", async () => {
    const h = setup(6);
    // The snapshot starts at row 1000 of the terminal's life: ten lines of history and a prompt.
    h.marks.resync(1000);
    h.marks.reset();
    const lines = Array.from({ length: 10 }, (_, i) => (i === 2 ? "$ make" : i === 6 ? "$ ls" : `line ${i}`));
    await h.write(`${lines.join("\r\n")}\r\n$ `);
    const list: MarksEvent = {
      type: "marks",
      first_abs_row: 1000,
      prompt_row: 1010,
      list: [
        { n: 40, command: "make", exit_code: 2, prompt_row: 1002, output_row: 1003, end_row: 1006, running: false },
        { n: 41, command: "ls", exit_code: 0, prompt_row: 1006, output_row: 1007, end_row: 1010, running: false },
      ],
    };
    h.marks.apply(list);
    expect(h.marks.describe()).toEqual([
      { n: 40, line: 2, result: "failed" },
      { n: 41, line: 6, result: "ok" },
    ]);
    expect(h.marks.lastOutput()).toEqual({ n: 41, text: "line 7\nline 8\nline 9" });
    expect(h.marks.promptLines()).toEqual([2, 6, 10]);
  });

  it("keeps rows and lines together while the history is trimmed", async () => {
    const h = setup(5, 40);
    h.marks.resync(0);
    h.marks.reset();
    const at = { row: 0 };
    await run(h, at, 1, "first", ["gone soon"], 0);
    // Far more than the 45 lines the terminal keeps, in many writes: the history is trimmed under
    // the marks the whole time.
    for (let i = 0; i < 30; i++) await h.write("noise\r\n".repeat(10));
    at.row += 300;
    await run(h, at, 2, "second", ["kept"], 1);
    await run(h, at, 3, "third", ["also kept", "and this"], 0);
    const described = h.marks.describe();
    expect(described[0]).toEqual({ n: 1, line: -1, result: "ok" });
    expect(lineText(h.term, described[1].line)).toBe("$ second");
    expect(lineText(h.term, described[2].line)).toBe("$ third");
    expect(h.marks.lastOutput()).toEqual({ n: 3, text: "also kept\nand this" });
  });

  it("follows the history being trimmed with no prompt to go by", async () => {
    // A shell that marks commands but not prompts: every row is placed through the anchor alone.
    const h = setup(5, 40);
    h.marks.resync(0);
    h.marks.reset();
    await h.write("$ one\r\n");
    h.marks.apply(start(1, 1, null, "one"));
    for (let i = 0; i < 30; i++) await h.write("noise\r\n".repeat(10));
    h.marks.apply(end(1, 1, 301, 0));
    await h.write("$ two\r\nresult\r\n");
    h.marks.apply(start(2, 302, null, "two"));
    h.marks.apply(end(2, 302, 303, 0));
    const two = h.marks.describe()[1];
    expect(lineText(h.term, two.line)).toBe("result");
    expect(h.marks.lastOutput()).toEqual({ n: 2, text: "result" });
  });

  it("says the rows are gone when the last command's output left the history", async () => {
    const h = setup(5, 10);
    h.marks.resync(0);
    h.marks.reset();
    const at = { row: 0 };
    await run(h, at, 1, "cat big", Array.from({ length: 40 }, (_, i) => `row ${i}`), 0);
    expect(h.marks.lastOutput()).toBeUndefined();
  });

  it("waits for the normal screen to place marks listed while the alternate screen is up", async () => {
    const h = setup(6);
    h.marks.resync(0);
    h.marks.reset();
    await h.write("$ make\r\nok\r\n$ vim\r\n\x1b[?1049h\x1b[Hediting");
    h.marks.apply({ type: "marks", first_abs_row: 0, prompt_row: 2, list: [{ n: 1, command: "make", exit_code: 0, prompt_row: 0, output_row: 1, end_row: 2, running: false }, { n: 2, command: "vim", exit_code: null, prompt_row: 2, output_row: 3, end_row: null, running: true }] });
    expect(h.marks.describe().map((m) => m.line)).toEqual([-1, -1]);
    await h.write("\x1b[?1049l");
    expect(h.marks.describe()).toEqual([
      { n: 1, line: 0, result: "ok" },
      { n: 2, line: 2, result: "running" },
    ]);
  });

  it("jumps from prompt to prompt, starting from the command that ran last", async () => {
    const h = setup(5);
    h.marks.resync(0);
    h.marks.reset();
    const at = { row: 0 };
    for (let n = 1; n <= 4; n++) await run(h, at, n, `step ${n}`, Array.from({ length: 6 }, (_, i) => `out ${n}.${i}`), 0);
    h.marks.apply(prompt(at.row));
    await h.write("$ ");
    const lines = h.marks.promptLines();
    expect(lines).toEqual([0, 7, 14, 21, 28]);
    const viewport = () => h.term.buffer.active.viewportY;
    expect(viewport()).toBe(h.term.buffer.active.baseY);
    expect(h.marks.jump(-1)).toBe(true);
    expect(viewport()).toBe(21);
    expect(h.marks.jump(-1)).toBe(true);
    expect(viewport()).toBe(14);
    expect(h.marks.jump(-1)).toBe(true);
    expect(h.marks.jump(-1)).toBe(true);
    expect(viewport()).toBe(0);
    expect(h.marks.jump(-1)).toBe(false);
    expect(h.marks.jump(1)).toBe(true);
    expect(viewport()).toBe(7);
    // Past the last prompt, the next jump is back to the bottom.
    h.term.scrollToLine(22);
    expect(h.marks.jump(1)).toBe(true);
    expect(viewport()).toBe(h.term.buffer.active.baseY);
    expect(h.marks.jump(1)).toBe(false);
  });

  it("does nothing for a shell that reports no commands", async () => {
    const h = setup(5);
    h.marks.resync(0);
    h.marks.reset();
    await h.write("plain output\r\n");
    expect(h.marks.active).toBe(false);
    expect(h.marks.jump(-1)).toBe(false);
    expect(h.marks.lastOutput()).toBeNull();
  });

  it("forgets the old screen's marks at a reset", async () => {
    const h = setup(24);
    h.marks.resync(0);
    h.marks.reset();
    const at = { row: 0 };
    await run(h, at, 1, "ls", ["x"], 0);
    h.term.reset();
    h.marks.resync(50);
    h.marks.reset();
    expect(h.marks.describe()).toEqual([]);
    expect(h.marks.summary.last).toBeNull();
  });
});
