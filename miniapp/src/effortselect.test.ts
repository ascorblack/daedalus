import { describe, expect, it } from "vitest";
import { REASONING_EFFORTS, effortIndex } from "./models";

describe("the effort slider's stops", () => {
  it("are low, medium, high, xhigh in that order", () => {
    expect([...REASONING_EFFORTS]).toEqual(["low", "medium", "high", "xhigh"]);
  });

  it("puts a missing or unknown value on medium", () => {
    expect(effortIndex(undefined)).toBe(1);
    expect(effortIndex("")).toBe(1);
    expect(effortIndex("nope")).toBe(1);
  });

  it("names each stop by its index", () => {
    expect(effortIndex("low")).toBe(0);
    expect(effortIndex("medium")).toBe(1);
    expect(effortIndex("high")).toBe(2);
    expect(effortIndex("xhigh")).toBe(3);
  });
});
