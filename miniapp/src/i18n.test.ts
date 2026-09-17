// A half-translated screen is worse than an untranslated one: the reader cannot tell whether the
// English sentence in the middle of a Russian page is a gap or a term of art. So the dictionary is
// checked as a table — every key present in both languages, and neither column left as a copy of
// the other where a translation was meant.

/// <reference types="vite/client" />
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DICT, LANGS, setLang, t } from "./i18n";

// Every source file under src/, as text. Read through the bundler rather than from disk: the app
// has no Node types, and importing the modules themselves would run code that wants a browser.
const SOURCES = import.meta.glob("./**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true }) as Record<string, string>;

/** The rail's destinations, read out of the router rather than imported. */
function screens(): string[] {
  const found = SOURCES["./router.ts"].match(/export const SCREENS: Screen\[\] = \[([^\]]+)\]/);
  if (!found) throw new Error("the router no longer lists its screens where this test looks");
  return [...found[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}

describe("the dictionary", () => {
  it("has every key in every language", () => {
    const missing: string[] = [];
    for (const [key, entry] of Object.entries(DICT)) {
      for (const lang of LANGS) {
        if (!entry[lang] || !entry[lang].trim()) missing.push(`${key}.${lang}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it("translates rather than repeating, outside the handful of words that are the same in both", () => {
    const same = Object.entries(DICT)
      .filter(([, entry]) => entry.en === entry.ru)
      .map(([key]) => key);
    // Names and brands stay as they are; everything else being identical means a row was copied.
    expect(same.sort()).toEqual(["lang.name.en", "lang.name.ru", "login.title"]);
  });

  it("keeps the same holes in both languages", () => {
    const holes = (text: string) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
    for (const [key, entry] of Object.entries(DICT)) {
      expect(holes(entry.ru), key).toEqual(holes(entry.en));
    }
  });
});

describe("the keys the code asks for", () => {
  it("are all in the table", () => {
    const missing = new Set<string>();
    for (const [path, text] of Object.entries(SOURCES)) {
      if (path.endsWith(".test.ts")) continue;
      for (const [, key] of text.matchAll(/\bt\("([^"]+)"/g)) {
        if (!(key in DICT)) missing.add(`${key} (${path})`);
      }
    }
    expect([...missing]).toEqual([]);
  });

  // The two keys built from a variable. A screen added to the rail without a name, or an effort
  // level without a word, shows its own key on the page.
  it("includes every key built from a name", () => {
    const missing = [
      ...screens().map((s) => `nav.${s}`),
      ...["work", "autonomy", "knowledge", "observe"].map((g) => `nav.group.${g}`),
      ...["low", "medium", "high"].map((e) => `add.effort.${e}`),
      ...LANGS.map((l) => `lang.name.${l}`),
    ].filter((key) => !(key in DICT));
    expect(missing).toEqual([]);
  });
});

// Which language a visit is in is decided once, as the module loads, and the order matters: the
// launcher names one in the address, and that has to beat both the reader's last choice here and
// the browser's own preference. The module reads the world at import time, so the world is built
// here first and the module imported after it.
describe("which language a visit is in", () => {
  const kept = new Map<string, string>();

  beforeEach(() => {
    vi.resetModules();
    kept.clear();
  });
  afterEach(() => vi.unstubAllGlobals());

  async function visit(search: string, browser: string, chosen?: string) {
    if (chosen) kept.set("daedalus.lang", chosen);
    const replaced: string[] = [];
    vi.stubGlobal("window", {
      location: { href: "http://127.0.0.1:8765/app/" + search, pathname: "/app/", search, hash: "" },
      history: { state: null, replaceState: (_s: unknown, _t: string, next: string) => replaced.push(next) },
    });
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => kept.get(key) ?? null,
      setItem: (key: string, value: string) => void kept.set(key, value),
    });
    vi.stubGlobal("navigator", { language: browser, languages: [browser] });
    const module = await import("./i18n");
    return { lang: module.lang(), replaced };
  }

  it("takes the language the address names, keeps it, and takes it out of the address", async () => {
    const visited = await visit("?lang=ru", "en-GB");
    expect(visited.lang).toBe("ru");
    expect(kept.get("daedalus.lang")).toBe("ru");
    expect(visited.replaced).toEqual(["/app/"]);
  });

  it("falls back to what the reader chose here, and then to the browser", async () => {
    expect((await visit("", "en-GB", "ru")).lang).toBe("ru");
    vi.resetModules();
    kept.clear();
    expect((await visit("", "ru-RU")).lang).toBe("ru");
    vi.resetModules();
    kept.clear();
    expect((await visit("", "en-GB")).lang).toBe("en");
  });
});

describe("t", () => {
  it("fills the holes and falls back to the key it does not know", () => {
    setLang("en");
    expect(t("add.pill.context", { n: "128k" })).toBe("128k context");
    setLang("ru");
    expect(t("add.pill.context", { n: "128k" })).toBe("контекст 128k");
    expect(t("nothing.like.this")).toBe("nothing.like.this");
    setLang("en");
  });
});
