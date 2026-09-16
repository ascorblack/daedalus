import { describe, expect, it } from "vitest";
import type { Preset } from "./api";
import { presetIdFor, priceFor, retyped } from "./models";

const OPUS: Preset = {
  provider: "openrouter",
  model: "anthropic/claude-opus-5",
  label: "Claude Opus 5",
  thinking: true,
  reasoning_effort: "medium",
  images: true,
  context_window: 400000,
  max_output_tokens: 32000,
};
const OPUS_PRICE = { input: 5, output: 25, cache_hit: 0.5 };

describe("a model id typed after one was picked", () => {
  it("keeps the pick when the id is the one that was picked", () => {
    const out = retyped("anthropic/claude-opus-5", { preset: OPUS, pricing: OPUS_PRICE });
    expect(out.preset).toBe(OPUS);
    expect(out.pricing).toBe(OPUS_PRICE);
  });

  it("keeps the pick through the whitespace of typing", () => {
    expect(retyped("  anthropic/claude-opus-5 ", { preset: OPUS, pricing: OPUS_PRICE }).preset).toBe(OPUS);
  });

  it("leaves nothing of another model's description standing", () => {
    const out = retyped("z-ai/glm-5.3-flash", { preset: OPUS, pricing: OPUS_PRICE });
    expect(out.pricing).toBe(null);
    expect(out.preset.label).toBe("");
    expect(out.preset.images).toBe(false);
    expect(out.preset.context_window).toBe(128000);
    expect(out.preset.model).toBe("");
    expect(out.preset.provider).toBe("openrouter");
  });
});

describe("the price a model is recorded at", () => {
  it("is the endpoint's for the model the endpoint described", () => {
    expect(priceFor("anthropic/claude-opus-5", { preset: OPUS, pricing: OPUS_PRICE })).toBe(OPUS_PRICE);
  });

  it("is nothing for a model that was typed instead of picked", () => {
    expect(priceFor("z-ai/glm-5.3-flash", { preset: OPUS, pricing: OPUS_PRICE })).toBe(null);
  });

  it("is nothing when the endpoint published no price", () => {
    expect(priceFor("anthropic/claude-opus-5", { preset: OPUS, pricing: { input: 5 } })).toBe(null);
    expect(priceFor("anthropic/claude-opus-5", { preset: OPUS, pricing: null })).toBe(null);
  });
});

describe("preset ids", () => {
  it("are the model id with what the server does not accept replaced", () => {
    expect(presetIdFor("openrouter", "z-ai/glm-5.3-flash")).toBe("openrouter.z-ai-glm-5.3-flash");
  });
});
