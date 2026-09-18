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

  it("says which agents are stopped and what is stopping them, as keys the panel translates", () => {
    expect(agentNote(agent({ waiting: "operator", progress: "which photos?" })).waiting).toBe("voice.agent.waiting.operator");
    expect(agentNote(agent({ waiting: "approval", progress: "waiting for approval — Exec: rm -rf build" })).waiting).toBe("voice.agent.waiting.approval");
    expect(agentNote(agent({ waiting: "" })).waiting).toBe("");
  });
});

const { BARGE_LEVEL, ECHO_GUARD_MS, IDLE_VOICE, micReady, orbVisual, shouldBargeIn, smoothLevel, voiceReducer } = await import("./voice");
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

  it("keeps the concierge's phase when the microphone is opened mid-answer", () => {
    // The page used to answer the tap with "listening" while an answer was being read out, and
    // nothing said "speaking" again afterwards — so the chip lied for the whole answer and the
    // barge-in, which will not interrupt a page that is not speaking, could not happen at all.
    const answering = run([{ type: "asked", text: "go on" }, { type: "speaking", on: true }]);
    const tapped = voiceReducer(answering, { type: "mic", on: true });
    expect(tapped.phase).toBe("speaking");
    expect(tapped.micOn).toBe(true);
    expect(voiceReducer(tapped, { type: "speaking", on: false }).phase).toBe("listening");
    // A tap with nothing being said still opens the microphone and says so.
    expect(run([{ type: "mic", on: true }]).phase).toBe("listening");
  });

  it("does not hold the microphone shut for a voice that is loading, but does say so", () => {
    // The two engines are loaded for two different reasons and only one of them is about hearing.
    // A synthesiser that is still being built means the next answer is read by the browser, which is
    // a line on the page; it is not a reason to refuse to listen.
    const loading = run([{ type: "voice", engine: { state: "loading", model: "ru-dmitri" } }, { type: "mic", on: true }]);
    expect(loading.voice.state).toBe("loading");
    expect(micReady(loading)).toBe(true);
    expect(loading.phase).toBe("listening");
    const ready = voiceReducer(loading, { type: "voice", engine: { state: "ready", loadedInMs: 1480 } });
    expect(ready.voice.loadedInMs).toBe(1480);
    expect(ready.phase).toBe("listening");
  });

  it("keeps the last answer's wait between the words and the sound", () => {
    const heard = run([{ type: "asked", text: "read me the board" }, { type: "say", text: "Eleven went out." }, { type: "audio", turn: "run-1", ms: 2400 }]);
    expect(heard.firstAudioMs).toBe(2400);
    // A newer answer replaces the number rather than adding to it: the line is about the last one.
    expect(voiceReducer(heard, { type: "audio", turn: "run-2", ms: 480 }).firstAudioMs).toBe(480);
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

  it("goes back to listening the moment the operator talks over the answer", () => {
    const speaking = run([{ type: "mic", on: true }, { type: "asked", text: "read me the digest" }, { type: "say", text: "Three things happened." }]);
    expect(speaking.phase).toBe("speaking");
    const barged = voiceReducer(speaking, { type: "barge" });
    expect(barged.phase).toBe("listening");
    expect(barged.spoken).toEqual(["Three things happened."]);
    expect(barged.partial).toBe("");
  });

  it("leaves a page that is not speaking alone when a barge-in arrives", () => {
    const thinking = run([{ type: "mic", on: true }, { type: "asked", text: "what is on the board?" }]);
    expect(voiceReducer(thinking, { type: "barge" })).toBe(thinking);
  });

  it("marks the sentences nobody read out, and forgets them when the next thing is asked", () => {
    const answered = run([
      { type: "asked", text: "how did the invoices go?" },
      { type: "say", text: "All eleven went out." },
      { type: "say", text: "Two came back." },
      { type: "unspoken", text: "Two came back." },
      { type: "unspoken", text: "Two came back." },
      { type: "unspoken", text: "   " },
    ]);
    // The sentence stays where it was — it is the answer — and only the claim that it was heard goes.
    expect(answered.spoken).toEqual(["All eleven went out.", "Two came back."]);
    expect(answered.unspoken).toEqual(["Two came back."]);
    expect(run([{ type: "asked", text: "and the rest?" }], answered).unspoken).toEqual([]);
    // One of two read out is still an answer being spoken; none of them is not.
    expect(answered.phase).toBe("speaking");
    expect(voiceReducer(answered, { type: "unspoken", text: "All eleven went out." }).phase).toBe("idle");
  });

  it("remembers that the browser wants a tap before it will speak, past a cleared conversation", () => {
    const blocked = run([{ type: "blocked", on: true }]);
    expect(blocked.blocked).toBe(true);
    expect(voiceReducer(blocked, { type: "blocked", on: true })).toBe(blocked);
    // Starting a new conversation does not un-block a browser; only speaking successfully does.
    expect(run([{ type: "cleared" }], blocked).blocked).toBe(true);
    expect(run([{ type: "blocked", on: false }], blocked).blocked).toBe(false);
  });

  it("shows a problem without pretending the page is still working", () => {
    const failed = run([{ type: "mic", on: true }, { type: "asked", text: "x" }, { type: "problem", message: "the concierge stopped" }]);
    expect([failed.phase, failed.problem]).toEqual(["listening", "the concierge stopped"]);
  });
});

describe("telling the operator from the page's own speaker", () => {
  it("does nothing when nothing is being read out", () => {
    expect(shouldBargeIn({ speaking: false, playingForMs: 5000, level: 0.9 })).toBe(false);
  });

  it("takes the first moments after playback begins for the speaker itself", () => {
    expect(shouldBargeIn({ speaking: true, playingForMs: ECHO_GUARD_MS - 1 })).toBe(false);
    expect(shouldBargeIn({ speaking: true, playingForMs: ECHO_GUARD_MS })).toBe(true);
  });

  it("believes a listener that reports no level, because that is its voice activity detector", () => {
    expect(shouldBargeIn({ speaking: true, playingForMs: 1200 })).toBe(true);
  });

  it("ignores a level too low to be somebody talking over a speaker", () => {
    expect(shouldBargeIn({ speaking: true, playingForMs: 1200, level: BARGE_LEVEL - 0.01 })).toBe(false);
    expect(shouldBargeIn({ speaking: true, playingForMs: 1200, level: BARGE_LEVEL })).toBe(true);
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

const { modelRow } = await import("./voice");

const fast = { id: "or.qwen-flash", label: "Qwen Flash", provider: "openrouter", model: "qwen/qwen3.7-flash", thinking: false, max_output_tokens: 4000, fast: true };
const thinker = { id: "ds.reasoner", label: "", provider: "deepseek", model: "deepseek-reasoner", thinking: true, max_output_tokens: 32000, fast: false };
const wordy = { id: "ds.chat", label: "DeepSeek Chat", provider: "deepseek", model: "deepseek-chat", thinking: false, max_output_tokens: 32000, fast: false };

describe("modelRow", () => {
  it("offers every configured preset with what it is, and marks the ones a conversation cannot wait for", () => {
    const row = modelRow({ preset: "or.qwen-flash", using: "or.qwen-flash", presets: [fast, thinker] });
    expect(row.value).toBe("or.qwen-flash");
    expect(row.choices).toEqual([
      { id: "or.qwen-flash", label: "Qwen Flash", detail: "openrouter · qwen/qwen3.7-flash", slow: false },
      // A preset with no label of its own is shown by its id, never as an empty line.
      { id: "ds.reasoner", label: "ds.reasoner", detail: "deepseek · deepseek-reasoner", slow: true },
    ]);
    expect(row.warn).toBe("");
    expect(row.fallback).toBe("");
    expect(row.addFast).toBe(false);
  });

  it("names the model an empty choice comes out as, so the first option is not a guess", () => {
    const row = modelRow({ preset: "", using: "or.qwen-flash", presets: [fast, thinker] });
    expect(row.value).toBe("");
    expect(row.fallback).toBe("Qwen Flash");
    expect(row.warn).toBe("");
  });

  it("says which kind of slow the model in use is: thinking, or allowed to write at length", () => {
    expect(modelRow({ preset: "", using: "ds.reasoner", presets: [fast, thinker] }).warn).toBe("voice.card.model.slow");
    expect(modelRow({ preset: "ds.chat", using: "ds.chat", presets: [fast, wordy] }).warn).toBe("voice.card.model.long");
  });

  it("asks for a fast model only where there is none to pick, and survives a page with no answer yet", () => {
    expect(modelRow({ preset: "", using: "ds.reasoner", presets: [thinker, wordy] }).addFast).toBe(true);
    expect(modelRow({ preset: "", using: "or.qwen-flash", presets: [fast, thinker] }).addFast).toBe(false);
    const empty = modelRow(null);
    expect(empty).toEqual({ value: "", choices: [], warn: "", fallback: "", addFast: true });
  });
});

// ── what is in the middle of the page ──────────────────────────────────────────────────────
//
// The view is in the reducer because it is a fact about the conversation, not about a component, and
// because the one thing that must be true of it is a statement about everything else: changing it
// changes nothing. A test that reads the whole state before and after is the only one that says so.

const { centerSession } = await import("./voice");

describe("the view state", () => {
  const busy: VoiceUi = {
    ...IDLE_VOICE,
    phase: "speaking",
    micOn: true,
    asked: "how did the invoice run go?",
    heard: "read me the second",
    spoken: ["All eleven went out."],
    partial: "and two came",
    agents: [],
  };

  it("starts on the orb", () => {
    expect(IDLE_VOICE.center).toEqual({ view: "orb" });
  });

  it("changes nothing but itself", () => {
    const read = voiceReducer(busy, { type: "center", center: { view: "transcript" } });
    expect(read.center).toEqual({ view: "transcript" });
    expect({ ...read, center: busy.center }).toEqual(busy);
  });

  it("goes to an agent and back without disturbing the answer being spoken", () => {
    const agent = voiceReducer(busy, { type: "center", center: { view: "agent", id: "s-1", title: "Invoice run" } });
    const back = voiceReducer(agent, { type: "center", center: { view: "orb" } });
    expect(agent.center).toEqual({ view: "agent", id: "s-1", title: "Invoice run" });
    expect(back.phase).toBe("speaking");
    expect(back.spoken).toEqual(busy.spoken);
    expect(back.heard).toBe(busy.heard);
  });

  it("is the same state when the view asked for is the one already drawn", () => {
    const read = voiceReducer(busy, { type: "center", center: { view: "transcript" } });
    expect(voiceReducer(read, { type: "center", center: { view: "transcript" } })).toBe(read);
    const agent = voiceReducer(busy, { type: "center", center: { view: "agent", id: "s-1", title: "Invoice run" } });
    expect(voiceReducer(agent, { type: "center", center: { view: "agent", id: "s-1", title: "Invoice run" } })).toBe(agent);
    expect(voiceReducer(agent, { type: "center", center: { view: "agent", id: "s-2", title: "Support inbox" } })).not.toBe(agent);
  });

  it("comes back to the orb for a new conversation, which has no transcript to read", () => {
    const read = voiceReducer(busy, { type: "center", center: { view: "agent", id: "s-1", title: "Invoice run" } });
    expect(voiceReducer(read, { type: "cleared" }).center).toEqual({ view: "orb" });
  });

  it("names the session whose transcript is being read, and none for the orb", () => {
    expect(centerSession({ view: "orb" }, "voice-1")).toBe("");
    expect(centerSession({ view: "transcript" }, "voice-1")).toBe("voice-1");
    expect(centerSession({ view: "agent", id: "s-1", title: "Invoice run" }, "voice-1")).toBe("s-1");
  });
});
