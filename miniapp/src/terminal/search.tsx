// Find in a terminal's buffer, scrollback included: next and previous, case, regular expression, whole
// word. The bar sits over the terminal's top edge and gives focus back to the terminal when it closes.

import { useEffect, useRef, useState } from "react";
import type { ISearchOptions, SearchAddon } from "@xterm/addon-search";
import { t } from "../i18n";
import { Icon } from "../icons";

type Flags = { caseSensitive: boolean; regex: boolean; wholeWord: boolean };

function options(flags: Flags): ISearchOptions {
  // Only the ruler colours are required for the result count to be reported; the highlight itself
  // uses the warn colour so a match reads against both schemes.
  return {
    ...flags,
    decorations: { matchBackground: "#e8b44c55", matchOverviewRuler: "#e8b44c", activeMatchBackground: "#e8b44c", activeMatchColorOverviewRuler: "#f5cf7a" },
  };
}

export function TerminalSearch({ search, onClose }: { search: SearchAddon; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [flags, setFlags] = useState<Flags>({ caseSensitive: false, regex: false, wholeWord: false });
  const [result, setResult] = useState<{ index: number; count: number } | null>(null);
  const field = useRef<HTMLInputElement>(null);

  useEffect(() => {
    field.current?.focus();
    field.current?.select();
    const sub = search.onDidChangeResults((r) => setResult({ index: r.resultIndex, count: r.resultCount }));
    return () => {
      sub.dispose();
      search.clearDecorations();
    };
  }, [search]);

  useEffect(() => {
    if (!query) {
      search.clearDecorations();
      setResult(null);
      return;
    }
    try {
      search.findNext(query, { ...options(flags), incremental: true });
    } catch {
      // An unfinished regular expression while it is being typed; the next key completes it.
    }
  }, [query, flags, search]);

  const step = (forward: boolean) => {
    if (!query) return;
    try {
      if (forward) search.findNext(query, options(flags));
      else search.findPrevious(query, options(flags));
    } catch {
      /* as above */
    }
  };

  const toggle = (key: keyof Flags) => setFlags((f) => ({ ...f, [key]: !f[key] }));
  const count = result && query ? (result.count === 0 ? t("term.search.none") : t("term.search.count", { n: result.index + 1, total: result.count })) : "";

  return (
    <div className="term-search" role="search" onKeyDown={(e) => e.stopPropagation()}>
      <input
        ref={field}
        className="term-search-field"
        value={query}
        placeholder={t("term.search.placeholder")}
        aria-label={t("term.search.placeholder")}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            step(!e.shiftKey);
          } else if (e.key === "Escape") {
            e.preventDefault();
            onClose();
          }
        }}
      />
      <span className="term-search-count num" aria-live="polite">{count}</span>
      <button className={`term-flag ${flags.caseSensitive ? "on" : ""}`} aria-pressed={flags.caseSensitive} onClick={() => toggle("caseSensitive")} title={t("term.search.case")} aria-label={t("term.search.case")}>Aa</button>
      <button className={`term-flag ${flags.wholeWord ? "on" : ""}`} aria-pressed={flags.wholeWord} onClick={() => toggle("wholeWord")} title={t("term.search.word")} aria-label={t("term.search.word")}>ab</button>
      <button className={`term-flag ${flags.regex ? "on" : ""}`} aria-pressed={flags.regex} onClick={() => toggle("regex")} title={t("term.search.regex")} aria-label={t("term.search.regex")}>.*</button>
      <button className="iconbtn small flat" onClick={() => step(false)} title={t("term.search.previous")} aria-label={t("term.search.previous")}><Icon name="up" size={14} /></button>
      <button className="iconbtn small flat" onClick={() => step(true)} title={t("term.search.next")} aria-label={t("term.search.next")}><Icon name="down" size={14} /></button>
      <button className="iconbtn small flat" onClick={onClose} title={t("common.close")} aria-label={t("common.close")}><Icon name="close" size={14} /></button>
    </div>
  );
}
