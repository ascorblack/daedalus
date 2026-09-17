import { describe, expect, it } from "vitest";

// The module reaches the browser API through ./api, which reads the address bar as it loads. The panel
// helper under test touches none of that, so the test gives the module the little of a window it needs
// and imports it afterwards.
(globalThis as unknown as { window: unknown }).window = { location: { search: "", pathname: "/", hash: "" }, history: { replaceState: () => undefined } };
const { agentNote } = await import("./voice");
type AgentNews = Parameters<typeof agentNote>[0];

const agent = (over: Partial<AgentNews>): AgentNews => ({
  session_id: "s-1",
  title: "Parser",
  status: "running",
  last_message_at: "2026-09-17T10:00:00Z",
  answer: "",
  ...over,
});

describe("agentNote", () => {
  it("shows what the agent said on the way, with the time it said it", () => {
    const note = agentNote(agent({ progress: "Found the problem in the lexer.", progress_at: "2026-09-17T10:05:00Z", answer: "an older answer" }));
    expect(note).toEqual({ line: "Found the problem in the lexer.", when: "2026-09-17T10:05:00Z", waiting: "", live: true });
  });

  it("falls back to the last answer when the agent has said nothing on the way", () => {
    const note = agentNote(agent({ answer: "the parser is fixed" }));
    expect(note).toEqual({ line: "the parser is fixed", when: "2026-09-17T10:00:00Z", waiting: "", live: false });
  });

  it("says which agents are stopped and what is stopping them", () => {
    expect(agentNote(agent({ waiting: "operator", progress: "which photos?" })).waiting).toBe("Waiting for you");
    expect(agentNote(agent({ waiting: "approval", progress: "waiting for approval — Exec: rm -rf build" })).waiting).toBe("Waiting for approval");
    expect(agentNote(agent({ waiting: "" })).waiting).toBe("");
  });
});
