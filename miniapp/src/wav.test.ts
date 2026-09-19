import { describe, expect, it } from "vitest";
import { mixMono, pcmToWav } from "./wav";

describe("a WAV the local recogniser can open", () => {
  it("is 16-bit mono PCM with a header the size of the samples", () => {
    const samples = new Float32Array([0, 0.5, -0.5, 1, -1]);
    const blob = pcmToWav(samples, 16000);
    expect(blob.type).toBe("audio/wav");
    return blob.arrayBuffer().then((buf) => {
      const view = new DataView(buf);
      const ascii = (o: number, n: number) => String.fromCharCode(...new Uint8Array(buf.slice(o, o + n)));
      expect(ascii(0, 4)).toBe("RIFF");
      expect(ascii(8, 4)).toBe("WAVE");
      expect(view.getUint16(22, true)).toBe(1);
      expect(view.getUint16(34, true)).toBe(16);
      expect(view.getUint32(24, true)).toBe(16000);
      expect(view.getUint32(40, true)).toBe(samples.length * 2);
      expect(buf.byteLength).toBe(44 + samples.length * 2);
    });
  });

  it("mixes channels by averaging", () => {
    const left = new Float32Array([1, 0]);
    const right = new Float32Array([0, 1]);
    expect([...mixMono([left, right])]).toEqual([0.5, 0.5]);
    expect(mixMono([left])).toBe(left);
  });
});
