// The speech-model picker's own data: what the server says about local recognition, and how one
// answer is folded into the last one.
//
// This exists because of a crash. `POST /api/stt/select` used to answer with the models alone, while
// `GET /api/stt` answered with the models *and* the settings, the engine, the decoders and the
// recommendations. The picker replaced its whole view with whatever came back from "Use this one",
// the next render read `decoders.opus` off an object that no longer carried `decoders`, and the
// screen went down with "Cannot read properties of undefined (reading 'opus')" — while the server had
// already saved the selection, so a reload showed the model happily in use.
//
// Both halves of that are fixed: the server answers every one of those calls with the whole view, and
// nothing here believes it. A view is folded into the one before it, missing pieces are kept rather
// than dropped, and every optional sub-object has a value even when the answer had none. A page must
// not be one field away from a white screen.

import { api } from "./api";

export type SpeechModel = {
  id: string;
  label: string;
  kind: string;
  streaming: boolean;
  languages: string[];
  language_count: number;
  size_bytes: number;
  disk_bytes: number;
  memory_mb: number;
  licence: string;
  accuracy: number;
  speed: number;
  note: string;
  recommended_for: string[];
  installed: boolean;
  installed_bytes: number;
  selected: boolean;
  verified: boolean;
  detects_language: boolean;
  progress?: SttProgress;
};

export type SttProgress = { id: string; state: string; fraction: number; error: string };

/** Where the chosen model's weights are: nothing loaded, loading now, ready, or a load that failed. */
export type SttLoad = { state: string; model: string; loaded_in_ms: number; error: string };

export type SttDecoders = { opus: boolean; any: boolean };

export type SpeechView = {
  models: SpeechModel[];
  languages: string[];
  selected: string;
  disk_bytes: number;
  language: string;
  threads: number;
  engine_installed: boolean;
  decoders: SttDecoders;
  recommended: Record<string, string>;
  load: SttLoad;
};

/** What a view is before anything has answered: every field present, nothing claimed. */
export const EMPTY_VIEW: SpeechView = {
  models: [],
  languages: [],
  selected: "",
  disk_bytes: 0,
  language: "auto",
  threads: 2,
  engine_installed: false,
  decoders: { opus: false, any: false },
  recommended: {},
  load: { state: "idle", model: "", loaded_in_ms: 0, error: "" },
};

/** An answer that may be missing anything, as a complete view over the one the page already had. */
export function mergeSttView(previous: SpeechView | null, answer: Partial<SpeechView> | null | undefined): SpeechView {
  const base = previous ?? EMPTY_VIEW;
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
    decoders: next.decoders ?? base.decoders,
    recommended: next.recommended ?? base.recommended,
    load: next.load ?? base.load,
  };
}

/** One frame of `/api/stt/progress`: a download's bar, or the engine loading a model into memory. */
export type SttFrame =
  | { kind: "download"; progress: SttProgress }
  | { kind: "engine"; load: SttLoad };

/** Read one frame off the stream. Anything unrecognised is dropped rather than guessed at. */
export function sttFrame(raw: unknown): SttFrame | null {
  if (!raw || typeof raw !== "object") return null;
  const body = raw as Record<string, unknown>;
  if (body.kind === "engine") {
    return {
      kind: "engine",
      load: {
        state: String(body.state ?? "idle"),
        model: String(body.model ?? ""),
        loaded_in_ms: Number(body.loaded_in_ms ?? 0),
        error: String(body.error ?? ""),
      },
    };
  }
  if (typeof body.id !== "string" || !body.id) return null;
  return { kind: "download", progress: { id: body.id, state: String(body.state ?? ""), fraction: Number(body.fraction ?? 0), error: String(body.error ?? "") } };
}

export const fetchSttView = () => api.get<Partial<SpeechView>>("/api/stt");

/** Choose a model, a language or a thread count. Every field is optional; what is sent is what changes. */
export const postSttSelect = (patch: { model?: string; language?: string; threads?: number }) =>
  api.post<Partial<SpeechView>>("/api/stt/select", patch);

/** Start loading the chosen model now, rather than under the first thing the operator says. */
export const warmStt = () => api.post<SttLoad>("/api/stt/engine/warm", {});
