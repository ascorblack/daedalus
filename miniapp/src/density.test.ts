// The stylesheet is read as a table of sizes, the way i18n.test.ts reads the dictionary as a table
// of words. The app once grew to 15 px body text, 44 px round buttons and 64 px session rows one
// rule at a time, each of them reasonable on its own; this is what stops the next one.
//
// Three claims. Every size comes from the scale in :root. Nothing outside the answer, the headings
// and the prose is drawn above 14 px. Rows and controls take their height from the row and control
// tokens, so a phone gets its 44 px from one media rule and not from forty.

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import css from "./styles.css?raw";

type Rule = { selector: string; media: string; body: string };

/** Every rule with the media query it sits in; comments stripped first, and one space after every colon
 *  in a declaration, so the two spacing styles the file is written in read as one. */
function rules(): Rule[] {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const out: Rule[] = [];
  const media: string[] = [];
  let i = 0;
  let start = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === "{") {
      const head = text.slice(start, i).trim();
      if (head.startsWith("@")) {
        media.push(head);
        i += 1;
        start = i;
        continue;
      }
      const end = text.indexOf("}", i);
      out.push({ selector: head, media: media.join(" "), body: text.slice(i + 1, end).replace(/\s*:\s*/g, ": ") });
      i = end + 1;
      start = i;
      continue;
    }
    if (ch === "}") {
      media.pop();
      i += 1;
      start = i;
      continue;
    }
    i += 1;
  }
  return out;
}

const TOKENS: Record<string, number> = { "--fs-11": 11, "--fs-12": 12, "--fs-13": 13, "--fs-14": 14, "--fs-15": 15, "--fs-16": 16, "--fs-18": 18, "--fs-mono": 12.5, "--fs-prose": 15 };

/** The pixels a font-size value stands for, or null when it is relative (em, %) and inherits its scale. */
function pixels(value: string): number | null {
  const v = value.trim();
  const token = /^var\((--fs-[a-z0-9]+)\)/.exec(v);
  if (token) return TOKENS[token[1]] ?? Number.POSITIVE_INFINITY;
  const clamp = /^clamp\(\s*([\d.]+)px/.exec(v);
  if (clamp) return Number(clamp[1]);
  const px = /^([\d.]+)px/.exec(v);
  return px ? Number(px[1]) : null;
}

/** Where text is allowed to be larger than the UI: running prose, headings, numbers meant to be read from across a room, and glyphs. */
const LARGE_ALLOWED = [".answer", ".summary-body", ".preview-doc", ".composer-box textarea", "h1", "h2", "h3", ".kpi .value", ".empty b", ".stat b", ".step-head b", ".dropzone", ".attachment-glyph", ".btn.big", ".chev", ".voice-", ".orb", ".onboard-head", ".login"];

const all = rules();

describe("the scale", () => {
  it("is declared once, in :root", () => {
    const root = all.find((r) => r.selector === ":root" && !r.media);
    expect(root).toBeDefined();
    for (const name of [...Object.keys(TOKENS), "--space-2", "--radius-sm", "--radius-md", "--radius-lg", "--row-h", "--row-h-dense", "--row-h-touch", "--ctl-h", "--ctl-h-sm", "--ctl-h-lg", "--avatar", "--sidebar-w", "--sidebar-strip", "--panel-w", "--reading-w", "--chat-w", "--head-h"]) {
      expect(root!.body, name).toContain(`${name}:`);
    }
  });

  it("gives a phone its touch sizes from one media rule", () => {
    const touch = all.find((r) => r.selector === ":root" && r.media.includes("max-width: 1023px"));
    expect(touch?.body).toContain("--row-h: var(--row-h-touch)");
    expect(touch?.body).toContain("--fs-prose: var(--fs-16)");
  });

  it("sets the body at 14", () => {
    const body = all.find((r) => r.selector === "body" && !r.media);
    expect(body?.body).toContain("font-size: var(--fs-14)");
  });
});

describe("every size", () => {
  it("is at or under 14 px outside prose, headings and glyphs", () => {
    const large: string[] = [];
    for (const r of all) {
      // A text field on a touch screen is 16 px on purpose: the alternative is Safari zooming the page.
      if (r.media.includes("hover: none")) continue;
      for (const m of r.body.matchAll(/(?:^|;)\s*font(?:-size)?\s*:\s*([^;]+)/g)) {
        const size = pixels(m[1]);
        if (size === null || size <= 14) continue;
        if (LARGE_ALLOWED.some((sel) => r.selector.includes(sel))) continue;
        large.push(`${r.selector} { font-size: ${m[1].trim()} }`);
      }
    }
    expect(large).toEqual([]);
  });

  it("names a step of the scale rather than a number", () => {
    // A size written as a number is a size the scale cannot move. Two are allowed: the phone's
    // 16 px field, and the 10 px beta tag, which sits below the scale on purpose.
    const numeric: string[] = [];
    for (const r of all) {
      if (r.media.includes("hover: none") || LARGE_ALLOWED.some((sel) => r.selector.includes(sel))) continue;
      for (const m of r.body.matchAll(/(?:^|;)\s*font-size\s*:\s*([\d.]+px)/g)) {
        if (m[1] === "10px") continue;
        numeric.push(`${r.selector} { font-size: ${m[1]} }`);
      }
    }
    expect(numeric).toEqual([]);
  });
});

describe("rows and controls", () => {
  const decl = (selector: string) => all.filter((r) => r.selector === selector && !r.media).map((r) => r.body).join(" ");

  it("take their height from the tokens", () => {
    expect(decl(".erow")).toContain("min-height: var(--row-h)");
    expect(decl(".iconbtn")).toContain("width: var(--ctl-h)");
    expect(decl(".iconbtn")).toContain("height: var(--ctl-h)");
    expect(decl(".iconbtn.small")).toContain("var(--ctl-h-sm)");
    expect(decl(".btn")).toContain("min-height: var(--ctl-h)");
    expect(decl(".menu button")).toContain("min-height: var(--row-h)");
    expect(decl(".erow .avatar")).toContain("var(--avatar)");
  });

  it("fold a step of the run into 28 px", () => {
    expect(decl(".act")).toContain("min-height: 28px");
  });

  it("cap the conversation at the stripe and the prose at the reading width", () => {
    const wide = all.filter((r) => r.media.includes("min-width: 1024px")).map((r) => `${r.selector}{${r.body}}`).join("\n");
    expect(wide).toMatch(/\.timeline, \.composer-box\{[^}]*var\(--chat-w\)/);
    expect(wide).toMatch(/\.answer[^{]*\{[^}]*max-width: var\(--reading-w\)/);
  });
});
