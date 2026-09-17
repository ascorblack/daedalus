// A half-translated screen is worse than an untranslated one: the reader cannot tell whether the
// English sentence in the middle of a Russian page is a gap or a term of art. So the dictionary is
// checked as a table — every key present in both languages, and neither column left as a copy of
// the other where a translation was meant.

import { describe, expect, it } from "vitest";
import { DICT, LANGS, setLang, t } from "./i18n";

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
