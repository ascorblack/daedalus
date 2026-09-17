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

const { IDLE_VOICE, micReady, orbVisual, smoothLevel, voiceReducer } = await import("./voice");
type VoiceUi = typeof IDLE_VOICE;
type VoiceEvent = Parameters<typeof voiceReducer>[1];

const run = (events: VoiceEvent[], from: VoiceUi = IDLE_VOICE): VoiceUi => events.reduce(voiceReducer, from);

describe("the page's state machine", () => {
  it("goes back to listening after an answer, and to idle when the microphone is shut", () => {
    const talking = run([{ type: "mic", on: true }, { type: "asked", text: "what is on the board?" }]);
    expect(talking.phase).toBe("thinking");
    expect(run([{ type: "done" }], talking).phase).toBe("listening");
    const typedOnly = run([{ type: "asked", text: "what is on the board?" }]);
    expect(run([{ type: "done" }], typedOnly).phase).toBe("idle");
  });

  it("keeps the concierge's phase when the microphone is shut mid-answer", () => {
    const answering = run([{ type: "mic", on: true }, { type: "asked", text: "go on" }, { type: "speaking", on: true }]);
    const shut = voiceReducer(answering, { type: "mic", on: false });
    expect(shut.phase).toBe("speaking");
    expect(voiceReducer(shut, { type: "speaking", on: false }).phase).toBe("idle");
  });

  it("holds the microphone shut while the model is loading and opens it when it is ready", () => {
    const loading = run([{ type: "engine", engine: { state: "loading", model: "nemotron-streaming-multi" } }, { type: "mic", on: true }]);
    expect(loading.phase).toBe("loading");
    expect(micReady(loading)).toBe(false);
    const ready = voiceReducer(loading, { type: "engine", engine: { state: "ready", loadedInMs: 5400 } });
    expect(ready.phase).toBe("listening");
    expect(micReady(ready)).toBe(true);
    expect(ready.engine.loadedInMs).toBe(5400);
  });

  it("collects the answer a sentence at a time and clears it when the next thing is said", () => {
    const answered = run([
      { type: "asked", text: "how did the invoices go?" },
      { type: "partial", text: "All eleven" },
      { type: "say", text: "All eleven went out." },
      { type: "say", text: "  Two came back.  " },
      { type: "say", text: "   " },
    ]);
    expect(answered.spoken).toEqual(["All eleven went out.", "Two came back."]);
    // The first sentence is the answer being read out, whatever the audio is doing.
    expect(answered.phase).toBe("speaking");
    expect(run([{ type: "asked", text: "and the rest?" }], answered).spoken).toEqual([]);
  });

  it("names the agent it is setting up, and forgets it when the run ends", () => {
    const delegating = run([{ type: "status", state: "delegating", title: "Invoice run" }]);
    expect([delegating.phase, delegating.delegating]).toEqual(["delegating", "Invoice run"]);
    expect(voiceReducer(delegating, { type: "done" }).delegating).toBe("");
  });

  it("shows a problem without pretending the page is still working", () => {
    const failed = run([{ type: "mic", on: true }, { type: "asked", text: "x" }, { type: "problem", message: "the concierge stopped" }]);
    expect([failed.phase, failed.problem]).toEqual(["listening", "the concierge stopped"]);
  });
});

describe("the orb", () => {
  it("grows with the voice and never past its bounds", () => {
    const quiet = orbVisual("listening", 0);
    const talking = orbVisual("listening", 0.08);
    const shouting = orbVisual("listening", 1);
    expect(quiet.scale).toBe(1);
    // Speech at a laptop's distance sits around an RMS of 0.05-0.2: the orb has to move there, not
    // only when someone shouts into the microphone.
    expect(talking.scale).toBeGreaterThan(1.1);
    expect(talking.scale).toBeLessThan(shouting.scale);
    expect(shouting.scale).toBeLessThanOrEqual(1.24);
    expect(shouting.glow).toBeLessThanOrEqual(1);
    expect(orbVisual("listening", -5).scale).toBe(1);
  });

  it("gives each state its own look whatever the microphone is doing", () => {
    expect(orbVisual("thinking", 0).spin).toBeGreaterThan(orbVisual("idle", 0).spin);
    expect(orbVisual("speaking", 0.5).scale).toBeLessThan(orbVisual("listening", 0.5).scale);
    expect(orbVisual("idle", 1).scale).toBe(1);
  });

  it("rises fast and falls slowly, the way an ear hears a sentence", () => {
    expect(smoothLevel(0, 1)).toBeGreaterThan(0.5);
    expect(smoothLevel(1, 0)).toBeGreaterThan(0.8);
    expect(smoothLevel(0.5, 5)).toBeLessThanOrEqual(1);
  });
});
