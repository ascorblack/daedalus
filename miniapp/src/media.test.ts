import { describe, expect, it } from "vitest";
import type { MediaPresentation } from "./api";
import { mediaCopyText, splitMediaAnswer } from "./mediaformat";

const id = "11111111-1111-1111-1111-111111111111";
const presentation: MediaPresentation = {
  id,
  layout: "single",
  items: [{ id: "item", kind: "image", mime_type: "image/png", filename: "result.png", byte_size: 3, width: 1, height: 1, alt: "Result", caption: "After saving" }],
};

describe("inline media answers", () => {
  it("keeps the media at its position between text blocks", () => {
    const parts = splitMediaAnswer(`Before.\n\n![Result](daedalus-media:${id})\n\nAfter.`, [presentation]);
    expect(parts.map((part) => part.kind)).toEqual(["text", "media", "text"]);
    expect(parts[0]).toMatchObject({ text: "Before." });
    expect(parts[2]).toMatchObject({ text: "After." });
  });

  it("does not activate a reference the server did not bind", () => {
    const text = "![guess](daedalus-media:22222222-2222-2222-2222-222222222222)";
    expect(splitMediaAnswer(text, [presentation])).toEqual([{ kind: "text", text }]);
  });

  it("copies a human label rather than an internal media id", () => {
    expect(mediaCopyText(`See\n\n![Result](daedalus-media:${id})`, [presentation])).toBe("See\n\n[After saving]");
  });
});
