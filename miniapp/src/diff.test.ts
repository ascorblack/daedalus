import { describe, expect, it } from "vitest";
import { diffFileName, looksLikeDiff, parseDiff } from "./diff";

describe("unified diffs", () => {
  it("numbers both sides and does not invent a context line from the final newline", () => {
    const files = parseDiff("--- a/a.py\n+++ b/a.py\n@@ -2,2 +2,2 @@\n keep\n-old\n+new\n\\ No newline at end of file\n");
    expect(files[0].added).toBe(1);
    expect(files[0].removed).toBe(1);
    expect(files[0].hunks[0].lines.map((l) => [l.oldNo, l.newNo])).toEqual([[2, 2], [3, null], [null, 3], [null, null]]);
  });
  it("separates consecutive files without git headers and keeps header-like payload lines", () => {
    const files = parseDiff("--- a/one\n+++ b/one\n@@ -1 +1 @@\n--- old\n+++ new\n--- a/two\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n");
    expect(files).toHaveLength(2);
    expect(diffFileName(files[1])).toBe("two");
    expect(files[0].hunks[0].lines[0].text).toBe("-- old");
  });
  it("recognises a diff carried by a receipt and a headerless hunk", () => {
    expect(looksLikeDiff("check output\n@@ -0,0 +1 @@\n+hello\n")).toBe(true);
    expect(parseDiff("@@ -0,0 +1 @@\n+hello\n")[0].added).toBe(1);
    expect(looksLikeDiff("ordinary output")).toBe(false);
  });
});
