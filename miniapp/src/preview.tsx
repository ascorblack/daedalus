// File previews: images, Markdown, CSV, PDF, Word, Excel, audio/video and plain text — in a dialog,
// for workspace files and for attachments waiting in the composer. Everything is fetched with the
// auth header (an <img src> cannot carry one) and shown from a blob URL; the office formats are
// converted in the browser by libraries loaded only when such a file is opened.

import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { Icon } from "./icons";
import { renderMarkdown } from "./md";
import { errorText, fmtBytes } from "./ui";

/** Where the bytes come from: a file in a session's workspace, or a File object from the composer. */
export type PreviewSource = { sessionId: string; path: string } | { file: File };

export type PreviewKind = "image" | "markdown" | "csv" | "pdf" | "docx" | "sheet" | "audio" | "video" | "text" | "html" | "other";

const IMAGE_RE = /\.(png|jpe?g|gif|webp|bmp|svg|avif|ico)$/i;
const TEXT_RE = /\.(txt|log|json|jsonl|ya?ml|toml|ini|cfg|conf|env|py|ts|tsx|js|jsx|mjs|cjs|sh|bash|zsh|sql|xml|rs|go|java|kt|c|h|cpp|hpp|cs|rb|php|swift|css|scss|less|diff|patch|lock|gitignore|dockerfile|makefile|tex|bib|srt|vtt)$/i;

export function previewKind(name: string): PreviewKind {
  const n = name.toLowerCase();
  if (IMAGE_RE.test(n)) return "image";
  if (/\.(md|markdown|mdx)$/.test(n)) return "markdown";
  if (/\.(csv|tsv)$/.test(n)) return "csv";
  if (/\.pdf$/.test(n)) return "pdf";
  if (/\.docx$/.test(n)) return "docx";
  if (/\.(xlsx|xlsm|xls|ods)$/.test(n)) return "sheet";
  if (/\.(mp3|wav|ogg|oga|m4a|flac|aac|opus)$/.test(n)) return "audio";
  if (/\.(mp4|webm|mov|m4v|ogv)$/.test(n)) return "video";
  if (/\.(html?|htm)$/.test(n)) return "html";
  if (TEXT_RE.test(n) || !/\.[a-z0-9]{1,8}$/.test(n)) return "text";
  return "other";
}

export function canPreview(name: string): boolean {
  return previewKind(name) !== "other";
}

/** An emoji for a file row, by what the file is. */
export function fileGlyph(name: string, dir = false): string {
  if (dir) return "📁";
  switch (previewKind(name)) {
    case "image":
      return "🖼";
    case "pdf":
      return "📕";
    case "docx":
      return "📝";
    case "sheet":
    case "csv":
      return "📊";
    case "audio":
      return "🎵";
    case "video":
      return "🎬";
    case "markdown":
      return "📘";
    default:
      return "📄";
  }
}

function sourceName(src: PreviewSource): string {
  return "file" in src ? src.file.name : src.path.split("/").pop() || src.path;
}

async function sourceBlob(src: PreviewSource): Promise<Blob> {
  if ("file" in src) return src.file;
  const res = await fetch(`/api/sessions/${src.sessionId}/download?path=${encodeURIComponent(src.path)}`, { headers: api.authHeaders() });
  if (!res.ok) throw new Error(res.status === 404 ? "no such file" : `could not load the file (${res.status})`);
  return res.blob();
}

/** A blob URL for the source, revoked when the component that asked for it goes away. */
export function useBlobUrl(src: PreviewSource | null): { url: string | null; blob: Blob | null; error: string | null } {
  const [state, setState] = useState<{ url: string | null; blob: Blob | null; error: string | null }>({ url: null, blob: null, error: null });
  const key = src === null ? "" : "file" in src ? `file:${src.file.name}:${src.file.size}:${src.file.lastModified}` : `${src.sessionId}:${src.path}`;
  useEffect(() => {
    if (!src) return;
    let url: string | null = null;
    let gone = false;
    setState({ url: null, blob: null, error: null });
    sourceBlob(src)
      .then((blob) => {
        if (gone) return;
        url = URL.createObjectURL(blob);
        setState({ url, blob, error: null });
      })
      .catch((e) => !gone && setState({ url: null, blob: null, error: errorText(e) }));
    return () => {
      gone = true;
      if (url) URL.revokeObjectURL(url);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

/** An image fetched with the auth header. */
export function AuthImg({ src, alt, className, onClick }: { src: PreviewSource; alt: string; className?: string; onClick?: () => void }) {
  const { url, error } = useBlobUrl(src);
  if (error) return <span className="sub">{alt}: {error}</span>;
  if (!url) return <span className={`${className ?? ""} img-loading`} aria-busy="true" />;
  return <img className={className} src={url} alt={alt} onClick={onClick} style={onClick ? { cursor: "zoom-in" } : undefined} />;
}

// ── parsers ────────────────────────────────────────────────────────────────────────────

/** CSV/TSV with quoted fields; the delimiter is guessed from the first line. */
export function parseCsv(text: string, max = 2000): string[][] {
  const first = text.split(/\r?\n/, 1)[0] ?? "";
  const delimiter = [",", ";", "\t", "|"].map((d) => [d, (first.match(new RegExp(`\\${d}`, "g")) ?? []).length] as const).sort((a, b) => b[1] - a[1])[0][0];
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        cell += '"';
        i++;
      } else if (c === '"') quoted = false;
      else cell += c;
      continue;
    }
    if (c === '"') quoted = true;
    else if (c === delimiter) {
      row.push(cell);
      cell = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
      if (rows.length >= max) return rows;
    } else cell += c;
  }
  if (cell !== "" || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

function Grid({ rows, note }: { rows: string[][]; note?: string }) {
  if (!rows.length) return <div className="empty">empty</div>;
  const width = Math.max(...rows.map((r) => r.length));
  return (
    <div className="tablewrap preview-grid">
      <table>
        <thead>
          <tr>
            <th className="num">#</th>
            {Array.from({ length: width }, (_, i) => <th key={i}>{rows[0][i] ?? ""}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.slice(1).map((r, i) => (
            <tr key={i}>
              <td className="num sub">{i + 1}</td>
              {Array.from({ length: width }, (_, j) => <td key={j}>{r[j] ?? ""}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      {note && <div className="sub" style={{ padding: "6px 0" }}>{note}</div>}
    </div>
  );
}

// ── the dialog ─────────────────────────────────────────────────────────────────────────

const TEXT_LIMIT = 512_000;

export function FilePreview({ src, onClose }: { src: PreviewSource; onClose: () => void }) {
  const name = sourceName(src);
  const kind = previewKind(name);
  const { url, blob, error } = useBlobUrl(src);
  const [body, setBody] = useState<{ html?: string; text?: string; rows?: string[][]; sheets?: { name: string; rows: string[][] }[]; error?: string } | null>(null);
  const [sheet, setSheet] = useState(0);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    if (!blob) return;
    let gone = false;
    const done = (b: typeof body) => !gone && setBody(b);
    (async () => {
      try {
        if (kind === "markdown" || kind === "text" || kind === "csv" || kind === "html") {
          const text = await blob.slice(0, TEXT_LIMIT).text();
          const clipped = blob.size > TEXT_LIMIT ? `\n\n[… ${fmtBytes(blob.size - TEXT_LIMIT)} more — download the file for the rest]` : "";
          if (kind === "markdown") done({ html: renderMarkdown(text + clipped) });
          else if (kind === "csv") {
            const rows = parseCsv(text);
            done({ rows, error: rows.length >= 2000 ? "showing the first 2000 rows" : undefined });
          } else done({ text: text + clipped });
        } else if (kind === "docx") {
          const mammoth = await import("mammoth");
          const r = await mammoth.convertToHtml({ arrayBuffer: await blob.arrayBuffer() });
          done({ html: r.value });
        } else if (kind === "sheet") {
          const XLSX = await import("xlsx");
          const wb = XLSX.read(await blob.arrayBuffer(), { type: "array" });
          const sheets = wb.SheetNames.map((n) => {
            const rows = XLSX.utils.sheet_to_json<unknown[]>(wb.Sheets[n], { header: 1, raw: false, defval: "" }) as unknown[][];
            return { name: n, rows: rows.slice(0, 2000).map((r) => r.map((c) => (c === null || c === undefined ? "" : String(c)))) };
          });
          done({ sheets });
        } else done({});
      } catch (e) {
        done({ error: errorText(e) });
      }
    })();
    return () => {
      gone = true;
    };
  }, [blob, kind]);

  const downloadName = useMemo(() => name, [name]);
  const failure = error ?? body?.error;
  const loading = !failure && (!url || (kind !== "image" && kind !== "pdf" && kind !== "audio" && kind !== "video" && kind !== "other" && !body));

  return (
    <div className="sheet-backdrop preview-backdrop" onClick={onClose}>
      <div className={`sheet preview ${kind}`} onClick={(e) => e.stopPropagation()} role="dialog" aria-label={name}>
        <div className="sheet-head">
          <h3 className="preview-name" title={"file" in src ? name : src.path}>
            <span aria-hidden>{fileGlyph(name)}</span> {name}
            {blob && <span className="sub"> · {fmtBytes(blob.size)}</span>}
          </h3>
          <div className="head-actions">
            {url && (
              <a className="iconbtn small" href={url} download={downloadName} title="Download" aria-label="download">
                <Icon name="download" size={16} />
              </a>
            )}
            <button className="iconbtn small" onClick={onClose} aria-label="close" title="Close">
              <Icon name="close" size={16} />
            </button>
          </div>
        </div>
        {body?.sheets && body.sheets.length > 1 && (
          <div className="segmented preview-tabs">
            {body.sheets.map((s, i) => (
              <button key={s.name} className={i === sheet ? "on" : ""} onClick={() => setSheet(i)}>{s.name}</button>
            ))}
          </div>
        )}
        <div className="sheet-body preview-body">
          {failure && <div className="empty">{failure}</div>}
          {loading && <div className="empty">Loading…</div>}
          {!failure && url && kind === "image" && <img className="preview-image" src={url} alt={name} />}
          {!failure && url && kind === "pdf" && <iframe className="preview-frame" src={url} title={name} />}
          {!failure && url && kind === "audio" && <audio className="preview-media" controls src={url} />}
          {!failure && url && kind === "video" && <video className="preview-media" controls src={url} />}
          {!failure && body?.html && kind === "markdown" && <div className="answer preview-doc" dangerouslySetInnerHTML={{ __html: body.html }} />}
          {!failure && body?.html && kind === "docx" && <div className="answer preview-doc docx" dangerouslySetInnerHTML={{ __html: body.html }} />}
          {!failure && body?.text !== undefined && kind === "html" && <pre className="filetext">{body.text}</pre>}
          {!failure && body?.text !== undefined && kind === "text" && <pre className="filetext">{body.text}</pre>}
          {!failure && body?.rows && <Grid rows={body.rows} note={body.error} />}
          {!failure && body?.sheets && <Grid rows={body.sheets[sheet]?.rows ?? []} note={(body.sheets[sheet]?.rows.length ?? 0) >= 2000 ? "showing the first 2000 rows" : undefined} />}
          {!failure && url && kind === "other" && (
            <div className="empty">
              No preview for this type.
              <div style={{ marginTop: 10 }}>
                <a className="btn small" href={url} download={downloadName}>download {fmtBytes(blob?.size ?? 0)}</a>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
