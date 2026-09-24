import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FALLBACK_STACK, FONT_STACK, pickFontFamily } from "./load";

type Fonts = Pick<FontFaceSet, "load">;
const fonts = (load: () => Promise<FontFace[]>): Fonts => ({ load: vi.fn(load) as unknown as FontFaceSet["load"] });
const face = {} as FontFace;

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("pickFontFamily", () => {
  it("opens with the bundled face when both weights load", async () => {
    const set = fonts(async () => [face]);
    await expect(pickFontFamily(set)).resolves.toBe(FONT_STACK);
    expect(set.load).toHaveBeenCalledTimes(2);
  });

  it("falls back after three seconds rather than keep a terminal from opening", async () => {
    const result = pickFontFamily(fonts(() => new Promise<FontFace[]>(() => undefined)));
    await vi.advanceTimersByTimeAsync(2999);
    let settled = false;
    void result.then(() => (settled = true));
    await Promise.resolve();
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(2);
    await expect(result).resolves.toBe(FALLBACK_STACK);
  });

  it("falls back when a weight is missing or loading fails, and without a font API", async () => {
    await expect(pickFontFamily(fonts(async () => []))).resolves.toBe(FALLBACK_STACK);
    await expect(pickFontFamily(fonts(async () => Promise.reject(new Error("blocked"))))).resolves.toBe(FALLBACK_STACK);
    await expect(pickFontFamily(undefined)).resolves.toBe(FALLBACK_STACK);
  });

  it("never names the bundled face in the fallback, so it cannot swap in under a measured grid", () => {
    expect(FALLBACK_STACK).not.toMatch(/JetBrains/);
    expect(FONT_STACK.startsWith('"JetBrains Mono"')).toBe(true);
  });
});
