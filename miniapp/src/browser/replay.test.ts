// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import type { BrowserFrame } from "../api";
import { frameOfRow, placeBox } from "./replay";

const frame = (no: number, kind: BrowserFrame["kind"], action_id = ""): BrowserFrame => ({ no, at: no * 1000, tab: "t1", url: "https://shop.test/", kind, action_id, w: 1280, h: 800, bytes: 1 });

describe("the replay", () => {
  it("finds a row's keyframe by the action it left, never a start or change frame", () => {
    const frames = [frame(1, "start"), frame(2, "action", "a7"), frame(3, "change"), frame(4, "action", "a9")];
    expect(frameOfRow(frames, { id: "41", action_id: "a9" })).toBe(3);
    expect(frameOfRow(frames, { id: "a7" })).toBe(1);
    expect(frameOfRow(frames, { id: "42", action_id: "" })).toBe(-1);
  });

  it("places the logged box by the picture's width over the viewport's", () => {
    // A 1280 px viewport drawn 640 px wide, 10 px from the left: half size, shifted.
    expect(placeBox({ x: 100, y: 40, w: 200, h: 60 }, { x: 10, y: 0, w: 640 }, 1280)).toEqual({ x: 60, y: 20, w: 100, h: 30 });
    // A viewport wider than the 1280 px a keyframe keeps: the picture's own ratio still places it.
    expect(placeBox({ x: 1920, y: 0, w: 0, h: 0 }, { x: 0, y: 0, w: 1280 }, 2560).x).toBe(960);
  });
});
