import { describe, expect, it } from "vitest";
import { filesKey, handleIds, keptBase, withoutAttachedList } from "./keptfiles";

describe("kept files", () => {
  it("finds each handle once, in order, and nothing that only looks like one", () => {
    const text = "see att:0123456789ab and att:ffffffffffff — again att:0123456789ab; not att:xyz or att:0123456789abc";
    expect(handleIds(text)).toEqual(["0123456789ab", "ffffffffffff"]);
    expect(handleIds("")).toEqual([]);
    expect(handleIds(undefined)).toEqual([]);
  });

  it("drops the host's list of attachments from the bubble and keeps the operator's words", () => {
    const text = [
      "Передай langpt",
      "",
      "Attached files (kept by handle; Read one with Peek(op='read', path=…); hand them to staff with Assign or Tell (files=[…])):",
      "- att:0123456789ab подсказки-free-pro.md (text/markdown, 8286 bytes)",
      "- huge.bin: not kept — huge.bin is 60.0 MB; files handed on are at most 50.0 MB",
    ].join("\n");
    expect(withoutAttachedList(text)).toBe("Передай langpt\n\n- huge.bin: not kept — huge.bin is 60.0 MB; files handed on are at most 50.0 MB");
    expect(withoutAttachedList("no list here\n- att:0123456789ab stays")).toBe("no list here\n- att:0123456789ab stays");
  });

  it("asks for the files by id and serves each from its own base", () => {
    expect(filesKey([])).toBeNull();
    expect(filesKey(["0123456789ab", "ffffffffffff"])).toBe("/api/files?ids=0123456789ab,ffffffffffff");
    expect(keptBase("0123456789ab")).toBe("/api/files/0123456789ab");
  });
});
