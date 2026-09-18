// The voice picker's own data, and how one answer is folded into the last one.
//
// The twin of `sttview.ts`, and here for the same reason that one is: three endpoints answer this
// picker — the list, "Use this one", and a delete — and the screen used to replace its whole view
// with whatever came back from any of them. `view.state.speed.toFixed(2)` is then one absent field
// away from a white screen, which is exactly the crash the recognition picker was rewritten to end.
// So the same rule holds on this side: a view is folded into the one before it, a missing piece is
// kept rather than dropped, and every sub-object has a value even when the answer had none.

export type TtsVoice = {
  id: string;
  label: string;
  kind: string;
  language: string;
  // Every language the model reads, where it reads more than one; empty for the ordinary case of a
  // voice that speaks what it was trained on. The filter consults it, so a Russian filter finds a
  // multilingual voice that is filed under English.
  languages: string[];
  new: boolean;
  gender: string;
  size_bytes: number;
  disk_bytes: number;
  memory_mb: number;
  sample_rate: number;
  licence: string;
  quality: number;
  speed: number;
  rtf: number;
  keeps_up: boolean;
  note: string;
  speakers: string[];
  recommended_for: string[];
  installed: boolean;
  installed_bytes: number;
  selected: boolean;
  progress?: TtsProgress;
};

export type TtsProgress = { id: string; state: string; fraction: number; error: string };

/** The chosen voice as the server holds it: what it is, how it is spoken, and where its weights are. */
export type TtsState = {
  voice: string;
  label: string;
  language: string;
  speaker: string;
  speed: number;
  threads: number;
  installed: boolean;
  active: boolean;
  engine_installed: boolean;
  state: string;
  error: string;
  loaded: string;
  encoder: boolean;
};

export type TtsView = {
  models: TtsVoice[];
  languages: string[];
  selected: string;
  disk_bytes: number;
  engine_installed: boolean;
  recommended: Record<string, string>;
  state: TtsState;
};

export const EMPTY_TTS_STATE: TtsState = {
  voice: "",
  label: "",
  language: "",
  speaker: "",
  speed: 1,
  threads: 2,
  installed: false,
  active: false,
  engine_installed: false,
  state: "idle",
  error: "",
  loaded: "",
  encoder: false,
};

export const EMPTY_TTS_VIEW: TtsView = {
  models: [],
  languages: [],
  selected: "",
  disk_bytes: 0,
  engine_installed: false,
  recommended: {},
  state: EMPTY_TTS_STATE,
};

/** An answer that may be missing anything, as a complete view over the one the page already had. */
export function mergeTtsView(previous: TtsView | null, answer: Partial<TtsView> | null | undefined): TtsView {
  const base = previous ?? EMPTY_TTS_VIEW;
  const next = answer ?? {};
  const models = Array.isArray(next.models) ? next.models : base.models;
  const before = new Map(base.models.map((m) => [m.id, m]));
  return {
    ...base,
    ...next,
    // A download's progress lives only in this browser — it arrives on a stream, not in a view — so an
    // answer that does not mention it must not wipe the bar that is moving on the card.
    models: models.map((m) => (m.progress ? m : { ...m, progress: before.get(m.id)?.progress })),
    languages: next.languages ?? base.languages,
    recommended: next.recommended ?? base.recommended,
    state: { ...base.state, ...(next.state ?? {}) },
  };
}
