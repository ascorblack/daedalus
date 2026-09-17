// Streaming speech recognition against a model that runs on the server's own CPU.
//
// The browser's `SpeechRecognition` is Chrome, Edge and Safari; Firefox has never had it, and a
// Telegram webview may or may not. Where a local model is installed this replaces it outright, and it
// behaves the same way from the page's point of view: interim words while the sentence is being said,
// a final when it ends. The difference is only where the recognising happens.
//
// Audio goes out as raw 16 kHz mono PCM16, a fifth of a second per request, POSTed rather than
// socketed — the app authenticates with a header, and a browser cannot put one on a WebSocket.
// Each answer carries the words heard so far; the one marked final is an utterance.

import { api } from "./api";
import type { Listener, ListenerHandlers } from "./voice";

/** What the models want, and what the capture graph is asked for directly. */
const RATE = 16000;

/** How much audio one request carries. Short enough that words appear as they are said. */
const CHUNK_MS = 200;

/** Frames the worklet hands over at a time; 128 is the platform's own block size. */
const WORKLET_SOURCE = `
class Tap extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) this.port.postMessage(new Float32Array(channel));
    return true;
  }
}
registerProcessor("daedalus-tap", Tap);
`;

function toPcm16(samples: Float32Array): ArrayBuffer {
  const out = new DataView(new ArrayBuffer(samples.length * 2));
  for (let i = 0; i < samples.length; i++) {
    // Clamp before scaling: a sample above 1 wraps to a loud click instead of clipping quietly.
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    out.setInt16(i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
  }
  return out.buffer;
}

/** Whether this browser can capture at all. The rest is the server's problem, not the page's. */
export function localListenSupported(): boolean {
  return typeof AudioContext !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
}

/**
 * A listener that sends samples to the server's model and shows what comes back.
 *
 * The stream id is this page's, for this microphone session: the server keeps one decoder per id and
 * lets go of it when the page closes it or stops feeding it. Errors are counted rather than shouted
 * about — one dropped chunk on a flaky link is not worth interrupting a sentence for, several in a
 * row is.
 */
export function createLocalListener(h: ListenerHandlers): Listener {
  const stream_id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  let media: MediaStream | null = null;
  let context: AudioContext | null = null;
  let node: AudioWorkletNode | ScriptProcessorNode | null = null;
  let pending: Float32Array[] = [];
  let held = 0;
  let inflight = false;
  let failures = 0;
  let spoke = false;
  let wanted = false;

  const url = (final: boolean) => `/api/voice/listen?stream=${encodeURIComponent(stream_id)}${final ? "&final=true" : ""}`;

  const post = async (body: ArrayBuffer, final: boolean) => {
    const response = await fetch(url(final), {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", ...api.authHeaders() },
      body,
    });
    if (!response.ok) throw new Error(response.status === 409 ? "no local speech model is selected" : `the stream was refused (${response.status})`);
    return (await response.json()) as { text: string; final: boolean };
  };

  const drain = async () => {
    if (inflight || !pending.length || !wanted) return;
    inflight = true;
    const batch = pending;
    pending = [];
    held = 0;
    const total = batch.reduce((n, part) => n + part.length, 0);
    const joined = new Float32Array(total);
    let at = 0;
    for (const part of batch) {
      joined.set(part, at);
      at += part.length;
    }
    try {
      const result = await post(toPcm16(joined), false);
      failures = 0;
      if (result.final) {
        // An endpoint: the model decided the sentence ended. That is one utterance, and the page
        // treats it exactly as it treats one the browser recognised itself.
        if (result.text.trim()) {
          spoke = false;
          h.onInterim("");
          h.onFinal(result.text.trim());
        }
      } else if (result.text) {
        if (!spoke) {
          spoke = true;
          h.onSpeechStart();
        }
        h.onInterim(result.text);
      }
    } catch (e) {
      // A single failure is a dropped request; three in a row is the feature not working, and the
      // operator should be told rather than left watching a microphone that hears nothing.
      if (++failures >= 3) h.onError(e instanceof Error ? e.message : "the stream stopped");
    } finally {
      inflight = false;
      if (pending.length) void drain();
    }
  };

  const take = (samples: Float32Array) => {
    if (!wanted) return;
    pending.push(samples);
    held += samples.length;
    if (held >= (RATE * CHUNK_MS) / 1000) void drain();
  };

  const start = async () => {
    if (media) return;
    wanted = true;
    try {
      media = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 } });
    } catch {
      wanted = false;
      h.onError("the microphone was refused");
      return;
    }
    // Asking the graph for the model's rate is what keeps a resampler out of this file; where a
    // browser refuses and gives its own rate, the server resamples instead.
    context = new AudioContext({ sampleRate: RATE });
    const source = context.createMediaStreamSource(media);
    try {
      const moduleUrl = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "application/javascript" }));
      await context.audioWorklet.addModule(moduleUrl);
      URL.revokeObjectURL(moduleUrl);
      const worklet = new AudioWorkletNode(context, "daedalus-tap");
      worklet.port.onmessage = (e) => take(e.data as Float32Array);
      source.connect(worklet);
      // A worklet with nothing downstream is not pulled on in every browser; a silent sink is enough
      // to keep the graph running without anything being heard.
      const sink = context.createGain();
      sink.gain.value = 0;
      worklet.connect(sink).connect(context.destination);
      node = worklet;
    } catch {
      // Older webviews have no AudioWorklet. The deprecated processor still captures, and this page
      // only ever reads from it.
      const processor = context.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = (e) => take(new Float32Array(e.inputBuffer.getChannelData(0)));
      source.connect(processor);
      processor.connect(context.destination);
      node = processor;
    }
  };

  const stop = () => {
    wanted = false;
    pending = [];
    held = 0;
    try {
      node?.disconnect();
    } catch {
      /* already gone */
    }
    node = null;
    media?.getTracks().forEach((t) => t.stop());
    media = null;
    void context?.close();
    context = null;
    // Best effort: the server drops an idle stream on its own, so a lost beacon costs one decoder
    // for a couple of minutes and nothing else.
    void fetch(`/api/voice/listen/close?stream=${encodeURIComponent(stream_id)}`, { method: "POST", headers: api.authHeaders() }).catch(() => undefined);
  };

  return { kind: "local", start, stop };
}
