import { describe, expect, it } from "vitest";
import { InputDeduper } from "./dedupe";

/** Feeds `[data, at]` pairs and returns what got through, joined. */
function typed(events: [string, number][]): string {
  const d = new InputDeduper();
  return events.filter(([data, at]) => d.accept(data, at)).map(([data]) => data).join("");
}

describe("the doubled-input filter", () => {
  it("drops a word replayed whole after it was typed letter by letter", () => {
    expect(typed([["l", 0], ["s", 120], ["ls", 140], ["\r", 400]])).toBe("ls\r");
  });

  it("drops a chunk delivered twice in the same instant", () => {
    expect(typed([["git", 0], ["git", 3], [" ", 200]])).toBe("git ");
  });

  it("keeps a word typed again later, and a letter typed twice at a human pace", () => {
    expect(typed([["l", 0], ["s", 100], [" ", 200], ["ls", 900]])).toBe("ls ls");
    expect(typed([["o", 0], ["o", 90]])).toBe("oo");
    expect(typed([["l", 0], ["s", 100], ["ls", 500]])).toBe("lsls");
  });

  it("keeps a held key's repeats", () => {
    expect(typed([["a", 0], ["a", 33], ["a", 66], ["a", 99]])).toBe("aaaa");
  });

  it("never drops Enter, Backspace or an escape sequence, however fast", () => {
    expect(typed([["\r", 0], ["\r", 1], ["\x7f", 2], ["\x7f", 3], ["\x1b[A", 4], ["\x1b[A", 5]])).toBe("\r\r\x7f\x7f\x1b[A\x1b[A");
  });

  it("starts a new word after a space or Enter", () => {
    // "c", "d" typed, then a space: the replay check must not see "cd" as still pending.
    expect(typed([["c", 0], ["d", 50], [" ", 80], ["cd", 100]])).toBe("cd cd");
  });

  it("counts a multi-byte letter as one letter", () => {
    expect(typed([["п", 0], ["р", 100], ["пр", 130]])).toBe("пр");
  });

  it("forgets on reset", () => {
    const d = new InputDeduper();
    expect(d.accept("x", 0)).toBe(true);
    d.reset();
    expect(d.accept("x", 1)).toBe(true);
    expect(d.accept("", 2)).toBe(false);
  });
});
