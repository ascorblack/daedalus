import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// api.ts reads window at import time and these checks run without a DOM, so the browser globals it
// touches are put in place before the module graph is evaluated. Hoisted for that reason.
vi.hoisted(() => {
  const store = new Map<string, string>();
  Object.assign(globalThis, {
    window: { location: { search: "", pathname: "/app/", hash: "" }, history: { replaceState: () => undefined } },
    sessionStorage: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    },
    localStorage: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    },
  });
});

import { createLocalListener } from "./stt";
import type { ListenerHandlers } from "./voice";

// What is worth testing here without a real microphone is the negotiation, which is where this was
// wrong: the capture rate the browser actually gave has to reach the server, the context has to be
// resumed inside the gesture, and the tail of a sentence has to be flushed rather than dropped.
// Everything below drives the listener through fakes of exactly those three platform pieces.

type Call = { url: string; body?: BodyInit | null };

let calls: Call[] = [];
let contextState: AudioContextState;
let grantedRate: number;
let resumed: number;

class FakeAudioContext {
  sampleRate: number;
  state: AudioContextState;
  destination = {};
  audioWorklet = { addModule: async () => undefined };
  constructor(options?: { sampleRate?: number }) {
    // The whole point: the rate asked for is a request, and this browser answers with its own.
    this.sampleRate = grantedRate || options?.sampleRate || 16000;
    this.state = contextState;
  }
  async resume() {
    resumed += 1;
    this.state = "running";
  }
  async close() {
    this.state = "closed";
  }
  createMediaStreamSource() {
    return { connect: () => undefined };
  }
  createGain() {
    return { gain: { value: 1 }, connect: (n: unknown) => n };
  }
  createScriptProcessor() {
    return { onaudioprocess: null, connect: () => undefined, disconnect: () => undefined };
  }
}

class FakeWorkletNode {
  port: { onmessage: ((e: { data: Float32Array }) => void) | null } = { onmessage: null };
  connect(next: unknown) {
    return next;
  }
  disconnect() {}
}

function handlers(): ListenerHandlers & { finals: string[]; errors: string[] } {
  const finals: string[] = [];
  const errors: string[] = [];
  return {
    finals,
    errors,
    onInterim: () => undefined,
    onFinal: (t) => finals.push(t),
    onSpeechStart: () => undefined,
    onError: (m) => errors.push(m),
  };
}

beforeEach(() => {
  calls = [];
  contextState = "running";
  grantedRate = 0;
  resumed = 0;
  vi.stubGlobal("AudioContext", FakeAudioContext);
  vi.stubGlobal("AudioWorkletNode", FakeWorkletNode);
  vi.stubGlobal("URL", { ...URL, createObjectURL: () => "blob:x", revokeObjectURL: () => undefined });
  vi.stubGlobal("navigator", {
    mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop: () => undefined }] }) },
  });
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    calls.push({ url, body: init?.body ?? null });
    if (url.includes("/listen/open")) {
      return { ok: true, status: 200, json: async () => ({ stream: "server-minted", rate: 48000 }) };
    }
    return { ok: true, status: 200, json: async () => ({ text: "hello there", final: true }) };
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the local listener", () => {
  it("opens the stream with the rate the graph actually gave, not the one it asked for", async () => {
    grantedRate = 48000;
    const h = handlers();
    const listener = createLocalListener(h);
    await listener.start();
    const opened = calls.find((c) => c.url.includes("/listen/open"));
    expect(opened?.url).toContain("rate=48000");
    expect(h.errors).toEqual([]);
  });

  it("resumes a suspended context and refuses to pretend it is listening when it cannot be resumed", async () => {
    contextState = "suspended";
    const h = handlers();
    await createLocalListener(h).start();
    expect(resumed).toBe(1);
    expect(h.errors).toEqual([]);
  });

  it("says so when the browser will not start the microphone at all", async () => {
    contextState = "suspended";
    // A context that cannot leave "suspended" is an open microphone attached to a graph that never
    // runs — the page would otherwise sit on "Listening" forever.
    class Stuck extends FakeAudioContext {
      async resume() {
        resumed += 1;
      }
    }
    vi.stubGlobal("AudioContext", Stuck);
    const h = handlers();
    await createLocalListener(h).start();
    expect(h.errors[0]).toMatch(/would not start the microphone/);
    expect(calls.some((c) => c.url.includes("/listen/open"))).toBe(false);
  });

  it("numbers its chunks and flushes the tail with final=true rather than dropping it", async () => {
    const h = handlers();
    const listener = createLocalListener(h);
    await listener.start();
    listener.stop();
    await new Promise((r) => setTimeout(r, 0));
    const chunks = calls.filter((c) => c.url.startsWith("/api/voice/listen?"));
    expect(chunks.length).toBe(1);
    expect(chunks[0].url).toContain("stream=server-minted");
    expect(chunks[0].url).toContain("seq=1");
    expect(chunks[0].url).toContain("final=true");
    expect(h.finals).toEqual(["hello there"]);
    // final=true pops the stream server-side, so an abandon call after it would be a second close.
    expect(calls.some((c) => c.url.includes("/listen/close"))).toBe(false);
  });

  it("does not open a stream, or leave one open, when the microphone is refused", async () => {
    vi.stubGlobal("navigator", {
      mediaDevices: {
        getUserMedia: async () => {
          throw new Error("denied");
        },
      },
    });
    const h = handlers();
    await createLocalListener(h).start();
    expect(h.errors).toEqual(["the microphone was refused"]);
    expect(calls).toEqual([]);
  });
});
