import { describe, expect, it } from "vitest";
import type { MessageView } from "./api";
import { applyLive, buildTurns, EMPTY_LIVE, isOlderPage, liveBase, prepend, reconcile } from "./turns";

let clock = 1_700_000_000_000;

function msg(seq: number, over: Partial<MessageView> = {}): MessageView {
  clock += 1000;
  return { role: "assistant", seq, text: "", thinking: "", tool_calls: [], tool_results: [], created_at: new Date(clock).toISOString(), ...over };
}

const user = (seq: number, text: string) => msg(seq, { role: "user", text });
const answer = (seq: number, text: string) => msg(seq, { text });
const call = (seq: number, id: string, name = "Exec") => msg(seq, { tool_calls: [{ id, name, arguments: {} }] });
const result = (seq: number, id: string, content: string) => msg(seq, { role: "tool", tool_results: [{ id, content, is_error: false }] });

describe("buildTurns", () => {
  it("groups a user message with the work that followed it", () => {
    const turns = buildTurns([user(1, "go"), call(2, "c1"), result(3, "c1", "done"), answer(4, "finished")]);
    expect(turns).toHaveLength(1);
    expect(turns[0].user?.text).toBe("go");
    expect(turns[0].answer).toBe("finished");
    expect(turns[0].activity.filter((a) => a.kind === "tool")).toHaveLength(1);
    expect(turns[0].pendingTools).toBe(0);
    expect(turns[0].toolIds).toEqual(["c1"]);
  });

  it("counts a tool call whose result has not landed as pending", () => {
    const turns = buildTurns([user(1, "go"), call(2, "c1")]);
    expect(turns[0].pendingTools).toBe(1);
  });

  it("hands back the same objects when a new message is appended", () => {
    const history = [user(1, "one"), answer(2, "first"), user(3, "two"), answer(4, "second")];
    const before = buildTurns(history);
    const after = buildTurns([...history, user(5, "three"), answer(6, "third")], before);
    expect(after).toHaveLength(3);
    expect(after[0]).toBe(before[0]);
    expect(after[1]).toBe(before[1]);
    expect(after[2]).not.toBe(before[1]);
  });

  it("rebuilds only the turn a late tool result belongs to", () => {
    const history = [user(1, "one"), answer(2, "first"), user(3, "two"), call(4, "c4")];
    const before = buildTurns(history);
    expect(before[1].pendingTools).toBe(1);
    const after = buildTurns([...history, result(5, "c4", "output")], before);
    expect(after[0]).toBe(before[0]);
    expect(after[1]).not.toBe(before[1]);
    expect(after[1].pendingTools).toBe(0);
    expect((after[1].activity[0] as { result?: string }).result).toBe("output");
  });

  it("keeps a host compaction as a block of its own and opens a new turn after it", () => {
    const turns = buildTurns([user(1, "go"), answer(2, "done"), msg(3, { summary: true, text: "…", compaction: { reason: "auto" } }), answer(4, "after")]);
    expect(turns.map((t) => t.key)).toEqual(["u1", "s3", "a4"]);
    expect(turns[1].summary?.text).toBe("…");
    expect(liveBase(turns)?.key).toBe("a4");
  });

  it("does not continue a compaction block with the streaming turn", () => {
    const turns = buildTurns([user(1, "go"), answer(2, "done"), msg(3, { summary: true, text: "…", compaction: { reason: "auto" } })]);
    expect(liveBase(turns)).toBeNull();
  });

  it("puts a core compaction inside the turn it interrupted", () => {
    const turns = buildTurns([user(1, "go"), answer(2, "part"), msg(3, { summary: true, text: "…", compaction: { reason: "core" } }), answer(4, "rest")]);
    expect(turns).toHaveLength(1);
    expect(turns[0].activity.map((a) => a.kind)).toEqual(["note", "summary"]);
    expect(turns[0].answer).toBe("rest");
  });

  it("leaves internal and system messages out", () => {
    const turns = buildTurns([user(1, "go"), msg(2, { role: "system", text: "x" }), msg(3, { internal: true, text: "y" }), answer(4, "z")]);
    expect(turns).toHaveLength(1);
    expect(turns[0].answer).toBe("z");
  });
});

describe("applyLive", () => {
  it("merges the streamed answer into the settled tail without touching it", () => {
    const turns = buildTurns([user(1, "go")]);
    const live = applyLive(liveBase(turns), { ...EMPTY_LIVE, text: "half a sen" }, 5);
    expect(live.answer).toBe("half a sen");
    expect(live.key).toBe("u1");
    expect(turns[0].answer).toBe("");
    expect(turns[0].activity).toHaveLength(0);
  });

  it("cuts a trailing retrieval headline off the streamed text", () => {
    const live = applyLive(null, { ...EMPTY_LIVE, text: "the answer\n⟦index: a, b, c⟧" }, 5);
    expect(live.answer).toBe("the answer");
  });

  it("shows a streamed tool that is not in the history yet, once", () => {
    const turns = buildTurns([user(1, "go"), call(2, "c1")]);
    const state = { ...EMPTY_LIVE, tools: [{ id: "c1", name: "Exec", args: "{}" }, { id: "c9", name: "Read", args: '{"path":"a"}' }] };
    const live = applyLive(liveBase(turns), state, 5);
    const tools = live.activity.filter((a) => a.kind === "tool");
    expect(tools).toHaveLength(2);
    expect(live.pendingTools).toBe(2);
  });

  it("fills a persisted call with the result that streamed in", () => {
    const turns = buildTurns([user(1, "go"), call(2, "c1")]);
    const live = applyLive(liveBase(turns), { ...EMPTY_LIVE, tools: [{ id: "c1", name: "Exec", args: "{}", result: "ok" }] }, 5);
    const tool = live.activity[0] as { result?: string; running: boolean };
    expect(tool.result).toBe("ok");
    expect(tool.running).toBe(false);
    expect(live.pendingTools).toBe(0);
    expect((turns[0].activity[0] as { running: boolean }).running).toBe(true);
  });

  it("demotes an answer to a note once newer work streams in", () => {
    const turns = buildTurns([user(1, "go"), answer(2, "an earlier answer")]);
    const live = applyLive(liveBase(turns), { ...EMPTY_LIVE, text: "the next one" }, 5);
    expect(live.activity.map((a) => a.kind)).toEqual(["note"]);
    expect(live.answer).toBe("the next one");
  });
});

describe("reconcile", () => {
  const history = [user(1, "one"), answer(2, "first"), user(3, "two"), answer(4, "second")];

  it("appends what the tail added", () => {
    const tail = [history[3], user(5, "three")];
    const out = reconcile(history, tail);
    expect(out.gap).toBe(false);
    expect(out.messages.map((m) => m.seq)).toEqual([1, 2, 3, 4, 5]);
  });

  it("returns the same array when the tail says nothing new", () => {
    const out = reconcile(history, history.slice(2));
    expect(out.messages).toBe(history);
    expect(out.gap).toBe(false);
  });

  it("replaces a message the run has since filled in", () => {
    const grown = [{ ...history[3], text: "second, at last" }];
    const out = reconcile(history, grown);
    expect(out.messages).toHaveLength(4);
    expect(out.messages[3].text).toBe("second, at last");
  });

  it("asks for a whole read when the tail does not reach what is on screen", () => {
    expect(reconcile(history, [user(40, "much later")]).gap).toBe(true);
  });

  it("asks for a whole read when the history got shorter", () => {
    expect(reconcile(history, [history[0]]).gap).toBe(true);
  });

  it("asks for a whole read when a message carries no seq", () => {
    expect(reconcile(history, [{ ...history[3], seq: null }]).gap).toBe(true);
  });

  it("takes the tail as the history when nothing is on screen yet", () => {
    expect(reconcile([], history).messages).toHaveLength(4);
  });

  it("keeps the turn objects alive across a tail that changed nothing", () => {
    const turns = buildTurns(history);
    const out = reconcile(history, history.slice(3));
    expect(buildTurns(out.messages, turns)[0]).toBe(turns[0]);
  });

  // What the engine holds and the transcript has not written yet arrives with the row number it is
  // going to get and says it is live. Reading that as a hole in the history is what made every
  // event of a run re-read the whole session.
  const live = (seq: number, text: string) => msg(seq, { text, live: true });

  it("folds a live tail in without calling it a gap", () => {
    const out = reconcile(history, [history[3], live(5, "still writing")]);
    expect(out.gap).toBe(false);
    expect(out.messages).toHaveLength(5);
    expect(out.messages[4].text).toBe("still writing");
  });

  it("replaces the live tail it already holds instead of keeping both", () => {
    const once = reconcile(history, [live(5, "still")]);
    const twice = reconcile(once.messages, [live(5, "still writ")]);
    expect(twice.gap).toBe(false);
    expect(twice.messages).toHaveLength(5);
    expect(twice.messages[4].text).toBe("still writ");
  });

  it("takes the row over the live copy of it once it is written", () => {
    const once = reconcile(history, [live(5, "done")]);
    const settled = reconcile(once.messages, [answer(5, "done")]);
    expect(settled.gap).toBe(false);
    expect(settled.messages).toHaveLength(5);
    expect(settled.messages[4].live).toBeUndefined();
  });

  it("holds the array still when the live tail said the same thing twice", () => {
    const once = reconcile(history, [live(5, "still")]);
    const again = reconcile(once.messages, [history[3], live(5, "still")]);
    expect(again.messages).toBe(once.messages);
  });

  it("still asks for a whole read when a settled row carries no seq", () => {
    expect(reconcile(history, [{ ...history[3], seq: null }]).gap).toBe(true);
  });

  it("still asks for a whole read when the settled tail does not reach what is on screen", () => {
    expect(reconcile(history, [answer(40, "much later"), live(41, "…")]).gap).toBe(true);
  });
});

describe("older pages", () => {
  const history = [user(10, "one"), answer(11, "first")];

  it("recognises a page that really is older", () => {
    expect(isOlderPage([user(1, "x"), answer(2, "y")], 10)).toBe(true);
  });

  it("refuses a page an API without the cursor just repeated", () => {
    expect(isOlderPage(history, 10)).toBe(false);
    expect(isOlderPage([], 10)).toBe(false);
  });

  it("puts an older page in front and drops what is not older", () => {
    const out = prepend(history, [user(1, "x"), answer(11, "dup")]);
    expect(out.map((m) => m.seq)).toEqual([1, 10, 11]);
  });

  it("changes nothing when the page holds nothing older", () => {
    expect(prepend(history, [answer(11, "dup")])).toBe(history);
  });
});
