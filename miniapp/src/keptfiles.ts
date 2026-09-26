// The files orchestration keeps by handle (`att:<id>`): wherever a chat names one — the operator's
// attachment, an event card, an orchestrator's answer — the app draws it as a file card that
// downloads and previews. The text keeps the handle, which is what the models pass on; the card is
// the operator's way to the bytes, whichever machine they came from.

/** A handle as the host writes it into a message: `att:` and twelve hex characters. */
export const HANDLE_RE = /\batt:([0-9a-f]{12})\b/g;

export type KeptFile = { id: string; handle: string; name: string; mime: string; size: number; sha256: string; origin: string; created_at: string };

/** The ids of the handles a text names, in order, each once. */
export function handleIds(text: string | null | undefined): string[] {
  const seen: string[] = [];
  for (const match of (text ?? "").matchAll(HANDLE_RE)) if (!seen.includes(match[1])) seen.push(match[1]);
  return seen;
}

// The host's list of an orchestrator's attachments: a header line, then `- att:<id> <name> (…)` per
// file (or `- <name>: not kept — …`). The bubble shows the operator's own words; the cards below it
// show the files, so the list is not printed twice.
const HEADER_RE = /^Attached files \(kept by handle[^\n]*\):\s*$/;
const LINE_RE = /^- (att:[0-9a-f]{12} .*|[^\n]+: not kept — .*)$/;

/** The operator's text without the host's list of the files it carried; anything else is left. */
export function withoutAttachedList(text: string): string {
  const lines = text.split("\n");
  const at = lines.findIndex((line) => HEADER_RE.test(line));
  if (at < 0) return text;
  let end = at + 1;
  while (end < lines.length && LINE_RE.test(lines[end])) end++;
  const refused = lines.slice(at + 1, end).filter((line) => !line.startsWith("- att:"));
  return [...lines.slice(0, at), ...refused, ...lines.slice(end)].join("\n").trim();
}

/** The query that names the files of some ids, or null when there are none. */
export function filesKey(ids: string[]): string | null {
  return ids.length ? `/api/files?ids=${ids.join(",")}` : null;
}

/** Where a kept file's bytes are served, in the shape the preview and the download links take. */
export function keptBase(id: string): string {
  return `/api/files/${id}`;
}
