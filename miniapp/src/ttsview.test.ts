import { describe, expect, it } from "vitest";

import { EMPTY_TTS_VIEW, mergeTtsView } from "./ttsview";

type TtsView = typeof EMPTY_TTS_VIEW;

const voice = (over: Partial<TtsView["models"][number]> = {}): TtsView["models"][number] => ({
  id: "ru-dmitri",
  label: "Dmitri",
  kind: "piper",
  language: "ru",
  languages: [],
  new: false,
  gender: "male",
  size_bytes: 1,
  disk_bytes: 1,
  memory_mb: 1,
  sample_rate: 22050,
  licence: "CC BY 4.0",
  quality: 80,
  speed: 90,
  rtf: 0.24,
  keeps_up: true,
  note: "",
  speakers: [],
  recommended_for: ["ru"],
  installed: true,
  installed_bytes: 1,
  selected: true,
  ...over,
});

const loaded: TtsView = {
  ...EMPTY_TTS_VIEW,
  models: [voice()],
  languages: ["en", "ru"],
  selected: "ru-dmitri",
  recommended: { ru: "ru-dmitri" },
  engine_installed: true,
  state: { ...EMPTY_TTS_VIEW.state, voice: "ru-dmitri", speed: 1.2, threads: 4, installed: true, active: true },
};

describe("mergeTtsView", () => {
  it("fills in everything the picker reads, from nothing at all", () => {
    const view = mergeTtsView(null, null);
    expect(view.models).toEqual([]);
    expect(view.state.speed.toFixed(2)).toBe("1.00");
    expect(view.recommended).toEqual({});
  });

  it("keeps what an answer does not mention rather than dropping it", () => {
    const after = mergeTtsView(loaded, { models: [voice({ selected: false })], selected: "" });
    expect(after.languages).toEqual(["en", "ru"]);
    expect(after.recommended).toEqual({ ru: "ru-dmitri" });
    expect(after.state.threads).toBe(4);
    expect(after.state.speed).toBe(1.2);
  });

  it("folds a partial state into the one the page already had", () => {
    const after = mergeTtsView(loaded, { state: { ...EMPTY_TTS_VIEW.state, speed: 1.5 } });
    expect(after.state.speed).toBe(1.5);
    expect(after.models).toHaveLength(1);
  });

  it("carries a multilingual voice's whole language list through a fold", () => {
    // The card says which languages it reads and the filter consults the same list; an answer that
    // mentioned neither must not leave the card looking like a single-language voice.
    const many = voice({ id: "multi-supertonic", languages: ["ru", "en", "de"], new: true });
    const after = mergeTtsView({ ...loaded, models: [many] }, { selected: "multi-supertonic" });
    expect(after.models[0].languages).toEqual(["ru", "en", "de"]);
    expect(after.models[0].new).toBe(true);
    expect(after.models[0].licence).toBe("CC BY 4.0");
  });

  it("never wipes a bar that is moving on a card", () => {
    const downloading = mergeTtsView(loaded, {
      models: [voice({ progress: { id: "ru-dmitri", state: "downloading", fraction: 0.4, error: "" } })],
    });
    const after = mergeTtsView(downloading, { models: [voice()] });
    expect(after.models[0].progress?.fraction).toBe(0.4);
  });
});
