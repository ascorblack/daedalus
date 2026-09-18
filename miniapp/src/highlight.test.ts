import { describe, expect, it } from "vitest";
import { highlight, highlightLines, langOf, tokenize } from "./highlight";

describe("syntax colours", () => {
  it.each([
    ["app.ts", "const x = 'hello'; // a comment", "const"],
    ["app.py", 'def greet():\n    """a long\n    string"""\n    return 42', "def"],
    ["query.sql", "SELECT name FROM items WHERE id = 42", "SELECT"],
    ["build.sh", "if true; then echo 'done'; fi", "if"],
    ["data.json", '{"ready":true,"count":42}', "true"],
  ])("preserves every byte of %s while marking its keywords", (file, source, keyword) => {
    const tokens = tokenize(source, langOf(file));
    expect(tokens.map((x) => x.text).join("")).toBe(source);
    expect(tokens.some((x) => x.cls === "k" && x.text === keyword)).toBe(true);
  });
  it("escapes markup, closes spans on each line and recognises tags", () => {
    const html = highlight('<img src="x" onerror="alert(1)">', "html");
    expect(html).not.toContain("<img");
    expect(html).toContain("tk-t");
    const lines = highlightLines("/* first\nsecond */", "js");
    expect(lines).toHaveLength(2);
    for (const line of lines) expect(line).toMatch(/^<span class="tk-c">.*<\/span>$/);
  });
});
