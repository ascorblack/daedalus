// A shell's commands, marked in its terminal: a dot beside each command's prompt in the colour of how
// it ended, Ctrl+↑/↓ from one prompt to the next, and the text of the last command's output.
//
// The daemon reports each mark with an absolute row: counted from the terminal's start, so a row keeps
// its number while older ones leave the history. xterm.js numbers lines from the top of what it still
// holds, and that number changes as the history is trimmed. The two are tied by an anchor, an xterm.js
// marker whose absolute row is known: every snapshot starts the buffer at `first_abs_row`, so after
// the reset the anchor is line 0; after every write the anchor moves down with the output, before the
// trimming of a long history could take it away. A command's mark is itself a marker, so once it is
// placed xterm.js keeps it on its line, however the lines above it go.
//
// Everything here runs in stream order: the connection hands over a mark only after the bytes before
// it are parsed (`connection.ts`), so the rows it names exist in the buffer.

import type { IDecoration, IMarker } from "@xterm/xterm";
import type { CommandEvent, MarkItem, MarksEvent } from "./protocol";

/** How a command ended, as its dot shows it: running, zero, non-zero, or no status reported. */
export type CommandResult = "running" | "ok" | "failed" | "unknown";

/** What a view needs to know without reading the buffer: the last command and whether there is anywhere to jump. */
export type MarksSummary = {
  /** The newest command, or null when none has run since the terminal (or its snapshot) began. */
  last: { n: number; command: string; result: CommandResult; exitCode: number | null } | null;
  /** Whether a command has ended, so there is an output to copy. */
  ended: boolean;
  /** Prompts to jump between. */
  prompts: number;
  /** Whether the shell reports its commands at all (any mark was ever seen). */
  active: boolean;
};

/** The part of an xterm.js terminal this uses; `@xterm/headless` satisfies it too, for the tests. */
export interface MarkTerminal {
  readonly rows: number;
  readonly buffer: {
    readonly active: {
      readonly type: "normal" | "alternate";
      readonly baseY: number;
      readonly cursorY: number;
      readonly viewportY: number;
      readonly length: number;
      getLine(y: number): { readonly isWrapped: boolean; translateToString(trimRight?: boolean): string } | undefined;
    };
  };
  registerMarker(cursorYOffset?: number): IMarker;
  registerDecoration?(options: { marker: IMarker; x?: number; width?: number; layer?: "bottom" | "top" }): IDecoration | undefined;
  scrollToLine(line: number): void;
  scrollToBottom(): void;
}

type Mark = {
  n: number;
  command: string;
  exitCode: number | null;
  running: boolean;
  /** On the command's prompt row (its first output row when the shell marked no prompt). */
  marker: IMarker | null;
  decoration: IDecoration | null;
  element: HTMLElement | null;
  /** Rows from the marker to the first output row, and to one past the last (null while it runs). */
  outputOffset: number;
  endOffset: number | null;
  /** Absolute rows, kept to place the marker later when the buffer could not take it (the alternate screen). */
  anchorRow: number;
  /** The last jump went to it. */
  current: boolean;
};


export function resultOf(exitCode: number | null, running: boolean): CommandResult {
  if (running) return "running";
  if (exitCode === null) return "unknown";
  return exitCode === 0 ? "ok" : "failed";
}

function row(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

export class CommandMarks {
  private marks = new Map<number, Mark>();
  /** The current prompt (a command has not started from it yet): a place to jump to, without a dot. */
  private prompt: { row: number; marker: IMarker | null } | null = null;
  private anchor: { marker: IMarker; row: number } | null = null;
  /** The first row of the next snapshot, from the `resync` before it. */
  private nextFirst = 0;
  /** Where the last jump went: the next one continues from there while the view has not moved. */
  private jumped: { line: number; viewportY: number } | null = null;
  private seen = false;
  private summaryValue: MarksSummary = { last: null, ended: false, prompts: 0, active: false };

  constructor(private readonly term: MarkTerminal, private onChange: () => void = () => undefined) {}

  get summary(): MarksSummary {
    return this.summaryValue;
  }

  /** Whether any mark was ever seen: a terminal whose shell reports nothing keeps Ctrl+↑/↓ for its program. */
  get active(): boolean {
    return this.seen;
  }

  // ── the stream ──────────────────────────────────────────────────────────────────────────

  /** The `resync` before a snapshot: the row the snapshot's first line has. */
  resync(firstAbsRow: number): void {
    this.nextFirst = row(firstAbsRow) ?? 0;
  }

  /**
   * The terminal was just reset for a snapshot: every mark was on a screen that is gone, and the
   * buffer's line 0 is about to be the snapshot's first row. The `marks` after it place them again.
   */
  reset(): void {
    this.clear();
    this.anchor = { marker: this.term.registerMarker(-(this.term.buffer.active.baseY + this.term.buffer.active.cursorY)), row: this.nextFirst };
    this.publish();
  }

  /** After every parsed write: keep the anchor near the cursor, and place what could not be placed. */
  parsed(): void {
    const buffer = this.term.buffer.active;
    if (!this.anchor || buffer.type !== "normal") return;
    if (this.anchor.marker.isDisposed) {
      // More output than the whole history arrived in one write: the rows can no longer be tied to
      // lines until the next snapshot or prompt says where they are.
      this.anchor = null;
      return;
    }
    const cursor = buffer.baseY + buffer.cursorY;
    // Moved once it is a screen above the cursor: a history of any length is trimmed from its top,
    // and an anchor kept within a screen of the output survives any write shorter than the history.
    if (cursor - this.anchor.marker.line > this.term.rows) {
      const moved = this.term.registerMarker(0);
      const at = this.anchor.row + (cursor - this.anchor.marker.line);
      this.anchor.marker.dispose();
      this.anchor = { marker: moved, row: at };
    }
    this.placeMissing();
  }

  apply(event: CommandEvent | MarksEvent): void {
    this.seen = true;
    if (event.type === "marks") this.applyList(event);
    else if (event.phase === "prompt") this.applyPrompt(event);
    else if (event.phase === "start") this.applyStart(event);
    else if (event.phase === "end") this.applyEnd(event);
    this.publish();
  }

  /** Records from the host (`GET …/commands`) after a reattach that brought no snapshot and so no marks. */
  fill(records: { n: number; command?: string; exit_code: number | null; prompt_row: number | null; output_row: number; end_row: number | null }[]): void {
    if (!records.length) return;
    this.seen = true;
    for (const r of records) this.upsert({ n: r.n, command: r.command ?? "", exit_code: r.exit_code, prompt_row: r.prompt_row, output_row: r.output_row, end_row: r.end_row, running: r.end_row === null });
    this.publish();
  }

  dispose(): void {
    this.clear();
    this.onChange = () => undefined;
  }

  // ── what the view asks ──────────────────────────────────────────────────────────────────

  /** Scroll to the previous (`-1`) or next (`1`) prompt. False when there is none that way. */
  jump(direction: -1 | 1): boolean {
    const buffer = this.term.buffer.active;
    if (buffer.type !== "normal") return false;
    const lines = this.promptLines();
    if (!lines.length) return false;
    const atBottom = buffer.viewportY >= buffer.baseY;
    // Continue from the last jump while the view is still where it put it: a prompt already on the
    // last screen cannot be scrolled to the top, and starting from the viewport would find it again.
    // From the bottom, the prompt being typed at is where "previous" starts: the first Ctrl+↑ goes to
    // the command that ran last, not to the line the cursor is on.
    let from: number;
    if (this.jumped && this.jumped.viewportY === buffer.viewportY) from = this.jumped.line;
    else if (atBottom) from = direction < 0 ? this.livePromptLine() ?? buffer.baseY + buffer.cursorY : buffer.length;
    else from = buffer.viewportY;
    const target = direction < 0 ? [...lines].reverse().find((l) => l < from) : lines.find((l) => l > from);
    if (target === undefined) {
      if (direction > 0 && !atBottom) {
        this.term.scrollToBottom();
        this.jumped = null;
        this.highlight(null);
        return true;
      }
      return false;
    }
    this.term.scrollToLine(target);
    this.jumped = { line: target, viewportY: this.term.buffer.active.viewportY };
    // A prompt already on the last screen cannot scroll to the top, so its dot says where the jump went.
    this.highlight(target);
    return true;
  }

  private livePromptLine(): number | null {
    const marker = this.prompt?.marker;
    return marker && !marker.isDisposed ? marker.line : null;
  }

  private highlight(line: number | null): void {
    for (const mark of this.marks.values()) {
      mark.current = line !== null && !!mark.marker && mark.marker.line === line;
      this.restyle(mark);
    }
  }

  /**
   * The output of the newest command that has ended, as the buffer holds it: its rows joined, a
   * wrapped row continuing the one before it, trailing blank lines dropped. `null` when no command
   * has ended; `undefined` when one has but its rows are no longer here (scrolled out of the history,
   * or never placed) — the caller asks the host, which kept more.
   */
  lastOutput(): { n: number; text: string } | null | undefined {
    const ended = [...this.marks.values()].filter((m) => !m.running).sort((a, b) => b.n - a.n)[0];
    if (!ended) return null;
    if (!ended.marker || ended.marker.isDisposed || ended.endOffset === null) return undefined;
    const buffer = this.term.buffer.active;
    if (buffer.type !== "normal") return undefined;
    const from = ended.marker.line + ended.outputOffset;
    const to = ended.marker.line + ended.endOffset;
    let text = "";
    for (let y = from; y < to && y < buffer.length; y++) {
      const line = buffer.getLine(y);
      if (!line) continue;
      if (y > from && !line.isWrapped) text += "\n";
      text += line.translateToString(true);
    }
    return { n: ended.n, text: text.replace(/[\s]+$/, "") };
  }

  /** The buffer lines of every mark's prompt, in order; the checks read them to see where a jump went. */
  promptLines(): number[] {
    const lines: number[] = [];
    for (const m of this.marks.values()) if (m.marker && !m.marker.isDisposed) lines.push(m.marker.line);
    if (this.prompt?.marker && !this.prompt.marker.isDisposed) lines.push(this.prompt.marker.line);
    return [...new Set(lines)].sort((a, b) => a - b);
  }

  /** Every mark with its line and result, oldest first (for the checks' debug hook). */
  describe(): { n: number; line: number; result: CommandResult }[] {
    return [...this.marks.values()]
      .sort((a, b) => a.n - b.n)
      .map((m) => ({ n: m.n, line: m.marker && !m.marker.isDisposed ? m.marker.line : -1, result: resultOf(m.exitCode, m.running) }));
  }

  // ── inside ──────────────────────────────────────────────────────────────────────────────

  private applyList(event: MarksEvent): void {
    // The list describes the same point of the stream as the snapshot before it; anything this
    // terminal still holds from before the snapshot was cleared by `reset`.
    for (const item of Array.isArray(event.list) ? event.list : []) this.upsert(item);
    const promptRow = row(event.prompt_row);
    const taken = [...this.marks.values()].some((m) => m.anchorRow === promptRow);
    this.setPrompt(promptRow !== null && !taken ? promptRow : null);
  }

  private applyPrompt(event: CommandEvent): void {
    const at = row(event.abs_row);
    if (at === null) return;
    if (!this.anchor) this.calibrate(at);
    this.setPrompt(at);
  }

  private applyStart(event: CommandEvent): void {
    const n = row(event.n);
    const output = row(event.abs_row);
    if (n === null || output === null || this.marks.has(n)) return;
    const promptRow = row(event.prompt_row);
    this.upsert({ n, command: event.command ?? "", exit_code: null, prompt_row: promptRow, output_row: output, end_row: null, running: true });
    // The prompt it started from is now the command's own mark.
    if (this.prompt && (this.prompt.row === promptRow || promptRow === null)) this.setPrompt(null);
  }

  private applyEnd(event: CommandEvent): void {
    const n = row(event.n);
    const output = row(event.abs_row);
    if (n === null || output === null) return;
    const exit = typeof event.exit_code === "number" ? event.exit_code : null;
    this.upsert({ n, command: event.command ?? "", exit_code: exit, prompt_row: row(event.prompt_row), output_row: output, end_row: row(event.end_row) ?? output, running: false });
  }

  /** Add a command or bring one up to date; `n` names it, so the same report twice changes nothing. */
  private upsert(item: MarkItem): void {
    const n = row(item.n);
    const output = row(item.output_row);
    if (n === null || output === null) return;
    const promptRow = row(item.prompt_row);
    const anchorRow = promptRow !== null && promptRow <= output ? promptRow : output;
    const end = row(item.end_row);
    const running = !!item.running && end === null;
    const known = this.marks.get(n);
    if (known) {
      known.running = running;
      known.exitCode = running ? null : typeof item.exit_code === "number" ? item.exit_code : null;
      if (end !== null) known.endOffset = Math.max(known.outputOffset, end - known.anchorRow);
      if (item.command && !known.command) known.command = item.command;
      this.restyle(known);
      return;
    }
    const mark: Mark = {
      n,
      command: item.command ?? "",
      exitCode: running ? null : typeof item.exit_code === "number" ? item.exit_code : null,
      running,
      marker: null,
      decoration: null,
      element: null,
      current: false,
      outputOffset: output - anchorRow,
      endOffset: end === null ? null : Math.max(output - anchorRow, end - anchorRow),
      anchorRow,
    };
    this.marks.set(n, mark);
    this.place(mark);
  }

  private setPrompt(at: number | null): void {
    if (this.prompt?.marker) this.prompt.marker.dispose();
    this.prompt = at === null ? null : { row: at, marker: this.markerAt(at) };
  }

  /** Without an anchor (lost to a flood), a prompt is taken to be on the cursor's row: it is the last thing a shell prints. */
  private calibrate(at: number): void {
    const buffer = this.term.buffer.active;
    if (buffer.type !== "normal") return;
    this.anchor = { marker: this.term.registerMarker(0), row: at };
  }

  /** A marker on the line that has absolute row `at`, or null when that line is not in the buffer. */
  private markerAt(at: number): IMarker | null {
    const buffer = this.term.buffer.active;
    if (!this.anchor || this.anchor.marker.isDisposed || buffer.type !== "normal") return null;
    const line = this.anchor.marker.line + (at - this.anchor.row);
    const cursor = buffer.baseY + buffer.cursorY;
    if (line < 0 || line > cursor + 1) return null;
    const marker = this.term.registerMarker(line - cursor);
    return marker && !marker.isDisposed ? marker : null;
  }

  private place(mark: Mark): void {
    if (mark.marker) return;
    mark.marker = this.markerAt(mark.anchorRow);
    if (!mark.marker) return;
    mark.decoration = this.term.registerDecoration?.({ marker: mark.marker, x: 0, width: 1, layer: "top" }) ?? null;
    mark.decoration?.onRender((element) => {
      mark.element = element;
      this.style(mark, element);
    });
  }

  private placeMissing(): void {
    let placed = false;
    for (const mark of this.marks.values()) {
      if (mark.marker) continue;
      this.place(mark);
      placed ||= !!mark.marker;
    }
    if (this.prompt && !this.prompt.marker) this.prompt.marker = this.markerAt(this.prompt.row);
    if (placed) this.publish();
  }

  private restyle(mark: Mark): void {
    if (mark.element) this.style(mark, mark.element);
  }

  private style(mark: Mark, element: HTMLElement): void {
    element.classList.add("term-mark");
    element.dataset.result = resultOf(mark.exitCode, mark.running);
    element.dataset.n = String(mark.n);
    element.classList.toggle("current", mark.current);
    element.title = mark.command;
  }

  private clear(): void {
    for (const mark of this.marks.values()) {
      mark.decoration?.dispose();
      mark.marker?.dispose();
    }
    this.marks.clear();
    this.prompt?.marker?.dispose();
    this.prompt = null;
    this.anchor?.marker.dispose();
    this.anchor = null;
    this.jumped = null;
  }

  private publish(): void {
    const newest = [...this.marks.values()].sort((a, b) => b.n - a.n)[0];
    const last = newest ? { n: newest.n, command: newest.command, result: resultOf(newest.exitCode, newest.running), exitCode: newest.exitCode } : null;
    const prompts = this.promptLines().length;
    const ended = [...this.marks.values()].some((m) => !m.running);
    const previous = this.summaryValue;
    if (
      previous.active === this.seen &&
      previous.ended === ended &&
      previous.prompts === prompts &&
      previous.last?.n === last?.n &&
      previous.last?.result === last?.result &&
      previous.last?.exitCode === last?.exitCode
    )
      return;
    this.summaryValue = { last, ended, prompts, active: this.seen };
    this.onChange();
  }
}
