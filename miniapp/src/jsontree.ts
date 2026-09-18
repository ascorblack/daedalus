// A JSON document as rows a reader can fold: one row per value, the containers with a count and a
// preview when closed. The viewer draws the rows; this file decides what they are.

export type JsonKind = "object" | "array" | "string" | "number" | "boolean" | "null";

export type JsonRow = {
  /** The path as a key: "" for the root, JSON Pointer segments under it. */
  id: string;
  key: string;
  depth: number;
  kind: JsonKind;
  /** A scalar's text, or a closed container's preview. */
  text: string;
  /** How many entries a container holds. */
  count: number;
  open: boolean;
  container: boolean;
};

export function kindOf(v: unknown): JsonKind {
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  return typeof v === "object" ? "object" : typeof v === "string" ? "string" : typeof v === "number" ? "number" : "boolean";
}

/** How much of a closed container is shown on its row. */
const PREVIEW_MAX = 60;

export function previewOf(v: unknown): string {
  const kind = kindOf(v);
  if (kind === "array") {
    const a = v as unknown[];
    const inner = a.slice(0, 5).map((x) => (kindOf(x) === "object" ? "{…}" : kindOf(x) === "array" ? "[…]" : JSON.stringify(x))).join(", ");
    return `[${inner}${a.length > 5 ? ", …" : ""}]`.slice(0, PREVIEW_MAX);
  }
  if (kind === "object") {
    const keys = Object.keys(v as object);
    const inner = keys.slice(0, 4).map((k) => `${k}: ${scalarText((v as Record<string, unknown>)[k], true)}`).join(", ");
    return `{${inner}${keys.length > 4 ? ", …" : ""}}`.slice(0, PREVIEW_MAX);
  }
  return scalarText(v, false);
}

function scalarText(v: unknown, short: boolean): string {
  const kind = kindOf(v);
  if (kind === "object") return "{…}";
  if (kind === "array") return `[${(v as unknown[]).length}]`;
  if (kind === "string") {
    const s = v as string;
    return JSON.stringify(short && s.length > 24 ? `${s.slice(0, 24)}…` : s);
  }
  return String(v);
}

/** Below this depth everything opens by default; deeper containers start closed. */
export const OPEN_DEPTH = 1;

/** Which rows are open: the default is by depth, and every toggle is remembered against it. */
export type OpenState = { toggled: Set<string> };

export function isOpen(state: OpenState, id: string, depth: number): boolean {
  const byDefault = depth <= OPEN_DEPTH;
  return state.toggled.has(id) ? !byDefault : byDefault;
}

export function toggleRow(state: OpenState, id: string): OpenState {
  const toggled = new Set(state.toggled);
  if (toggled.has(id)) toggled.delete(id);
  else toggled.add(id);
  return { toggled };
}

/** The rows in view, top to bottom. A closed container's children are not here. */
export function jsonRows(value: unknown, state: OpenState, max = 5000): JsonRow[] {
  const out: JsonRow[] = [];
  const walk = (v: unknown, key: string, id: string, depth: number) => {
    if (out.length >= max) return;
    const kind = kindOf(v);
    const container = kind === "object" || kind === "array";
    const count = kind === "array" ? (v as unknown[]).length : kind === "object" ? Object.keys(v as object).length : 0;
    const open = container && count > 0 && isOpen(state, id, depth);
    out.push({ id, key, depth, kind, text: container ? (open ? "" : previewOf(v)) : scalarText(v, false), count, open, container });
    if (!open) return;
    if (kind === "array") (v as unknown[]).forEach((x, i) => walk(x, String(i), `${id}/${i}`, depth + 1));
    else for (const [k, x] of Object.entries(v as Record<string, unknown>)) walk(x, k, `${id}/${k.replace(/~/g, "~0").replace(/\//g, "~1")}`, depth + 1);
  };
  walk(value, "", "", 0);
  return out;
}

/** JSON, or JSON Lines read as an array of its lines. */
export function parseJsonText(text: string): { value: unknown } | { error: string } {
  const t = text.trim();
  try {
    return { value: JSON.parse(t) };
  } catch (e) {
    const lines = t.split("\n").filter((l) => l.trim());
    if (lines.length > 1) {
      try {
        return { value: lines.map((l) => JSON.parse(l)) };
      } catch {
        /* not lines either */
      }
    }
    return { error: e instanceof Error ? e.message : String(e) };
  }
}
