// What the app knows about a model before it is a preset: what an endpoint published about it, and
// the rules for turning that into the thing that is saved.
//
// It sits apart from the screen that shows it because these are the rules a mistake here is
// expensive in: a price, a window or a modality recorded against the wrong model is wrong in every
// figure read from it afterwards — every spend total, every cap, every usage chart — and nothing in
// the app ever says so. They are worth testing on their own.

import type { Preset } from "./api";

/** One model as an endpoint described it. Only the id is ever certain. */
export type ModelEntry = {
  id: string;
  name?: string;
  context_length?: number;
  max_output_tokens?: number;
  input_modalities?: string[];
  images?: boolean;
  reasoning?: boolean;
  pricing?: { input?: number; output?: number; cache_hit?: number };
};

/** Efforts a preset or a session may ask for. Hosted vendors map these onto their own names. */
export const REASONING_EFFORTS = ["low", "medium", "high", "xhigh"] as const;
export type ReasoningEffort = (typeof REASONING_EFFORTS)[number];

/** Index on the effort slider; an unknown or empty value sits on medium, the preset default. */
export function effortIndex(effort: string | undefined): number {
  const i = REASONING_EFFORTS.indexOf(effort as ReasoningEffort);
  return i >= 0 ? i : 1;
}

/** A preset with nothing of any model in it: the conservative answer, not an empty one. */
export const BLANK: Preset = { provider: "", model: "", label: "", thinking: true, reasoning_effort: "medium", images: false, context_window: 128000, max_output_tokens: 32000 };

/** What the endpoint said about the model that was picked, and what the form was filled in with from it. */
export type Picked = { preset: Preset; pricing: ModelEntry["pricing"] | null };

/** Fill the editable form from one model entry returned by the provider lookup API. */
export function prefilled(entry: ModelEntry, current: Preset): Preset {
  return {
    ...current,
    model: entry.id,
    label: entry.name && entry.name !== entry.id ? entry.name : current.label,
    images: entry.images ?? current.images,
    thinking: entry.reasoning ?? current.thinking,
    // The history a run may hold is capped below a very large hosted window on purpose: every turn
    // pays for the context it carries. llama.cpp windows at or below that cap are preserved exactly.
    context_window: entry.context_length ? Math.min(entry.context_length, 400000) : current.context_window,
    max_output_tokens: entry.max_output_tokens ? Math.min(entry.max_output_tokens, 64000) : current.max_output_tokens,
  };
}

/** A model id turned into a preset id: the same rule the server accepts (letters, digits, . _ -). */
export function presetIdFor(provider: string, model: string): string {
  const slug = model.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[.-]+|[.-]+$/g, "");
  return `${provider}.${slug || "model"}`;
}

/**
 * The form, for a model id typed by hand.
 *
 * Everything the form holds came from one catalogue entry, so for any other id it describes the
 * wrong model. Typing a different id therefore leaves nothing of the pick standing; typing the
 * picked id back changes nothing.
 */
export function retyped(value: string, picked: Picked): Picked {
  if (value.trim() === picked.preset.model) return picked;
  return { preset: { ...BLANK, provider: picked.preset.provider }, pricing: null };
}

/** The price to record against `model`, which is the endpoint's price only while it is that model's. */
export function priceFor(model: string, picked: Picked): ModelEntry["pricing"] | null {
  if (!model || model !== picked.preset.model) return null;
  const p = picked.pricing;
  return p?.input !== undefined && p?.output !== undefined ? p : null;
}
