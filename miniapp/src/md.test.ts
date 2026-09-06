import { describe, expect, it } from "vitest";
import { renderMarkdown } from "./md";

describe("renderMarkdown", () => {
  it("escapes markup from the model", () => {
    const html = renderMarkdown('<img src=x onerror=alert(1)> **b** [x](javascript:alert(1)) `<b>`');
    expect(html).not.toContain("<img");
    expect(html).toContain("<b>b</b>");
    expect(html).not.toContain('href="javascript');
    expect(html).toContain("<code>&lt;b&gt;</code>");
  });
  it("survives a header row followed by a bare rule", () => {
    expect(() => renderMarkdown("| a | b |\n---\nrest")).not.toThrow();
    expect(renderMarkdown("| a | b |\n---\nrest")).toContain("<hr/>");
  });
  it("renders tables, lists, code and details", () => {
    const html = renderMarkdown("| a | b |\n|---|--:|\n| 1 | 2 |\n\n- x\n  - y\n\n1. z\n\n```py\nprint(1)\n```\n<details><summary>s</summary>\n\nhidden\n\n</details>");
    expect(html).toContain("<table>");
    expect(html).toContain('<th style="text-align:right">b</th>');
    expect(html).toContain("<ul><li>x<ul><li>y</li></ul></li></ul>");
    expect(html).toContain("<ol><li>z</li></ol>");
    expect(html).toContain("codecard");
    expect(html).toContain("<details><summary>s</summary>");
  });
  it("closes unbalanced details", () => {
    const html = renderMarkdown("<details><summary>s</summary>\ntext");
    expect((html.match(/<\/details>/g) ?? []).length).toBe(1);
  });
});
