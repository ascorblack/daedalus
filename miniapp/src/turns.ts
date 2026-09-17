// The conversation as the screen shows it: the flat message list grouped into turns, and the
// streaming turn kept apart from the settled ones.
//
// Two things here exist for speed, and both matter on a phone. A settled turn is built once and
// the same object is handed back on every later build, so `memo` on the view holds and a token
// arriving re-renders one turn instead of six hundred. And the live turn is merged separately, so
// the history is not rebuilt to show a word.

import type { MessageView } from "./api";

export type LiveTool = { id: string; name: string; args: string; result?: string; error?: boolean };
/**
 * `ended` is the model's own full stop: the last `message_stop` of the run said `end_turn`, so the
 * answer on the screen is the whole answer. The text stays until the written copy takes its place,
 * but nothing about the turn is live any more — no cursor under it, no dots over it — however long
 * the session takes to report itself idle afterwards.
 */
export type LiveState = { text: string; thinking: string; tools: LiveTool[]; startedAt: number | null; ended: boolean };
export const EMPTY_LIVE: LiveState = { text: "", thinking: "", tools: [], startedAt: null, ended: false };

export type ToolItem = { kind: "tool"; id: string; name: string; args: Record<string, unknown>; result?: string; error?: boolean; running: boolean; length?: number; clipped?: boolean };
export type NoteItem = { kind: "note"; text: string };
export type ThinkItem = { kind: "thinking"; text: string };
export type SummaryItem = { kind: "summary"; text: string; reason: string };
export type Activity = ToolItem | NoteItem | ThinkItem | SummaryItem;

export type Turn = {
  key: string;
  user?: MessageView;
  summary?: MessageView;
  activity: Activity[];
  answer: string;
  startedAt: number;
  endedAt: number;
  pendingTools: number;
  /** Everything that fed this turn. Same signature, same rendering: the previous object is reused. */
  sig: string;
  /** The tool calls already shown here, so a streamed one is not shown a second time. */
  toolIds: string[];
};

/** The trailing retrieval headline ⟦…⟧ is for the transcript index, not for the reader; a half-streamed one is cut too. */
export function stripHeadline(text: string): string {
  const m = text.match(/(?:^|\n)\s*⟦[^⟦⟧]{3,2000}⟧\s*$/s);
  if (m && m.index !== undefined) return text.slice(0, m.index).trimEnd();
  const open = text.lastIndexOf("⟦");
  if (open !== -1 && !text.slice(open).includes("⟧")) {
    const lineStart = text.lastIndexOf("\n", open) + 1;
    if (!text.slice(lineStart, open).trim()) return text.slice(0, lineStart).trimEnd();
  }
  return text;
}

export function parseArgs(raw: string): Record<string, unknown> {
  try {
    return JSON.parse(raw || "{}");
  } catch {
    return { raw };
  }
}

/**
 * Group the flat message list into turns: a user message plus everything the agent did after it.
 *
 * `previous` is the result of the last call. A turn whose inputs did not change is returned as the
 * very same object, which is what keeps the view from reconciling the whole history.
 */
export function buildTurns(messages: MessageView[], previous: readonly Turn[] = []): Turn[] {
  const results = new Map<string, { content: string; is_error: boolean; length?: number; clipped?: boolean }>();
  for (const m of messages) for (const r of m.tool_results) results.set(r.id, r);
  const turns: Turn[] = [];
  const sigs: string[][] = [];
  let current: Turn | null = null;
  const open = (key: string, at: number): Turn => {
    const t: Turn = { key, activity: [], answer: "", startedAt: at, endedAt: at, pendingTools: 0, sig: "", toolIds: [] };
    turns.push(t);
    sigs.push([key]);
    return t;
  };
  const mark = (part: string) => sigs[sigs.length - 1].push(part);
  messages.forEach((m, i) => {
    const at = Date.parse(m.created_at) || 0;
    if (m.role === "tool" || m.internal) return;
    if (m.summary) {
      if (m.compaction?.reason !== "core") {
        // The host compacted between runs (auto) or on request (manual): a block of its own after the
        // turn, so the answer that came before it stays the answer.
        turns.push({ key: `s${m.seq ?? i}`, summary: m, activity: [], answer: "", startedAt: at, endedAt: at, pendingTools: 0, sig: "", toolIds: [] });
        sigs.push([`s${m.seq ?? i}`, `${m.text.length}`]);
        current = null;
        return;
      }
      // The core compacted mid-run: a step inside the turn, where the summarised work used to be.
      if (!current) current = open(`a${m.seq ?? i}`, at);
      if (current.answer) {
        current.activity.push({ kind: "note", text: current.answer });
        current.answer = "";
      }
      current.activity.push({ kind: "summary", text: m.text, reason: "core" });
      mark(`x${m.seq ?? i}:${m.text.length}`);
      return;
    }
    if (m.role === "user") {
      current = open(`u${m.seq ?? i}`, at);
      current.user = m;
      mark(`${m.text.length}:${m.origin ?? ""}`);
      return;
    }
    if (m.role === "system") return;
    if (!current) current = open(`a${m.seq ?? i}`, at);
    current.endedAt = at;
    mark(`m${m.seq ?? i}:${m.text.length}:${m.thinking.length}`);
    if (current.answer) {
      // Text that turned out not to be final becomes a note.
      current.activity.push({ kind: "note", text: current.answer });
      current.answer = "";
    }
    if (m.thinking) current.activity.push({ kind: "thinking", text: m.thinking });
    if (m.text && m.tool_calls.length) current.activity.push({ kind: "note", text: m.text });
    else if (m.text) current.answer = m.text;
    for (const c of m.tool_calls) {
      const r = results.get(c.id);
      const running = r === undefined;
      if (running) current.pendingTools++;
      current.toolIds.push(c.id);
      current.activity.push({ kind: "tool", id: c.id, name: c.name, args: c.arguments, result: r?.content, error: r?.is_error, running, length: r?.length, clipped: r?.clipped });
      mark(`t${c.id}:${r ? `${r.content.length}${r.is_error ? "!" : ""}` : "-"}`);
    }
  });
  const before = new Map<string, Turn>();
  for (const t of previous) before.set(t.key, t);
  return turns.map((t, i) => {
    t.sig = sigs[i].join("|");
    const old = before.get(t.key);
    return old && old.sig === t.sig ? old : t;
  });
}

/** The settled turn the streaming one continues, if there is one: a compaction block never is. */
export function liveBase(turns: readonly Turn[]): Turn | null {
  const last = turns[turns.length - 1];
  return last && !last.summary ? last : null;
}

/** The streaming turn: the settled tail with what the event stream has said since merged into it. */
export function applyLive(base: Turn | null, live: LiveState, now: number): Turn {
  const t: Turn = base
    ? { ...base, activity: base.activity.slice(), toolIds: base.toolIds }
    : { key: "live", activity: [], answer: "", startedAt: live.startedAt ?? now, endedAt: now, pendingTools: 0, sig: "", toolIds: [] };
  if (live.tools.length && t.pendingTools > 0) {
    // The call is in the history but its result is not written yet: the streamed result fills it in.
    t.activity = t.activity.map((a) => {
      if (a.kind !== "tool" || !a.running) return a;
      const lt = live.tools.find((x) => x.id === a.id);
      if (!lt || lt.result === undefined) return a;
      t.pendingTools--;
      // The stream carries the whole result, so what came over it is never cut short.
      return { ...a, result: lt.result, error: lt.error, running: false, length: lt.result.length, clipped: false };
    });
  }
  const fresh = live.tools.filter((lt) => !t.toolIds.includes(lt.id));
  if (t.answer && (live.text || live.thinking || fresh.length)) {
    // Something newer is streaming, so the text before it was not the final answer.
    t.activity.push({ kind: "note", text: t.answer });
    t.answer = "";
  }
  const thinkingKnown = t.activity.some((a) => a.kind === "thinking" && a.text === live.thinking);
  if (live.thinking && !thinkingKnown) t.activity.push({ kind: "thinking", text: live.thinking });
  for (const lt of fresh) {
    const running = lt.result === undefined;
    if (running) t.pendingTools++;
    t.activity.push({ kind: "tool", id: lt.id, name: lt.name, args: parseArgs(lt.args), result: lt.result, error: lt.error, running, length: lt.result?.length, clipped: false });
  }
  if (live.text) t.answer = stripHeadline(live.text);
  t.endedAt = now;
  return t;
}

// ── reconciling what the API says with what the screen already holds ──────────────────────

export type Merge = {
  messages: MessageView[];
  /** The tail did not overlap what is on screen (or the history was rewritten): re-read it whole. */
  gap: boolean;
};

const sameMessage = (a: MessageView, b: MessageView): boolean =>
  a.text === b.text && a.thinking === b.thinking && a.tool_calls.length === b.tool_calls.length && a.tool_results.length === b.tool_results.length && !!a.summary === !!b.summary;

/**
 * Fold a freshly read tail into the messages already on screen, matching by `seq`.
 *
 * The tail is what the run has just added; everything older is already here. When the two do not
 * overlap — the screen has been away long enough to miss messages — or the history got shorter
 * (a revert, a cleared session), there is nothing to fold into and the caller re-reads it whole.
 * When nothing in the tail is new, the array that came in is handed back unchanged, so the turn
 * objects keep their identity and nothing re-renders.
 *
 * A running session ends in messages the engine holds and the transcript has not written yet. Their
 * `seq` is the one the row is going to get, and they say so (`live`), because until the row exists
 * the number is a promise rather than a fact: the tail is the only authority on them, so whatever
 * the screen holds for them is dropped and what came in takes its place. They are never a gap —
 * treating them as one costs a re-read of the whole session on every event of a run.
 */
export function reconcile(known: readonly MessageView[], tail: readonly MessageView[]): Merge {
  if (!known.length) return { messages: tail.slice(), gap: false };
  if (!tail.length) return { messages: known.slice(), gap: false };
  const settled = known.filter((m) => !m.live);
  const fresh = tail.filter((m) => !m.live);
  const live = tail.filter((m) => m.live);
  // Nothing settled on either side to match on: the tail is all there is to go by.
  if (!settled.length) return { messages: tail.slice(), gap: false };
  if (settled.some((m) => m.seq == null) || fresh.some((m) => m.seq == null)) return { messages: known.slice(), gap: true };
  const maxKnown = settled[settled.length - 1].seq!;
  if (fresh.length) {
    if (fresh[fresh.length - 1].seq! < maxKnown) return { messages: known.slice(), gap: true };
    if (fresh[0].seq! > maxKnown + 1) return { messages: known.slice(), gap: true };
  }
  const bySeq = new Map<number, MessageView>();
  for (const m of settled) bySeq.set(m.seq!, m);
  let changed = false;
  for (const m of fresh) {
    const old = bySeq.get(m.seq!);
    if (old && sameMessage(old, m)) continue;
    changed = true;
    bySeq.set(m.seq!, m);
  }
  // The live tail always trails what is settled, so what the screen holds for it sits at the end.
  const heldLive = known.slice(settled.length);
  const sameLive = live.length === heldLive.length && live.every((m, i) => sameMessage(m, heldLive[i]));
  if (!changed && sameLive) return { messages: known as MessageView[], gap: false };
  const base = changed ? [...bySeq.values()].sort((a, b) => a.seq! - b.seq!) : settled;
  return { messages: live.length ? [...base, ...live] : base, gap: false };
}

/** An older page, put in front of what is on screen. Anything not actually older is dropped. */
export function prepend(known: readonly MessageView[], older: readonly MessageView[]): MessageView[] {
  if (!known.length) return older.slice();
  const first = known[0].seq;
  const head = first == null ? [] : older.filter((m) => m.seq != null && m.seq < first);
  return head.length ? [...head, ...known] : (known as MessageView[]);
}

/** Whether an answer to `before=<seq>` really is an older page: an API that ignores the cursor repeats the tail. */
export function isOlderPage(older: readonly MessageView[], oldestKnown: number | null | undefined): boolean {
  if (oldestKnown == null || !older.length) return false;
  return older.every((m) => m.seq != null && m.seq < oldestKnown);
}

/**
 * The streaming turn's state after one event off the wire. Pure, so what the screen shows during a
 * run is decided in one place and can be read back without a browser.
 *
 * The turn ends on the model's own full stop — `message_stop` with `end_turn` — and on nothing else.
 * The session reports itself idle later, after it has written the answer down, snapshotted the files
 * and told the other fronts, and waiting for that is what used to leave a cursor blinking under a
 * finished answer. The text is kept: the written copy takes its place when the read lands.
 */
export function liveAfter(state: LiveState, event: string, p: Record<string, any>): LiveState {
  if (event === "message_start") return { ...state, text: "", thinking: "", ended: false, startedAt: state.startedAt ?? Date.now() };
  if (event === "content_block_delta") {
    const d = p.delta ?? {};
    if (d.type === "text_delta") return { ...state, text: state.text + (d.text ?? "") };
    if (d.type === "thinking_delta") return { ...state, thinking: state.thinking + (d.text ?? "") };
    return state;
  }
  if (event === "tool_use_start") return { ...state, tools: [...state.tools, { id: p.tool_call_id, name: p.tool_name, args: "" }] };
  if (event === "tool_use_stop") return { ...state, tools: state.tools.map((t) => (t.id === p.tool_call_id ? { ...t, args: JSON.stringify(p.final_input ?? {}) } : t)) };
  if (event === "tool_result") return { ...state, tools: state.tools.map((t) => (t.id === p.tool_call_id ? { ...t, result: String(p.content ?? p.output ?? ""), error: !!p.is_error } : t)) };
  // A message that ended to make a tool call is not the end of the turn: the run goes on.
  if (event === "message_stop" && (p.stop_reason === "end_turn" || p.stop_reason === "max_tokens")) return { ...state, ended: true };
  return state;
}

// ── the streaming turn's state, outside React ─────────────────────────────────────────────

export type LiveStore = {
  get: () => LiveState;
  subscribe: (fn: () => void) => () => void;
  update: (fn: (s: LiveState) => LiveState) => void;
  reset: () => void;
};

/**
 * Tokens arrive faster than a phone repaints. They are folded into one object here and the
 * subscribers are told once a frame, so the screen does as much work per frame as it can show —
 * and only the components that read this one re-render, not the whole conversation.
 */
export function createLiveStore(): LiveStore {
  let state = EMPTY_LIVE;
  const subs = new Set<() => void>();
  let frame = 0;
  const raf = typeof requestAnimationFrame === "function" ? requestAnimationFrame : (fn: FrameRequestCallback) => setTimeout(() => fn(0), 16) as unknown as number;
  const cancel = typeof cancelAnimationFrame === "function" ? cancelAnimationFrame : (h: number) => clearTimeout(h);
  const flush = () => {
    frame = 0;
    for (const fn of [...subs]) fn();
  };
  return {
    get: () => state,
    subscribe(fn) {
      subs.add(fn);
      return () => {
        subs.delete(fn);
      };
    },
    update(fn) {
      const next = fn(state);
      // An event the streaming turn has nothing to do with — a state change, a hook, the end of the
      // run — leaves the state as it was, and repainting for it would cost a frame per event during
      // a run for nothing on the screen.
      if (next === state) return;
      state = next;
      if (!frame) frame = raf(flush);
    },
    reset() {
      if (frame) cancel(frame);
      frame = 0;
      if (state === EMPTY_LIVE) return;
      state = EMPTY_LIVE;
      for (const fn of [...subs]) fn();
    },
  };
}
