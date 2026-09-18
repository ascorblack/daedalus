import { describe, expect, it } from "vitest";
import { jsonRows, parseJsonText, toggleRow } from "./jsontree";

describe("folding JSON", () => {
  it("keeps scalars, empty containers and nested arrays distinguishable", () => {
    const rows = jsonRows({ a: [1, null, false, "x"], b: {} }, { toggled: new Set() });
    expect(rows.map((r) => r.kind)).toEqual(["object", "array", "number", "null", "boolean", "string", "object"]);
    expect(rows.at(-1)?.count).toBe(0);
    expect(rows.at(-1)?.open).toBe(false);
  });
  it("folds independently when keys contain dots, slashes, tildes or empty strings", () => {
    const data = { "a.b": [1], a: { b: [2] }, "": [3], "a/b": [4], "a~b": [5] };
    const state = { toggled: new Set<string>() };
    const rows = jsonRows(data, state);
    expect(new Set(rows.map((r) => r.id)).size).toBe(rows.length);
    const folded = jsonRows(data, toggleRow(state, "/a.b"));
    expect(folded.some((r) => r.id === "/a.b/0")).toBe(false);
    expect(folded.some((r) => r.id === "/a/b")).toBe(true);
    expect(jsonRows(data, state, 3)).toHaveLength(3);
  });
  it("accepts JSON Lines and reports broken input", () => {
    expect(parseJsonText('{"a":1}\n{"a":2}')).toEqual({ value: [{ a: 1 }, { a: 2 }] });
    expect(parseJsonText("null")).toEqual({ value: null });
    expect(parseJsonText("{broken")).toHaveProperty("error");
  });
});
