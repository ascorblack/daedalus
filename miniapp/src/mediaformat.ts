import type { MediaPresentation } from "./api";

export const MEDIA_REF = /!\[[^\]\n]{0,500}\]\(daedalus-media:([0-9a-f-]{36})\)/g;

export type AnswerPart = { kind: "text"; text: string } | { kind: "media"; presentation: MediaPresentation };

export function splitMediaAnswer(text: string, presentations: MediaPresentation[]): AnswerPart[] {
  const byId = new Map(presentations.map((item) => [item.id, item]));
  const parts: AnswerPart[] = [];
  let at = 0;
  for (const match of text.matchAll(MEDIA_REF)) {
    const presentation = byId.get(match[1]);
    if (!presentation || match.index === undefined) continue;
    const before = text.slice(at, match.index).trim();
    if (before) parts.push({ kind: "text", text: before });
    parts.push({ kind: "media", presentation });
    at = match.index + match[0].length;
  }
  const after = text.slice(at).trim();
  if (after) parts.push({ kind: "text", text: after });
  return parts.length ? parts : [{ kind: "text", text }];
}

export function mediaCopyText(text: string, presentations: MediaPresentation[]): string {
  const labels = new Map(presentations.map((p) => [p.id, p.items.map((i) => i.caption || i.alt || i.filename).join(", ")]));
  return text.replace(MEDIA_REF, (_, id: string) => labels.get(id) ? `[${labels.get(id)}]` : "").replace(/\n{3,}/g, "\n\n").trim();
}
