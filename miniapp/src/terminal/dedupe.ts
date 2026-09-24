// A filter for the doubled input some Android keyboards produce in a terminal.
//
// xterm.js reads a phone's keyboard through a hidden text field. Gboard (and keyboards built the same
// way) deliver a word twice there: once letter by letter as it is typed, and once more whole when the
// composition ends, and sometimes a chunk arrives from both the composition and the `input` event in
// the same instant. A shell cannot tell a replay from typing, so "ls" runs as "lsls". The filter drops:
// - a chunk identical to the one before it that arrives within `sameMs` — no hand repeats a word, or
//   even a letter, that fast (a held key repeats at 30 ms or slower);
// - a chunk that spells exactly the letters typed since the last word boundary, arriving within
//   `replayMs` of the last of them — the word replayed on composition end.
// Control input (Enter, Backspace, escape sequences) always passes: it is never composed, and a doubled
// Enter would be worse to lose than to keep.

export type DedupeOptions = { sameMs: number; replayMs: number };

export const DEFAULT_DEDUPE: DedupeOptions = { sameMs: 25, replayMs: 60 };

const isControl = (data: string) => /^[\x00-\x1f\x7f]/.test(data);
const BOUNDARY = /[\s\x00-\x1f\x7f]/;

export class InputDeduper {
  private last = "";
  private lastAt = -Infinity;
  /** Printable letters typed one at a time since the last word boundary. */
  private word = "";

  constructor(private readonly options: DedupeOptions = DEFAULT_DEDUPE) {}

  /** Whether `data`, arriving at `now` (ms), should reach the terminal. */
  accept(data: string, now: number): boolean {
    if (!data) return false;
    const since = now - this.lastAt;
    if (!isControl(data)) {
      if (data === this.last && since < this.options.sameMs) return false;
      if (data.length > 1 && data === this.word && since < this.options.replayMs) {
        this.lastAt = now;
        return false;
      }
    }
    this.remember(data, now);
    return true;
  }

  /** Forget everything: the terminal lost focus, or the keyboard was switched. */
  reset(): void {
    this.last = "";
    this.lastAt = -Infinity;
    this.word = "";
  }

  private remember(data: string, now: number): void {
    this.last = data;
    this.lastAt = now;
    if (isControl(data) || BOUNDARY.test(data[data.length - 1])) this.word = "";
    else if ([...data].length === 1) this.word += data;
    else this.word = "";
  }
}
