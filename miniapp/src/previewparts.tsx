// Rich views share the same downloaded bytes. Numbered source is bounded in the DOM so a long
// generated file does not make the conversation expensive just because its panel is open.

import { useEffect, useMemo, useRef, useState } from "react";
import { highlightLines, langOf, HIGHLIGHT_MAX_LINES } from "./highlight";
import { jsonRows, parseJsonText, toggleRow } from "./jsontree";
import { diffFileName, parseDiff } from "./diff";
import { t } from "./i18n";

export function SourceView({ text, name, range }: { text: string; name: string; range?: { from: number; to: number } | null }) {
  const [wrap, setWrap] = useState(false);
  const [jump, setJump] = useState(String(range?.from ?? 1));
  const [at, setAt] = useState(range?.from ?? 1);
  const [mark, setMark] = useState(range);
  const first = useRef<HTMLDivElement>(null);
  const lines = useMemo(() => highlightLines(text, text.split("\n").length > HIGHLIGHT_MAX_LINES ? "text" : langOf(name)), [text, name]);
  const start = Math.max(0, Math.min(lines.length - 1, at - 1) - 120);
  const end = Math.min(lines.length, start + 400);
  useEffect(() => { first.current?.scrollIntoView({ block: "center" }); }, [at, text]);
  return <>
    <div className="source-tools">
      <button className={`btn small ${wrap ? "on" : ""}`} aria-pressed={wrap} onClick={() => setWrap((v) => !v)}>{t("preview.wrap")}</button>
      <form onSubmit={(e) => { e.preventDefault(); const n = Math.max(1, Math.min(lines.length, Number(jump) || 1)); setAt(n); setMark({ from: n, to: n }); }}><input className="field" type="number" min="1" max={lines.length} aria-label={t("preview.goto")} value={jump} onChange={(e) => setJump(e.target.value)} /><button className="btn small" type="submit">{t("preview.goto")}</button></form>
      <span className="sub">{t("preview.lines.of", { from: start + 1, to: end, total: lines.length })}</span>
    </div>
    <div className={`filetext lined source-view ${wrap ? "wrap" : ""}`}>
      {start > 0 && <button className="linkbtn" onClick={() => setAt(Math.max(1, at - 280))}>{t("preview.previous")}</button>}
      {lines.slice(start, end).map((line, i) => { const n = start + i + 1; return <div key={n} data-line={n} className={`line ${mark && n >= mark.from && n <= mark.to ? "cited" : ""}`} ref={n === at ? first : undefined}><span className="linenum">{n}</span><span className="linebody" dangerouslySetInnerHTML={{ __html: line || " " }} /></div>; })}
      {end < lines.length && <button className="linkbtn" onClick={() => setAt(at + 280)}>{t("preview.next")}</button>}
    </div>
  </>;
}

export function JsonView({ text }: { text: string }) {
  const parsed = useMemo(() => parseJsonText(text), [text]);
  const [state, setState] = useState({ toggled: new Set<string>() });
  if ("error" in parsed) return <div className="empty">{parsed.error}</div>;
  const rows = jsonRows(parsed.value, state);
  return <div className="json-tree" role="tree" aria-label={t("preview.json")}>
    {rows.map((r) => <div key={r.id} className="json-row" role="treeitem" aria-level={r.depth + 1} aria-expanded={r.container ? r.open : undefined} style={{ paddingLeft: r.depth * 16 }}>
      {r.container ? <button className="linkbtn" onClick={() => setState((s) => toggleRow(s, r.id))} aria-label={`${r.key || t("preview.json")} (${r.count})`}><span aria-hidden>{r.open ? "⌄" : "›"}</span> {r.key && `${JSON.stringify(r.key)}: `}{r.kind === "array" ? `[${r.count}]` : `{${r.count}}`} <span className="sub">{r.text}</span></button> : <span>{r.key && `${JSON.stringify(r.key)}: `}<span className={`json-${r.kind}`}>{r.text}</span></span>}
    </div>)}
    {rows.length >= 5000 && <div className="sub">{t("preview.json.limit")}</div>}
  </div>;
}

export function DiffView({ text }: { text: string }) {
  const files = useMemo(() => parseDiff(text), [text]);
  return <div className="diff-view">{files.map((f, i) => <section key={i}>
    <div className="diff-file">{diffFileName(f)} <span className="tk-add">+{f.added}</span> <span className="tk-del">−{f.removed}</span></div>
    {f.hunks.map((h, j) => <div key={j}><div className="diff-hunk">{h.header}</div>{h.lines.map((l, k) => <div key={k} className={`diff-line diff-${l.type}`}><span className="linenum">{l.oldNo}</span><span className="linenum">{l.newNo}</span><span className="diff-sign">{l.type === "add" ? "+" : l.type === "del" ? "−" : " "}</span><span>{l.text}</span></div>)}</div>)}
    {!f.hunks.length && <pre className="filetext">{text}</pre>}
  </section>)}{!files.length && <pre className="filetext">{text}</pre>}</div>;
}

export function ImageView({ url, name }: { url: string; name: string }) {
  const [actual, setActual] = useState(false);
  const [size, setSize] = useState("");
  return <><div className="source-tools"><button className="btn small" aria-pressed={actual} onClick={() => setActual((v) => !v)}>{actual ? t("preview.fit") : "1:1"}</button><span className="sub">{size}</span></div><div className={`image-pan ${actual ? "actual" : ""}`}><img className="preview-image" src={url} alt={name} onLoad={(e) => setSize(`${e.currentTarget.naturalWidth} × ${e.currentTarget.naturalHeight} px`)} onClick={() => setActual((v) => !v)} /></div></>;
}
