// xterm.js must stay out of the page's entry bundle: a phone opening a chat should not download a
// terminal emulator it may never use. This walks the static imports from `main.tsx` through the source
// and fails if any of them reaches xterm.js, the terminal kit or the width table. A type-only import is
// erased at build time and does not count; `import("./kit")` is a separate chunk and is the only door.

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";

const GLOBBED = import.meta.glob("../**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true }) as Record<string, string>;

// Keys come back relative to this folder ("./kit.ts", "../main.tsx"); the walk wants one namespace,
// so every file is named relative to `src` instead ("terminal/kit.ts", "main.tsx").
const SOURCES: Record<string, string> = Object.fromEntries(
  Object.entries(GLOBBED).map(([key, text]) => [key.startsWith("./") ? `terminal/${key.slice(2)}` : key.replace(/^\.\.\//, ""), text]),
);

// `import x from "y"`, `import "y"`, `export … from "y"` — but not `import type`, `export type`, or `import("y")`.
const STATIC = /^\s*(?:import|export)\s+(?!type\b)(?:[^'";]*?\s+from\s+)?["']([^"']+)["']/gm;

function resolve(from: string, spec: string): string | null {
  if (!spec.startsWith(".")) return null;
  const parts = from.split("/").slice(0, -1);
  for (const part of spec.split("/")) {
    if (part === "..") parts.pop();
    else if (part !== ".") parts.push(part);
  }
  const base = parts.join("/");
  for (const candidate of [base, `${base}.ts`, `${base}.tsx`, `${base}/index.ts`]) if (candidate in SOURCES) return candidate;
  return null;
}

function staticClosure(entry: string): { files: Set<string>; packages: Set<string> } {
  const files = new Set<string>();
  const packages = new Set<string>();
  const queue = [entry];
  while (queue.length) {
    const file = queue.pop()!;
    if (files.has(file)) continue;
    files.add(file);
    for (const m of SOURCES[file].matchAll(STATIC)) {
      const spec = m[1];
      if (!spec.startsWith(".")) packages.add(spec);
      const next = resolve(file, spec);
      if (next) queue.push(next);
    }
  }
  return { files, packages };
}

describe("the entry bundle", () => {
  it("does not reach xterm.js, the terminal kit or the width table by a static import", () => {
    expect("main.tsx" in SOURCES).toBe(true);
    expect("terminal/kit.ts" in SOURCES).toBe(true);
    const { files, packages } = staticClosure("main.tsx");
    expect(files.size).toBeGreaterThan(20);
    expect([...packages].filter((p) => p.startsWith("@xterm/"))).toEqual([]);
    expect([...files].filter((f) => /terminal\/(kit|unicode|widths\.data)\.ts$/.test(f))).toEqual([]);
  });

  it("loads the kit only through the dynamic import in load.ts", () => {
    const importers = Object.entries(SOURCES).filter(([file, text]) => !file.endsWith(".test.ts") && /["']\.\/kit["']/.test(text)).map(([file]) => file);
    expect(importers).toEqual(["terminal/load.ts"]);
    expect(SOURCES["terminal/load.ts"]).toMatch(/import\("\.\/kit"\)/);
  });

  it("follows imports across folders", () => {
    expect(resolve("screens/Session.tsx", "../terminal/kit")).toBe("terminal/kit.ts");
    expect(resolve("main.tsx", "./terminal/load")).toBe("terminal/load.ts");
    expect(resolve("terminal/load.ts", "../api")).toBe("api.ts");
  });

  it("would notice a static import of xterm.js", () => {
    // The walker itself: a line that imports xterm.js statically is read as such.
    const sample = 'import { Terminal } from "@xterm/xterm";\nimport type { ITheme } from "@xterm/xterm";\nconst k = import("./kit");';
    const found = [...sample.matchAll(STATIC)].map((m) => m[1]);
    expect(found).toEqual(["@xterm/xterm"]);
  });
});
