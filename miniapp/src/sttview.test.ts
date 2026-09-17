import { describe, expect, it } from "vitest";

// The module's API client reads the address bar as it loads, and none of what is tested here touches
// it; the little of a window it needs is given first, as the voice test does.
(globalThis as unknown as { window: unknown }).window = { location: { search: "", pathname: "/", hash: "" }, history: { replaceState: () => undefined } };
const { EMPTY_VIEW, mergeSttView, sttFrame } = await import("./sttview");
type SpeechView = typeof EMPTY_VIEW;

const model = (over: Partial<SpeechView["models"][number]> = {}): SpeechView["models"][number] => ({
  id: "nemotron-streaming-multi",
  label: "Nemotron 3.5 Streaming 0.6B",
  kind: "transducer",
  streaming: true,
  languages: ["en", "ru"],
  language_count: 28,
  size_bytes: 1,
  disk_bytes: 1,
  memory_mb: 1,
  licence: "other",
  accuracy: 82,
  speed: 84,
  note: "",
  recommended_for: [],
  installed: true,
  installed_bytes: 1,
  selected: false,
  verified: true,
  detects_language: true,
  ...over,
});

const loaded: SpeechView = {
  ...EMPTY_VIEW,
  models: [model()],
  languages: ["en", "ru"],
  decoders: { opus: true, any: false },
  recommended: { ru: "gigaam-ru" },
  engine_installed: true,
};

describe("mergeSttView", () => {
  // The crash this whole module exists for: "Use this one" answered with the models alone, the page
  // replaced its view with that answer, and the next render read `decoders.opus` off an object that
  // no longer carried `decoders`.
  it("keeps what an answer does not mention, so a bare select cannot take the screen down", () => {
    const view = mergeSttView(loaded, { models: [model({ selected: true })], selected: "nemotron-streaming-multi", disk_bytes: 751, language: "auto", threads: 2 });
    expect(view.selected).toBe("nemotron-streaming-multi");
    expect(view.models[0].selected).toBe(true);
    expect(view.decoders).toEqual({ opus: true, any: false });
    expect(view.recommended).toEqual({ ru: "gigaam-ru" });
    expect(view.load.state).toBe("idle");
  });

  it("answers with a whole view even when there was nothing before and nothing came back", () => {
    const view = mergeSttView(null, undefined);
    expect(view.decoders.opus).toBe(false);
    expect(view.models).toEqual([]);
    expect(view.load).toEqual({ state: "idle", model: "", loaded_in_ms: 0, error: "" });
  });

  it("does not wipe a download that is moving, because the bar is only ever in this browser", () => {
    const downloading = mergeSttView(loaded, { models: [model({ progress: { id: "nemotron-streaming-multi", state: "downloading", fraction: 0.4, error: "" } })] });
    const afterSelect = mergeSttView(downloading, { models: [model({ selected: true })] });
    expect(afterSelect.models[0].progress?.fraction).toBe(0.4);
  });

  it("takes the load state when the answer carries one", () => {
    const view = mergeSttView(loaded, { load: { state: "ready", model: "nemotron-streaming-multi", loaded_in_ms: 5400, error: "" } });
    expect(view.load.loaded_in_ms).toBe(5400);
  });
});

describe("sttFrame", () => {
  it("tells a download apart from the engine loading a model", () => {
    expect(sttFrame({ kind: "download", id: "gigaam-ru", state: "downloading", fraction: 0.5, error: "" })).toEqual({
      kind: "download",
      progress: { id: "gigaam-ru", state: "downloading", fraction: 0.5, error: "" },
    });
    expect(sttFrame({ kind: "engine", state: "loading", model: "gigaam-ru", loaded_in_ms: 0, error: "" })).toEqual({
      kind: "engine",
      load: { state: "loading", model: "gigaam-ru", loaded_in_ms: 0, error: "" },
    });
  });

  it("drops what it does not recognise rather than guessing at it", () => {
    expect(sttFrame(null)).toBe(null);
    expect(sttFrame("keepalive")).toBe(null);
    expect(sttFrame({ state: "downloading" })).toBe(null);
  });
});
