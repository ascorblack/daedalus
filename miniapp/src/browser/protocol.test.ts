import { describe, expect, it } from "vitest";
import golden from "./testdata/frames.json";
import { decodeServerFrame, encodeAck, encodeAttach, encodeInput, encodeView, MAX_INPUT_BYTES, ProtocolError, splitText, type InputMessage } from "./protocol";

const fromHex = (hex: string) => Uint8Array.from(hex.match(/../g) ?? [], (b) => parseInt(b, 16));
const toHex = (bytes: Uint8Array) => [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");

type Golden = { name: string; direction: string; hex: string; value: Record<string, any> };

describe("the live view's wire format", () => {
  for (const g of golden.frames as Golden[]) {
    it(`${g.direction === "server" ? "reads" : "writes"} the golden ${g.name} frame`, () => {
      const v = g.value;
      if (g.direction === "server") {
        const frame = decodeServerFrame(fromHex(g.hex));
        if (frame.kind === "event") expect(frame).toEqual({ kind: "event", event: v.event });
        else expect({ kind: "frame", frame_no: frame.frameNo, meta: frame.meta, image_hex: toHex(frame.image) }).toEqual(v);
        return;
      }
      const bytes =
        v.kind === "attach" ? encodeAttach(v.attach)
        : v.kind === "ack" ? encodeAck(v.frame_no)
        : v.kind === "view" ? encodeView(v.view)
        : encodeInput(v.input as InputMessage);
      expect(toHex(bytes)).toBe(g.hex);
    });
  }

  for (const bad of golden.malformed) {
    it(`refuses the malformed ${bad.name} frame`, () => {
      expect(() => decodeServerFrame(fromHex(bad.hex))).toThrow(ProtocolError);
    });
  }

  it("writes the keys in the specification's order whatever order the caller used", () => {
    const bytes = encodeInput({ mods: 0, clicks: 1, button: "left", y: 2, x: 1, type: "up", t: "mouse" } as InputMessage);
    expect(new TextDecoder().decode(bytes.subarray(1))).toBe('{"t":"mouse","type":"up","x":1,"y":2,"button":"left","clicks":1,"mods":0}');
  });

  it("reads a frame that sits inside a larger buffer", () => {
    const hex = (golden.frames as Golden[]).find((g) => g.name === "frame-live")!.hex;
    const inner = fromHex(hex);
    const outer = new Uint8Array(inner.length + 9);
    outer.set(inner, 4);
    const frame = decodeServerFrame(outer.subarray(4, 4 + inner.length));
    expect(frame.kind === "frame" && frame.meta.scroll_y).toBe(1450);
  });

  it("refuses an acknowledgement it cannot write in 32 bits", () => {
    expect(() => encodeAck(0)).toThrow(ProtocolError);
    expect(() => encodeAck(2 ** 32)).toThrow(ProtocolError);
    expect(toHex(encodeAck(2 ** 32 - 1))).toBe("31ffffffff");
  });

  it("refuses an input longer than the host relays", () => {
    expect(() => encodeInput({ t: "text", text: "x".repeat(MAX_INPUT_BYTES) })).toThrow(ProtocolError);
  });

  it("cuts long text into pieces that each fit, never inside a character", () => {
    const pieces = splitText("ab😀".repeat(900));
    expect(pieces.join("")).toBe("ab😀".repeat(900));
    for (const p of pieces) {
      expect([...p].length).toBeLessThanOrEqual(1000);
      expect(() => encodeInput({ t: "text", text: p })).not.toThrow();
      expect(p.charCodeAt(p.length - 1) >= 0xd800 && p.charCodeAt(p.length - 1) <= 0xdbff).toBe(false);
    }
    const quotes = splitText('"'.repeat(3000));
    for (const p of quotes) expect(() => encodeInput({ t: "text", text: p })).not.toThrow();
    expect(splitText("")).toEqual([]);
  });
});
