import { describe, expect, it } from "vitest";
import golden from "./testdata/frames.json";
import { decodeServerFrame, encodeAck, encodeAttach, encodeInput, encodeResize, MAX_INPUT_BYTES, ProtocolError } from "./protocol";

const fromHex = (hex: string) => Uint8Array.from(hex.match(/../g) ?? [], (b) => parseInt(b, 16));
const toHex = (bytes: Uint8Array) => [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
const text = (bytes: Uint8Array) => new TextDecoder().decode(bytes);

type Golden = { name: string; direction: string; hex: string; value: Record<string, any> };

describe("the terminal wire format", () => {
  for (const g of golden.frames as Golden[]) {
    it(`${g.direction === "server" ? "reads" : "writes"} the golden ${g.name} frame`, () => {
      const v = g.value;
      if (g.direction === "server") {
        const frame = decodeServerFrame(fromHex(g.hex));
        if (frame.kind === "event") expect(frame).toEqual({ kind: "event", event: v.event });
        else expect({ ...frame, data: text(frame.data) }).toEqual(v);
        return;
      }
      const bytes =
        v.kind === "input" ? encodeInput(v.data)[0]
        : v.kind === "resize" ? encodeResize(v.cols, v.rows, v.px_w, v.px_h)
        : v.kind === "ack" ? encodeAck(v.seq)
        : encodeAttach(v.request);
      expect(toHex(bytes)).toBe(g.hex);
    });
  }

  for (const bad of golden.malformed) {
    it(`refuses the malformed ${bad.name} frame`, () => {
      expect(() => decodeServerFrame(fromHex(bad.hex))).toThrow(ProtocolError);
    });
  }

  it("passes an event it does not know through instead of failing", () => {
    const frame = decodeServerFrame(new Uint8Array([0x03, ...new TextEncoder().encode('{"type":"future","x":1}')]));
    expect(frame).toEqual({ kind: "event", event: { type: "unknown", raw: { type: "future", x: 1 } } });
  });

  it("reads a frame that sits inside a larger buffer", () => {
    const outer = new Uint8Array(20);
    outer.set(fromHex("01000000000000000a6f6b"), 5);
    const frame = decodeServerFrame(outer.subarray(5, 16));
    expect(frame.kind === "output" && frame.seq).toBe(10);
  });

  it("splits long input into frames the daemon accepts", () => {
    const frames = encodeInput("x".repeat(MAX_INPUT_BYTES * 2 + 5));
    expect(frames.map((f) => f.length)).toEqual([MAX_INPUT_BYTES + 1, MAX_INPUT_BYTES + 1, 6]);
    expect(frames.every((f) => f[0] === 0x10)).toBe(true);
    expect(encodeInput("")).toEqual([]);
  });

  it("clamps a resize to what fits in 16 bits", () => {
    expect(toHex(encodeResize(70000, -3, 1.6, 0))).toBe("11ffff000000020000");
  });

  it("refuses to write a sequence number it cannot represent exactly", () => {
    expect(() => encodeAck(2 ** 60)).toThrow(ProtocolError);
    expect(() => encodeAck(-1)).toThrow(ProtocolError);
  });
});
